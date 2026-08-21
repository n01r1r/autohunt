#!/usr/bin/env python3
"""Governor guarantees the --selftest does not cover, especially RESUME.

run.py's --selftest proves win + kill-switch. It never crashes mid-run and
restarts, so resume (move #3, durable state) is unverified there. This file
re-verifies resume plus the pure logic (classify), the refusal gates, and the
unattended-run guarantees: plateau never escalates, non-finite metrics are
rejected, hung commands are tree-killed, baseline precedes the first maker
run, improve mode ends at the budget, protected files trip `tampered`, and
rollback restores the incumbent best.

A crash is simulated the honest way: pre-seed the state file with the records a
dying process would have flushed (one line per attempt is durable), then call
run_contract and prove it picks up from prior count - not from attempt 1.

Usage:
    python test_governor.py
"""
from __future__ import annotations
import json
import tempfile
import time
from pathlib import Path
import sys

from run import run_contract, classify, numeric, TIMEOUT_EXIT, _identity, _ledger_header

PY = f'"{sys.executable}"'


def _seed_counter(root: Path, win_at: int) -> None:
    """maker increments n.txt; checker passes at n>=win_at; metric prints n."""
    (root / "inc.py").write_text(
        "from pathlib import Path\n"
        "f=Path('n.txt'); n=int(f.read_text()) if f.exists() else 0\n"
        "f.write_text(str(n+1))\n", encoding="utf-8")
    (root / "check.py").write_text(
        "import sys; from pathlib import Path\n"
        "n=int(Path('n.txt').read_text()) if Path('n.txt').exists() else 0\n"
        f"sys.exit(0 if n>={win_at} else 1)\n", encoding="utf-8")
    (root / "metric.py").write_text(
        "from pathlib import Path\n"
        "print(Path('n.txt').read_text() if Path('n.txt').exists() else '0')\n",
        encoding="utf-8")


def _records(state_file: Path, tag: str) -> list:
    return [record for line in state_file.read_text(encoding="utf-8").splitlines()
            if line.strip()
            for record in [json.loads(line)]
            if record.get("contract") == tag
            and record.get("phase") != "snapshot_prepare"]


def _write_state(state_file: Path, recs: list, contract_path: Path | None = None,
                 contract: dict | None = None) -> None:
    state_file.parent.mkdir(parents=True, exist_ok=True)
    identity = _identity(contract_path, contract) if contract_path and contract else None
    prepared = []
    for rec in recs:
        item = dict(rec)
        if identity:
            item["identity"] = identity
            item.update(identity)
        prepared.append(item)
    header = json.dumps(_ledger_header(), separators=(",", ":")) + "\n"
    state_file.write_text(header + "".join(json.dumps(r) + "\n" for r in prepared),
                          encoding="utf-8")


# ---- classify: the pure decision logic -------------------------------------

def test_classify() -> None:
    # won trumps everything
    assert classify(True, 5, [3, 4], "increase", 2) == "won"
    # no metric, not passed -> ambiguous, hand to human
    assert classify(False, None, [1], "increase", 2) == "unknown"
    # first metric, nothing to compare -> progress
    assert classify(False, 5, [], "increase", 2) == "progress"
    assert classify(False, 5, [None], "increase", 2) == "progress"
    # direction increase
    assert classify(False, 6, [5], "increase", 2) == "progress"
    assert classify(False, 4, [5], "increase", 2) == "regression"
    # direction decrease (default goal)
    assert classify(False, 4, [5], "decrease", 2) == "progress"
    assert classify(False, 6, [5], "decrease", 2) == "regression"
    # comparison base is the incumbent BEST, not the previous value:
    # 10 -> 8 -> 12(rej) -> 9 is still worse than best 8, never "progress"
    assert classify(False, 9, [10, 8, 12], "decrease", 2) == "regression"
    # equal to best: stagnation once the window of equal values is filled,
    # else plateau (deterministic continue - a tie is never a human question)
    assert classify(False, 5, [5], "increase", 2) == "stagnation"
    assert classify(False, 5, [5], "increase", 3) == "plateau"       # window not yet full
    assert classify(False, 5, [5, 5], "increase", 3) == "stagnation"
    # the 5 -> 4 -> 4 post-improvement tie: plateau, NOT unknown/escalate
    assert classify(False, 4, [5, 4], "decrease", 3) == "plateau"
    assert classify(False, 4, [5, 4, 4], "decrease", 3) == "stagnation"
    # success-only contract (no progress command): a non-passing attempt is an
    # unfinished retry, not an ambiguous signal -> progress (keep going), never
    # unknown/escalate. A configured progress cmd that yields no number stays
    # unknown (the default), so the escalation path is not lost.
    assert classify(False, None, [1], "increase", 2, has_progress=True) == "unknown"
    assert classify(False, None, [1], "increase", 2, has_progress=False, has_maker=True) == "progress"
    assert classify(False, None, [], "decrease", 2, has_progress=False, has_maker=True) == "progress"
    # checker-only gate (no maker, no progress): nothing changes on retry, so a
    # failing gate is terminal -> unknown -> escalate, NOT an endless retry.
    assert classify(False, None, [], "decrease", 2, has_progress=False, has_maker=False) == "unknown"
    print("classify ok  (won/progress/regression/stagnation/plateau/unknown, best-based)")


