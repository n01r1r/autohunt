#!/usr/bin/env python3
"""Governor guarantees the --selftest does not cover, especially RESUME.

run.py's --selftest proves win + kill-switch. It never crashes mid-run and
restarts, so resume (move #3, durable state) is unverified there. This file
re-verifies resume plus the pure logic (classify) and the refusal gates.

A crash is simulated the honest way: pre-seed the state file with the records a
dying process would have flushed (one line per attempt is durable), then call
run_contract and prove it picks up from prior count - not from attempt 1.

Usage:
    python test_governor.py
"""
from __future__ import annotations
import json
import tempfile
from pathlib import Path
import sys

from run import run_contract, classify

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
    return [json.loads(l) for l in state_file.read_text(encoding="utf-8").splitlines()
            if l.strip() and json.loads(l).get("contract") == tag]


def _write_state(state_file: Path, recs: list) -> None:
    state_file.parent.mkdir(parents=True, exist_ok=True)
    state_file.write_text("".join(json.dumps(r) + "\n" for r in recs), encoding="utf-8")


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
    # equal: stagnation once the window of equal values is filled, else unknown
    assert classify(False, 5, [5], "increase", 2) == "stagnation"
    assert classify(False, 5, [5], "increase", 3) == "unknown"       # window not yet full
    assert classify(False, 5, [5, 5], "increase", 3) == "stagnation"
    print("classify ok  (won/progress/regression/stagnation/unknown)")


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
        _write_state(state, [                                         # what the dead process flushed
            {"contract": tag, "attempt": 1, "verdict": "progress", "metric": 1.0, "action": "continue"},
            {"contract": tag, "attempt": 2, "verdict": "progress", "metric": 2.0, "action": "continue"},
        ])
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
        _write_state(state, [
            {"contract": tag, "attempt": 1, "verdict": "unknown", "metric": None, "action": "continue"},
            {"contract": tag, "attempt": 2, "verdict": "unknown", "metric": None, "action": "continue"},
        ])
        contract = {
            "success": {"command": f'{PY} -c "import sys; sys.exit(1)"'},
            "budget": {"attempts": 3},
            "state": {"file": "state.jsonl"},
        }
        cpath = root / tag
        cpath.write_text(json.dumps(contract), encoding="utf-8")

        assert run_contract(cpath, root) == "UNRESOLVED"
        assert len(_records(state, tag)) == 3, "cap=3 must hold across the resume (2 prior + 1 new)"
    print("resume ok    (cap held across restart: 2 prior + 1 = 3, then UNRESOLVED)")


def test_resume_won_short_circuits() -> None:
    """A prior WON record ends the run before the maker can run again."""
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        tag = "done.json"
        state = root / "state.jsonl"
        _write_state(state, [
            {"contract": tag, "attempt": 1, "verdict": "won", "metric": None, "action": "stop"},
        ])
        contract = {
            "maker": {"command": f"{PY} -c \"open('SENTINEL','w').close()\""},
            "success": {"command": f'{PY} -c "import sys; sys.exit(1)"'},
            "budget": {"attempts": 3},
            "state": {"file": "state.jsonl"},
        }
        cpath = root / tag
        cpath.write_text(json.dumps(contract), encoding="utf-8")

        assert run_contract(cpath, root) == "WON", "prior WON must short-circuit"
        assert not (root / "SENTINEL").exists(), "maker must not run once a prior WON is seen"
    print("resume ok    (prior WON short-circuits; maker never re-runs)")


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
    print("gates ok     (refuses no success.command and cap<1)")


def main() -> int:
    test_classify()
    test_resume_continues()
    test_resume_respects_cap()
    test_resume_won_short_circuits()
    test_gates()
    print("test_governor ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