def test_numeric_rejects_nonfinite() -> None:
    """NaN/inf is an invalid signal -> None -> unknown, never 'progress'."""
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        (root / "nan.py").write_text("print('nan')", encoding="utf-8")
        (root / "inf.py").write_text("print('inf')", encoding="utf-8")
        (root / "ok.py").write_text("print('nan')\nprint('3.5')", encoding="utf-8")
        assert numeric(f"{PY} nan.py", root) is None, "nan must be rejected"
        assert numeric(f"{PY} inf.py", root) is None, "inf must be rejected"
        assert numeric(f"{PY} ok.py", root) == 3.5, "finite line must still parse"
    print("numeric ok   (NaN/inf rejected; finite value parsed)")


def test_success_only_retries_to_won() -> None:
    """maker + success, NO progress command -> retries to WON, never escalates.

    Regression: with the unknown->escalate default, a non-passing attempt in a
    success-only contract classified as 'unknown' (metric is None because no
    progress command exists) and escalated at attempt 1 instead of retrying.
    """
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        _seed_counter(root, win_at=3)                # passes only at the 3rd maker run
        tag = "successonly.json"
        contract = {
            "maker": {"command": f"{PY} inc.py"},
            "success": {"command": f"{PY} check.py"},
            "budget": {"attempts": 5},
            "state": {"file": "state.jsonl"},
        }                                            # deliberately no "progress"
        cpath = root / tag
        cpath.write_text(json.dumps(contract), encoding="utf-8")
        assert run_contract(cpath, root) == "WON", \
            "success-only contract must retry to WON, not escalate at attempt 1"
    print("success-only ok (no progress cmd retries to WON, no premature escalate)")


def test_success_only_metricless_never_snapshots() -> None:
    """A metric-less 'progress' retry must not enter the snapshot/incumbent path.

    Regression guard: classifying a no-progress non-passing attempt as 'progress'
    must not make it a measured incumbent. With snapshot/rollback configured but
    NO progress command, the snapshot must never run on the unmeasured retry
    state, and no metric-less attempt may be recorded as an incumbent.
    """
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        _seed_counter(root, win_at=3)                # passes only at the 3rd run
        # snapshot writes a sentinel; if it ever runs on a retry the file appears
        (root / "snap.py").write_text(
            "from pathlib import Path\n"
            "p=Path('snap_calls'); p.write_text(str(int(p.read_text()) + 1) if p.exists() else '1')\n",
            encoding="utf-8")
        (root / "restore.py").write_text("pass\n", encoding="utf-8")
        tag = "sosnap.json"
        contract = {
            "maker": {"command": f"{PY} inc.py"},
            "success": {"command": f"{PY} check.py"},
            "budget": {"attempts": 5},
            "state": {"file": "state.jsonl"},
            "failure_policy": {"snapshot_command": f"{PY} snap.py",
                               "rollback_command": f"{PY} restore.py"},
        }                                            # snapshot set, but no progress
        cpath = root / tag
        cpath.write_text(json.dumps(contract), encoding="utf-8")
        assert run_contract(cpath, root) == "WON"
        assert not (root / "snap_calls").exists(), \
            "snapshot must never run on an unmeasured (metric-less) retry"
        recs = _records(root / "state.jsonl", tag)
        for r in recs:
            if r.get("verdict") == "progress":
                assert r.get("metric") is None and r.get("incumbent") is False, \
                    f"metric-less progress must not be an incumbent: {r}"
            assert r.get("snapshot_status") in (None, "not_requested"), \
                f"no snapshot should be requested for a metric-less run: {r}"
    print("so-snapshot ok (metric-less retry never snapshots nor becomes incumbent)")


def test_checker_only_gate_escalates() -> None:
    """Checker-only contract (no maker, no progress) escalates on a failing gate.

    Nothing changes between attempts, so a failing gate is terminal - hand to a
    human at once rather than re-running the identical check to no effect. This
    is the self-evolve gate's contract shape; the success-only retry fix must
    not turn it into a futile retry loop.
    """
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        tag = "gate.json"
        contract = {
            "success": {"command": f'{PY} -c "import sys; sys.exit(1)"'},
            "budget": {"attempts": 3},
            "state": {"file": "state.jsonl"},
        }                                            # no maker, no progress
        cpath = root / tag
        cpath.write_text(json.dumps(contract), encoding="utf-8")
        assert run_contract(cpath, root) == "ESCALATE", \
            "checker-only failing gate must escalate, not retry to UNRESOLVED"
    print("checker-only ok (no-maker failing gate escalates, not a retry loop)")


# ---- resume: the re-verification -------------------------------------------

def test_resume_continues() -> None:
    """Crash after 2 attempts -> restart resumes at attempt 3 and drives to WON.

    Proves: prior attempts are counted, attempt numbers continue (not reset to
    1), the cap accounts for work already done, and the maker only runs for the
    REMAINING attempts.
    """
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        _seed_counter(root, win_at=4)
        (root / "n.txt").write_text("2", encoding="utf-8")           # 2 increments already happened
        tag = "resume.json"
        state = root / "state.jsonl"
        contract = {
            "goal": {"direction": "increase"},
            "maker": {"command": f"{PY} inc.py"},
            "success": {"command": f"{PY} check.py"},
            "progress": {"command": f"{PY} metric.py"},
            "budget": {"attempts": 10},
            "state": {"file": "state.jsonl"},
        }
        cpath = root / tag
        cpath.write_text(json.dumps(contract), encoding="utf-8")
        _write_state(state, [                                         # what the dead process flushed
            {"contract": tag, "attempt": 1, "verdict": "progress", "metric": 1.0, "action": "continue"},
            {"contract": tag, "attempt": 2, "verdict": "progress", "metric": 2.0, "action": "continue"},
        ], cpath, contract)

        assert run_contract(cpath, root) == "WON", "resume must drive the counter to WON"
        assert (root / "n.txt").read_text() == "4", "maker must run only the 2 remaining attempts, not restart from 0"
        recs = _records(state, tag)
        assert [r["attempt"] for r in recs] == [1, 2, 3, 4], "attempt numbers must continue past the crash"
        assert recs[-1]["verdict"] == "won"
    print("resume ok    (continues from prior count, drives to WON)")


def test_resume_respects_cap() -> None:
    """Crash after 2 of 3 attempts on an unwinnable contract -> exactly 1 more,
    then UNRESOLVED. The kill switch counts prior attempts; resume cannot exceed it."""
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        tag = "stuck.json"
        state = root / "state.jsonl"
        contract = {
            "success": {"command": f'{PY} -c "import sys; sys.exit(1)"'},
            "budget": {"attempts": 3},
            "state": {"file": "state.jsonl"},
            "failure_policy": {"unknown": "continue"},
        }
        cpath = root / tag
        cpath.write_text(json.dumps(contract), encoding="utf-8")
        _write_state(state, [
            {"contract": tag, "attempt": 1, "verdict": "unknown", "metric": None, "action": "continue"},
            {"contract": tag, "attempt": 2, "verdict": "unknown", "metric": None, "action": "continue"},
        ], cpath, contract)

        assert run_contract(cpath, root) == "UNRESOLVED"
        assert len(_records(state, tag)) == 3, "cap=3 must hold across the resume (2 prior + 1 new)"
    print("resume ok    (cap held across restart: 2 prior + 1 = 3, then UNRESOLVED)")


def test_resume_won_short_circuits() -> None:
    """A prior WON record ends the run before the maker can run again."""
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        tag = "done.json"
        state = root / "state.jsonl"
        contract = {
            "maker": {"command": f"{PY} -c \"open('SENTINEL','w').close()\""},
            "success": {"command": f'{PY} -c "import sys; sys.exit(1)"'},
            "budget": {"attempts": 3},
            "state": {"file": "state.jsonl"},
        }
        cpath = root / tag
        cpath.write_text(json.dumps(contract), encoding="utf-8")
        _write_state(state, [
            {"contract": tag, "attempt": 1, "verdict": "won", "metric": None, "action": "stop"},
        ], cpath, contract)

        assert run_contract(cpath, root) == "WON", "prior WON must short-circuit"
        assert not (root / "SENTINEL").exists(), "maker must not run once a prior WON is seen"
    print("resume ok    (prior WON short-circuits; maker never re-runs)")


# ---- unattended-run guarantees ---------------------------------------------

def test_baseline_first() -> None:
    """With a progress command, the pristine metric is recorded as attempt 0
    BEFORE the maker ever runs, so attempt 1 has a real comparison."""
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        _seed_counter(root, win_at=2)
        tag = "base.json"
        contract = {
            "goal": {"direction": "increase"},
            "maker": {"command": f"{PY} inc.py"},
            "success": {"command": f"{PY} check.py"},
            "progress": {"command": f"{PY} metric.py"},
            "budget": {"attempts": 5},
            "state": {"file": "state.jsonl"},
        }
        cpath = root / tag
        cpath.write_text(json.dumps(contract), encoding="utf-8")
        assert run_contract(cpath, root) == "WON"
        recs = _records(root / "state.jsonl", tag)
        assert recs[0]["attempt"] == 0 and recs[0]["verdict"] == "baseline", \
            "first record must be the pristine baseline"
        assert recs[0]["metric"] == 0.0, "baseline must be measured before any maker run"
        assert recs[1]["attempt"] == 1 and recs[1]["verdict"] == "progress"
    print("baseline ok  (attempt 0 = pristine metric, measured pre-maker)")


def test_timeout_kills_hung_maker() -> None:
    """A hung maker is tree-killed at timeout_sec (exit 124) and the run
    continues to its verdict instead of blocking forever."""
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        tag = "hang.json"
        contract = {
            "maker": {"command": f'{PY} -c "import time; time.sleep(30)"',
                      "timeout_sec": 1},
            "success": {"command": f'{PY} -c "import sys; sys.exit(1)"'},
            "budget": {"attempts": 1},
            "state": {"file": "state.jsonl"},
        }
        cpath = root / tag
        cpath.write_text(json.dumps(contract), encoding="utf-8")
        t0 = time.monotonic()
        assert run_contract(cpath, root) == "ESCALATE"
        assert time.monotonic() - t0 < 15, "timeout must bound the attempt, not sleep 30s"
        recs = _records(root / "state.jsonl", tag)
        assert recs[-1]["maker_exit"] == TIMEOUT_EXIT, "timeout must be recorded as exit 124"
    print("timeout ok   (hung maker tree-killed, exit 124 recorded, loop continues)")


def test_hung_checker_is_unknown() -> None:
    """A success command that hits its timeout is an INVALID signal: the
    verdict is `unknown`, never `progress` — even if the metric improved."""
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        _seed_counter(root, win_at=999)
        tag = "hungchk.json"
        contract = {
            "goal": {"direction": "increase"},
            "maker": {"command": f"{PY} inc.py"},
            "success": {"command": f'{PY} -c "import time; time.sleep(30)"',
                        "timeout_sec": 1},
            "progress": {"command": f"{PY} metric.py"},
            "budget": {"attempts": 3},
            "state": {"file": "state.jsonl"},
            "failure_policy": {"unknown": "escalate"},
        }
        cpath = root / tag
        cpath.write_text(json.dumps(contract), encoding="utf-8")
        t0 = time.monotonic()
        assert run_contract(cpath, root) == "ESCALATE", "hung checker must escalate, not score"
        assert time.monotonic() - t0 < 20, "checker timeout must bound the attempt"
        recs = _records(root / "state.jsonl", tag)
        last = recs[-1]
        assert last["verdict"] == "unknown" and last["success_exit"] == TIMEOUT_EXIT, last
        assert last["best"] == 0.0, \
            f"untrusted metric must not advance best (got {last['best']})"
    print("checker ok   (hung success command -> unknown + exit 124, best untouched)")


def test_improve_legacy_baseline() -> None:
    """A ledger without a durable baseline is refused as legacy state."""
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        _seed_counter(root, win_at=999)
        (root / "n.txt").write_text("6", encoding="utf-8")
        tag = "legacy.json"
        state = root / "state.jsonl"
        contract = {
            "goal": {"direction": "increase", "mode": "improve"},
            "maker": {"command": f"{PY} inc.py"},
            "progress": {"command": f"{PY} metric.py"},
            "budget": {"attempts": 3},
            "state": {"file": "state.jsonl"},
        }
        cpath = root / tag
        cpath.write_text(json.dumps(contract), encoding="utf-8")
        _write_state(state, [    # prior durable records with current identity
            {"contract": tag, "attempt": 1, "verdict": "progress", "metric": 5.0, "action": "continue"},
            {"contract": tag, "attempt": 2, "verdict": "progress", "metric": 6.0, "action": "continue"},
        ], cpath, contract)
        try:
            run_contract(cpath, root)
        except SystemExit as exc:
            assert "no durable attempt-0 baseline" in str(exc)
        else:
            raise AssertionError("legacy improve ledger must be refused")
    print("legacy ok    (improve ledger without baseline is refused)")


def test_resume_drops_unscored_metric() -> None:
    """A crashed run whose last record was unknown/tampered must not feed that
    untrusted metric back into the incumbent on resume (no false IMPROVED)."""
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        _seed_counter(root, win_at=999)
        (root / "n.txt").write_text("5", encoding="utf-8")
        tag = "unscored.json"
        state = root / "state.jsonl"
        contract = {
            "goal": {"direction": "increase", "mode": "improve"},
            "maker": {"command": f'{PY} -c "pass"'},       # changes nothing
            "progress": {"command": f"{PY} metric.py"},
            "budget": {"attempts": 2},
            "state": {"file": "state.jsonl"},
        }
        cpath = root / tag
        cpath.write_text(json.dumps(contract), encoding="utf-8")
        _write_state(state, [
            {"contract": tag, "attempt": 0, "verdict": "baseline", "metric": 5.0, "action": "none"},
            {"contract": tag, "attempt": 1, "verdict": "unknown", "metric": 99.0,
             "action": "continue", "success_exit": 124},   # hung checker's untrusted reading
        ], cpath, contract)
        assert run_contract(cpath, root) == "UNRESOLVED", \
            "unscored 99.0 must not resurrect as best on resume (false IMPROVED)"
    print("unscored ok  (resume drops unknown/tampered metrics from the incumbent)")


def test_baseline_snapshot_restores_pristine() -> None:
    """The baseline snapshot is the initial incumbent: a regression on
    attempt 1 (before any progress) must restore the PRISTINE state."""
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        _seed_counter(root, win_at=4)
        (root / "n.txt").write_text("3", encoding="utf-8")           # pristine = 3
        (root / "maker.py").write_text(
            "from pathlib import Path\n"
            "if not Path('did_bad').exists():\n"
            "    Path('did_bad').write_text('x'); Path('n.txt').write_text('-5')\n"
            "else:\n"
            "    n=int(Path('n.txt').read_text()); Path('n.txt').write_text(str(n+1))\n",
            encoding="utf-8")
        (root / "snap.py").write_text(
            "from pathlib import Path\n"
            "Path('best.txt').write_text(Path('n.txt').read_text() if Path('n.txt').exists() else '0')\n",
            encoding="utf-8")
        (root / "restore.py").write_text(
            "from pathlib import Path\n"
            "Path('n.txt').write_text(Path('best.txt').read_text())\n", encoding="utf-8")
        tag = "basesnap.json"
        contract = {
            "goal": {"direction": "increase"},
            "maker": {"command": f"{PY} maker.py"},
            "success": {"command": f"{PY} check.py"},
            "progress": {"command": f"{PY} metric.py"},
            "budget": {"attempts": 3},
            "state": {"file": "state.jsonl"},
            "failure_policy": {"regression": "rollback",
                               "snapshot_command": f"{PY} snap.py",
                               "rollback_command": f"{PY} restore.py"},
        }
        cpath = root / tag
        cpath.write_text(json.dumps(contract), encoding="utf-8")
        assert run_contract(cpath, root) == "WON", "pristine restore must enable the win"
        verdicts = [r["verdict"] for r in _records(root / "state.jsonl", tag)]
        assert verdicts == ["baseline", "regression", "won"], verdicts
    print("basesnap ok  (attempt-1 regression restores the pristine baseline)")


def test_rollback_failure_escalates() -> None:
    """A failed rollback leaves an unrestored regression: ambiguous state,
    must escalate instead of silently continuing."""
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        _seed_counter(root, win_at=999)
        (root / "n.txt").write_text("3", encoding="utf-8")
        (root / "bad.py").write_text(
            "from pathlib import Path\nPath('n.txt').write_text('-5')\n", encoding="utf-8")
        tag = "rbfail.json"
        contract = {
            "goal": {"direction": "increase"},
            "maker": {"command": f"{PY} bad.py"},
            "success": {"command": f"{PY} check.py"},
            "progress": {"command": f"{PY} metric.py"},
            "budget": {"attempts": 3},
            "state": {"file": "state.jsonl"},
            "failure_policy": {"regression": "rollback",
                               "snapshot_command": f'{PY} -c "pass"',
                               "rollback_command": f'{PY} -c "import sys; sys.exit(1)"'},
        }
        cpath = root / tag
        cpath.write_text(json.dumps(contract), encoding="utf-8")
        assert run_contract(cpath, root) == "ESCALATE", "failed rollback must escalate"
        recs = _records(root / "state.jsonl", tag)
        assert recs[-1]["verdict"] == "regression"
    print("rbfail ok    (rollback_command failure -> escalate, not silent continue)")


def test_improve_mode() -> None:
    """goal.mode improve: no success gate; budget ends the run; IMPROVED iff
    the best metric beat the baseline, UNRESOLVED otherwise."""
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        _seed_counter(root, win_at=999)                # checker never used
        gain = {
            "goal": {"direction": "increase", "mode": "improve"},
            "maker": {"command": f"{PY} inc.py"},
            "progress": {"command": f"{PY} metric.py"},
            "budget": {"attempts": 2},
            "state": {"file": "state.jsonl"},
        }
        cpath = root / "gain.json"
        cpath.write_text(json.dumps(gain), encoding="utf-8")
        assert run_contract(cpath, root) == "IMPROVED", "metric rose above baseline -> IMPROVED"

        nogain = dict(gain, maker={"command": f'{PY} -c "pass"'})    # changes nothing
        cpath2 = root / "nogain.json"
        cpath2.write_text(json.dumps(nogain), encoding="utf-8")
        assert run_contract(cpath2, root) == "UNRESOLVED", "no metric gain -> UNRESOLVED"
    print("improve ok   (budget-terminated hunt: IMPROVED on gain, UNRESOLVED without)")


def test_protect_tampered() -> None:
    """A maker that edits a protected file gets verdict `tampered` -> escalate,
    regardless of what the checker would say."""
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        (root / "judge.py").write_text("import sys; sys.exit(0)\n", encoding="utf-8")
        tag = "tamper.json"
        contract = {
            "maker": {"command": f"{PY} -c \"open('judge.py','w').write('import sys; sys.exit(0)#x')\""},
            "success": {"command": f"{PY} judge.py"},
            "protect": ["judge.py"],
            "budget": {"attempts": 3},
            "state": {"file": "state.jsonl"},
        }
        cpath = root / tag
        cpath.write_text(json.dumps(contract), encoding="utf-8")
        assert run_contract(cpath, root) == "ESCALATE", "tampering must escalate, not win"
        recs = _records(root / "state.jsonl", tag)
        assert recs[-1]["verdict"] == "tampered" and recs[-1]["tampered"] == ["judge.py"]
    print("protect ok   (maker editing its judge -> tampered -> escalate)")


def test_rollback_restores_incumbent() -> None:
    """snapshot_command checkpoints each new best; a regression's rollback
    restores that incumbent, which lets the next attempt win from it."""
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        _seed_counter(root, win_at=2)
        # maker: first visit at n==1 writes a bad value once, else increments
        (root / "maker.py").write_text(
            "from pathlib import Path\n"
            "n=int(Path('n.txt').read_text()) if Path('n.txt').exists() else 0\n"
            "if n==1 and not Path('did_bad').exists():\n"
            "    Path('did_bad').write_text('x'); Path('n.txt').write_text('-5')\n"
            "else:\n"
            "    Path('n.txt').write_text(str(n+1))\n", encoding="utf-8")
        (root / "snap.py").write_text(
            "from pathlib import Path\n"
            "Path('best.txt').write_text(Path('n.txt').read_text() if Path('n.txt').exists() else '0')\n",
            encoding="utf-8")
        (root / "restore.py").write_text(
            "from pathlib import Path\n"
            "Path('n.txt').write_text(Path('best.txt').read_text())\n", encoding="utf-8")
        tag = "roll.json"
        contract = {
            "goal": {"direction": "increase"},
            "maker": {"command": f"{PY} maker.py"},
            "success": {"command": f"{PY} check.py"},
            "progress": {"command": f"{PY} metric.py"},
            "budget": {"attempts": 4},
            "state": {"file": "state.jsonl"},
            "failure_policy": {"regression": "rollback",
                               "snapshot_command": f"{PY} snap.py",
                               "rollback_command": f"{PY} restore.py"},
        }
        cpath = root / tag
        cpath.write_text(json.dumps(contract), encoding="utf-8")
        assert run_contract(cpath, root) == "WON", "restore-to-incumbent must enable the win"
        verdicts = [r["verdict"] for r in _records(root / "state.jsonl", tag)]
        assert verdicts == ["baseline", "progress", "regression", "won"], verdicts
    print("rollback ok  (snapshot on new best; regression restores incumbent; then WON)")


def test_snapshot_failure_is_not_an_incumbent() -> None:
    """A failed candidate checkpoint escalates without changing best state."""
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        _seed_counter(root, win_at=999)
        (root / "snap.py").write_text(
            "from pathlib import Path\n"
            "p=Path('snap_count'); n=int(p.read_text()) if p.exists() else 0\n"
            "p.write_text(str(n+1))\n"
            "if n >= 1: raise SystemExit(1)\n", encoding="utf-8")
        contract = {
            "goal": {"direction": "increase"},
            "maker": {"command": f"{PY} inc.py"},
            "success": {"command": f'{PY} -c "import sys; sys.exit(1)"'},
            "progress": {"command": f"{PY} metric.py"},
            "budget": {"attempts": 2},
            "state": {"file": "state.jsonl"},
            "failure_policy": {"regression": "rollback",
                               "snapshot_command": f"{PY} snap.py",
                               "rollback_command": f"{PY} -c \"pass\""},
        }
        cpath = root / "snapfail.json"
        cpath.write_text(json.dumps(contract), encoding="utf-8")
        assert run_contract(cpath, root) == "ESCALATE"
        records = _records(root / "state.jsonl", cpath.name)
        failed = records[-1]
        assert failed["snapshot_status"] == "failed"
        assert failed["incumbent"] is False
        assert failed["verdict"] == "unknown"
        assert failed["best"] == 0.0
    print("snapshot failure ok (candidate not admitted; escalates)")


def test_terminal_resume_is_sticky() -> None:
    """An escalated unknown is durable and never reruns the maker on resume."""
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        (root / "maker.py").write_text(
            "from pathlib import Path\n"
            "p=Path('maker_runs'); p.write_text(str(int(p.read_text()) + 1) if p.exists() else '1')\n",
            encoding="utf-8")
        # A progress command IS configured but yields no number -> genuine
        # ambiguity -> unknown -> escalate (the terminal state under test). A
        # success-only contract with no progress command would instead retry.
        (root / "noise.py").write_text("print('not-a-number')", encoding="utf-8")
        contract = {
            "maker": {"command": f"{PY} maker.py"},
            "success": {"command": f'{PY} -c "import sys; sys.exit(1)"'},
            "progress": {"command": f"{PY} noise.py"},
            "budget": {"attempts": 2},
            "state": {"file": "state.jsonl"},
        }
        cpath = root / "terminal.json"
        cpath.write_text(json.dumps(contract), encoding="utf-8")
        assert run_contract(cpath, root) == "ESCALATE"
        assert (root / "maker_runs").read_text() == "1"
        assert run_contract(cpath, root) == "ESCALATE"
        assert (root / "maker_runs").read_text() == "1"
        terminal_records = [r for r in _records(root / "state.jsonl", cpath.name)
                            if r.get("terminal_result")]
        assert terminal_records[-1]["terminal_reason"] == "unknown"
    print("terminal ok   (unknown escalation is sticky across resume)")


def test_improve_restores_committed_incumbent() -> None:
    """IMPROVED returns with the last committed snapshot restored."""
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        (root / "maker.py").write_text(
            "from pathlib import Path\n"
            "calls=Path('calls'); n=int(calls.read_text()) if calls.exists() else 0\n"
            "calls.write_text(str(n+1)); Path('value').write_text('1' if n == 0 else '0')\n",
            encoding="utf-8")
        (root / "metric.py").write_text(
            "from pathlib import Path\n"
            "print(Path('value').read_text() if Path('value').exists() else '0')\n",
            encoding="utf-8")
        (root / "value").write_text("0", encoding="utf-8")
        (root / "snapshot.py").write_text(
            "from pathlib import Path\n"
            "Path('incumbent').write_text(Path('value').read_text())\n",
            encoding="utf-8")
        (root / "restore.py").write_text(
            "from pathlib import Path\n"
            "Path('value').write_text(Path('incumbent').read_text())\n",
            encoding="utf-8")
        contract = {
            "goal": {"mode": "improve", "direction": "increase"},
            "maker": {"command": f"{PY} maker.py"},
            "progress": {"command": f"{PY} metric.py"},
            "budget": {"attempts": 2},
            "state": {"file": "state.jsonl"},
            "failure_policy": {
                "regression": "continue",
                "snapshot_command": f"{PY} snapshot.py",
                "rollback_command": f"{PY} restore.py",
            },
        }
        cpath = root / "improve.json"
        cpath.write_text(json.dumps(contract), encoding="utf-8")
        assert run_contract(cpath, root) == "IMPROVED"
        assert (root / "value").read_text() == "1", \
            "budget termination must restore the committed best snapshot"
    print("improve restore ok (budget result leaves committed incumbent active)")


def test_resume_identity_and_torn_tail() -> None:
    """Contract edits are fresh runs; one torn final JSONL line is truncated."""
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        state = root / "state.jsonl"
        contract = {
            "success": {"command": f'{PY} -c "import sys; sys.exit(0)"'},
            "budget": {"attempts": 1},
            "state": {"file": "state.jsonl"},
            "failure_policy": {"unknown": "continue"},
        }
        cpath = root / "identity.json"
        cpath.write_text(json.dumps(contract), encoding="utf-8")
        assert run_contract(cpath, root) == "WON"
        first_size = state.stat().st_size
        with state.open("ab") as handle:
            handle.write(b'{"torn":')
        # Change the evaluator: old WON must not short-circuit this run.
        contract["success"] = {"command": f'{PY} -c "import sys; sys.exit(1)"'}
        cpath.write_text(json.dumps(contract), encoding="utf-8")
        assert run_contract(cpath, root) == "UNRESOLVED"
        assert state.stat().st_size > first_size
        assert b'{"torn":' not in state.read_bytes()
    print("identity/torn-tail ok (stale WON ignored; tail truncated and resumed)")


def test_malformed_earlier_ledger_refuses() -> None:
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        cpath = root / "bad.json"
        contract = {"success": {"command": "true"}, "budget": {"attempts": 1},
                    "state": {"file": "state.jsonl"}}
        cpath.write_text(json.dumps(contract), encoding="utf-8")
        state = root / "state.jsonl"
        state.write_text(json.dumps(_ledger_header()) + "\n"
                         '{"broken":\n{"valid": true}\n', encoding="utf-8")
        try:
            run_contract(cpath, root)
        except SystemExit as exc:
            assert "malformed earlier ledger" in str(exc)
        else:
            raise AssertionError("malformed earlier ledger line must refuse")
    print("malformed ledger ok (earlier corruption refuses explicitly)")


def test_validator_contract_shape_and_defaults() -> None:
    """The shared validator covers the strict canonical contract shape."""
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        protected = root / "judge.py"
        protected.write_text("pass\n", encoding="utf-8")
        from run import validate_contract
        contract = {
            "goal": {"direction": "sideways"},
            "success": {"command": 3, "timeout_sec": float("inf")},
            "progress": {"command": "metric", "stagnation_window": 0},
            "budget": {"attempts": True},
            "protect": ["judge.py", "missing.py"],
            "failure_policy": {"regression": "ask", "snapshot_command": "snap",
                                "unknwon": "continue"},
            "unexpected": True,
        }
        errors = validate_contract(contract, root)
        assert any("direction" in error for error in errors)
        assert any("success.command" in error for error in errors)
        assert any("timeout_sec" in error for error in errors)
        assert any("stagnation_window" in error for error in errors)
        assert any("attempts" in error for error in errors)
        assert any("paired" in error for error in errors)
        assert any("protect" in error for error in errors)
        assert any("unknwon" in error for error in errors)
        assert any("unexpected" in error for error in errors)

        owned_path = root / "state.jsonl"
        owned_path.write_text("{}\n", encoding="utf-8")
        owned_contract = {"success": {"command": "check"},
                          "budget": {"attempts": 1},
                          "state": {"file": "state.jsonl"}}
        assert any("owned autohunt ledger" in error
                   for error in validate_contract(owned_contract, root))
    print("validator ok (canonical shape/path/action/timeout checks)")


# ---- refusal gates ---------------------------------------------------------

def _refuses(contract: dict) -> bool:
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        cpath = root / "c.json"
        cpath.write_text(json.dumps(contract), encoding="utf-8")
        try:
            run_contract(cpath, root)
            return False
        except SystemExit:
            return True


def test_gates() -> None:
    assert _refuses({"budget": {"attempts": 3}}), "must refuse a contract with no success.command"
    assert _refuses({"success": {"command": "true"}, "budget": {"attempts": 0}}), \
        "must refuse budget.attempts < 1 (the kill switch)"
    assert _refuses({"goal": {"mode": "improve"}, "budget": {"attempts": 3}}), \
        "must refuse improve mode without progress.command (no judge at all)"
    assert _refuses({"goal": {"mode": "vibe"}, "success": {"command": "true"},
                     "budget": {"attempts": 1}}), "must refuse an unknown goal.mode"
    print("gates ok     (refuses: no success.command, cap<1, improve w/o progress, bad mode)")


def main() -> int:
    test_classify()
    test_numeric_rejects_nonfinite()
    test_success_only_retries_to_won()
    test_success_only_metricless_never_snapshots()
    test_checker_only_gate_escalates()
    test_resume_continues()
    test_resume_respects_cap()
    test_resume_won_short_circuits()
    test_baseline_first()
    test_timeout_kills_hung_maker()
    test_hung_checker_is_unknown()
    test_improve_legacy_baseline()
    test_resume_drops_unscored_metric()
    test_baseline_snapshot_restores_pristine()
    test_rollback_failure_escalates()
    test_improve_mode()
    test_protect_tampered()
    test_rollback_restores_incumbent()
    test_snapshot_failure_is_not_an_incumbent()
    test_terminal_resume_is_sticky()
    test_improve_restores_committed_incumbent()
    test_resume_identity_and_torn_tail()
    test_malformed_earlier_ledger_refuses()
    test_validator_contract_shape_and_defaults()
    test_gates()
    print("test_governor ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
