#!/usr/bin/env python3
"""Generic contract runtime - the deterministic governor / checker.

ONE runtime for all contracts. It never calls a model. It runs the maker
(a separate subprocess with its own context), checks the result with a
deterministic command, classifies progress, enforces the budget, and writes
durable state so a crashed run resumes.

Autonomous by design: it drives attempts to the budget cap on its own and only
hands control back to a human on an `unknown` or `tampered` verdict (see
failure_policy). That is the whole point - unattended progress, human touched
only when the signal is ambiguous or the checker's integrity is broken.

Cost/reliability moves that are actually code:
    #2 kill switch   -> budget.attempts is a hard cap; loop cannot run forever
    #7 maker<>checker -> this process is ONLY the checker/governor; the maker is
                         a separate command, so no agent grades its own homework
    #3 durable state -> every attempt appended to state file; resume on restart
    #1 checkable gate -> refuses a contract with no success.command ("it's a vibe")
                         unless goal.mode is `improve` (budget-terminated hunt)
    timeouts         -> every command class (maker/success/progress/rollback)
                        may carry timeout_sec; a hung process is tree-killed
                        (exit 124) instead of blocking an unattended run
    baseline first   -> with a progress command, the pristine metric is measured
                        BEFORE the first maker run (attempt 0) so attempt 1 has
                        a real comparison and best-state tracking is sound
    incumbent best   -> classification compares against the best metric seen,
                        not the previous one; snapshot_command checkpoints each
                        new best, rollback_command restores it
    protect          -> listed files are hashed around the maker; any change is
                        `tampered` (the maker may not edit its own judge)

Moves #4/#5/#6 (MCP audit, cache prefix order, effort dial) are host habits,
not code - see principles/operating-policy.md.

Usage:
    python run.py <contract.yaml> [--cwd DIR]
    python run.py --selftest

Note: success/maker/progress/rollback commands come from the local contract file
(trusted config, like a Makefile) and run via the shell.
"""
from __future__ import annotations
import hashlib
import json
import math
import subprocess
import sys
import time
from pathlib import Path

TIMEOUT_EXIT = 124   # GNU timeout convention


def load_contract(p: Path) -> dict:
    text = p.read_text(encoding="utf-8")
    if p.suffix.lower() == ".json":
        return json.loads(text)
    try:
        import yaml
    except ImportError:
        sys.exit("contract is YAML but pyyaml is missing: `pip install pyyaml` "
                 "(or write the contract as .json)")
    return yaml.safe_load(text)


def _kill_tree(p: subprocess.Popen) -> None:
    """Kill the whole process tree, not just the shell. Best effort: if the
    tree kill is denied (restricted sandboxes), fall back to CTRL_BREAK on the
    process group (Windows) and then the direct child, so a timeout never
    leaves the governor blocked.
    # ponytail: without psutil/Job Objects a grandchild can survive a denied
    # taskkill; the governor's own wait stays bounded either way."""
    killed = False
    if sys.platform == "win32":
        try:
            r = subprocess.run(["taskkill", "/F", "/T", "/PID", str(p.pid)],
                               capture_output=True, timeout=10)
            killed = r.returncode == 0
        except (subprocess.TimeoutExpired, OSError):
            pass
        if not killed:
            import signal
            try:   # child was started in its own group (CREATE_NEW_PROCESS_GROUP)
                p.send_signal(signal.CTRL_BREAK_EVENT)
            except (OSError, ValueError):
                pass
    else:
        import os
        import signal
        try:
            os.killpg(p.pid, signal.SIGKILL)
            killed = True
        except (ProcessLookupError, PermissionError, OSError):
            pass
    if not killed:
        try:
            p.kill()
        except OSError:
            pass


def _popen_kw() -> dict:
    if sys.platform == "win32":
        # own process group so CTRL_BREAK can reach the whole tree on timeout
        return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
    return {"start_new_session": True}


def sh(cmd: str, cwd: Path, timeout: float | None = None) -> int:
    """Run a shell command; on timeout tree-kill it and return TIMEOUT_EXIT."""
    p = subprocess.Popen(cmd, shell=True, cwd=str(cwd), **_popen_kw())
    try:
        return p.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        _kill_tree(p)
        try:
            p.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass            # unkillable child: report the timeout, don't block on it
        return TIMEOUT_EXIT


def numeric(cmd: str | None, cwd: Path, timeout: float | None = None) -> float | None:
    if not cmd:
        return None
    p = subprocess.Popen(cmd, shell=True, cwd=str(cwd),
                         stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                         text=True, encoding="utf-8", errors="replace",
                         **_popen_kw())
    try:
        out, _ = p.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        _kill_tree(p)
        try:
            p.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            pass            # unkillable child: an absent metric is the signal
        return None
    for line in reversed((out or "").strip().splitlines()):
        try:
            v = float(line.strip())
        except ValueError:
            continue
        if math.isfinite(v):     # NaN/inf is an invalid signal, not a metric
            return v
    return None


def _best(history: list, direction: str) -> float | None:
    vals = [h for h in history if h is not None]
    if not vals:
        return None
    return min(vals) if direction == "decrease" else max(vals)


def classify(passed: bool, metric, history: list, direction: str, window: int) -> str:
    if passed:
        return "won"
    if metric is None:
        return "unknown"
    best = _best(history, direction)
    if best is None:
        return "progress"
    better = (metric < best) if direction == "decrease" else (metric > best)
    worse = (metric > best) if direction == "decrease" else (metric < best)
    if better:
        return "progress"
    if worse:
        return "regression"
    # equal to the incumbent best: stagnation once the last `window` values are
    # all equal; before that, plateau (deterministic continue, never a human).
    prev = [h for h in history if h is not None]
    recent = prev[-(window - 1):] + [metric] if window > 1 else [metric]
    return "stagnation" if len(recent) >= window and len(set(recent)) == 1 else "plateau"


def _digest(paths: list, cwd: Path) -> dict:
    d = {}
    for rel in paths:
        f = cwd / rel
        d[rel] = hashlib.sha256(f.read_bytes()).hexdigest() if f.is_file() else None
    return d


def _git_head(cwd: Path) -> str | None:
    r = subprocess.run("git rev-parse --short HEAD", shell=True, cwd=str(cwd),
                       capture_output=True, text=True)
    if r.returncode != 0:
        return None
    return r.stdout.strip() or None


def run_contract(contract_path: Path, cwd: Path) -> str:
    c = load_contract(contract_path)
    goal = c.get("goal") or {}
    mode = goal.get("mode", "complete")
    if mode not in ("complete", "improve"):
        sys.exit(f"REFUSED: goal.mode must be 'complete' or 'improve', got {mode!r}.")
    success = c.get("success") or {}
    success_cmd = success.get("command", "").strip()
    if not success_cmd and mode != "improve":       # move #1 gate
        sys.exit("REFUSED: no success.command. Not a task, a vibe - "
                 "write a checkable 'done and correct looks like ___' first "
                 "(or declare goal.mode: improve for a budget-terminated hunt).")
    cap = int((c.get("budget") or {}).get("attempts", 0))
    if cap < 1:                                     # move #2 kill switch
        sys.exit("REFUSED: budget.attempts must be >= 1 (the kill switch).")

    maker = c.get("maker") or {}
    maker_cmd = maker.get("command")
    prog = c.get("progress") or {}
    prog_cmd = prog.get("command")
    if mode == "improve" and not prog_cmd:
        sys.exit("REFUSED: goal.mode improve needs progress.command "
                 "(with no success gate, the metric is the only judge).")
    window = int(prog.get("stagnation_window", 2))
    if window < 1:
        sys.exit("REFUSED: progress.stagnation_window must be >= 1.")
    direction = goal.get("direction", "decrease")
    if direction not in ("increase", "decrease"):
        sys.exit(f"REFUSED: goal.direction must be 'increase' or 'decrease', got {direction!r}.")
    fp = c.get("failure_policy") or {}
    protect = c.get("protect") or []
    mk_t = maker.get("timeout_sec")
    ok_t = success.get("timeout_sec")
    pg_t = prog.get("timeout_sec")
    rb_t = fp.get("rollback_timeout_sec")
    snap_cmd = fp.get("snapshot_command")

    state_rel = (c.get("state") or {}).get("file", "harness/state/decisions.jsonl")
    state_file = cwd / state_rel
    state_file.parent.mkdir(parents=True, exist_ok=True)
    tag = contract_path.name

    def record(rec: dict) -> None:
        with state_file.open("a", encoding="utf-8") as f:  # durable, one line/attempt
            f.write(json.dumps(rec) + "\n")

    # resume (move #3): read prior attempts for this contract
    history: list = []
    prior = 0
    baseline = None
    if state_file.exists():
        for line in state_file.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            r = json.loads(line)
            if r.get("contract") != tag:
                continue
            if r.get("verdict") == "won":
                print(f"already satisfied (prior run). {state_file}")
                return "WON"
            if r.get("verdict") == "baseline":
                baseline = r.get("metric")
                history.append(baseline)
                continue
            prior += 1
            # unscored verdicts (unknown/tampered) never advance best - not on
            # resume either, or a crash after one would pollute the incumbent
            scored = r.get("verdict") not in ("unknown", "tampered")
            history.append(r.get("metric") if scored else None)

    # baseline: pristine metric BEFORE the maker ever runs, once per contract
    if prog_cmd and prior == 0 and baseline is None:
        baseline = numeric(prog_cmd, cwd, pg_t)
        record({"contract": tag, "attempt": 0, "verdict": "baseline",
                "metric": baseline, "action": "none", "commit": _git_head(cwd)})
        print(f"[0/{cap}] baseline metric={baseline}")
        history.append(baseline)
        if snap_cmd and baseline is not None:
            # deliberate: the pristine state IS the initial incumbent - without
            # this checkpoint, a regression on attempt 1 has nothing to restore
            rc = sh(snap_cmd, cwd, rb_t)
            if rc != 0:
                print(f"warning: snapshot_command failed (exit {rc}); baseline is not checkpointed")

    for i in range(prior, cap):
        attempt = i + 1
        t0 = time.monotonic()
        pre = _digest(protect, cwd) if protect else {}
        maker_exit = None
        if maker_cmd:                               # move #7: maker is separate
            maker_exit = sh(maker_cmd, cwd, mk_t)
        tampered = [k for k, v in pre.items() if _digest([k], cwd)[k] != v]

        note_file = state_file.parent / "attempt_note.txt"
        note = None
        if note_file.exists():
            note = note_file.read_text(encoding="utf-8").strip() or None
            note_file.unlink()

        success_exit = None
        if tampered:                                # the maker edited its judge
            passed, metric, verdict = False, None, "tampered"
        else:
            if success_cmd:
                success_exit = sh(success_cmd, cwd, ok_t)
            passed = success_exit == 0
            metric = numeric(prog_cmd, cwd, pg_t)
            if success_exit == TIMEOUT_EXIT:        # hung checker = invalid signal,
                verdict = "unknown"                 # never scored as progress
            else:
                verdict = classify(passed, metric, history, direction, window)
        default_action = "escalate" if verdict == "tampered" else "continue"
        action = "stop" if verdict == "won" else fp.get(verdict, default_action)

        scored = verdict not in ("unknown", "tampered")
        history.append(metric if scored else None)  # untrusted signal never advances best
        best = _best(history, direction)
        rec = {"contract": tag, "attempt": attempt, "verdict": verdict,
               "metric": metric, "best": best, "action": action,
               "maker_exit": maker_exit, "success_exit": success_exit,
               "duration_sec": round(time.monotonic() - t0, 1),
               "commit": _git_head(cwd)}
        if note:
            rec["note"] = note
        if tampered:
            rec["tampered"] = tampered
        record(rec)
        print(f"[{attempt}/{cap}] verdict={verdict} metric={metric} best={best} -> {action}")

        if verdict == "won":
            return "WON"
        if verdict == "progress" and snap_cmd:      # progress = new incumbent by construction
            rc = sh(snap_cmd, cwd, rb_t)
            if rc != 0:
                print(f"warning: snapshot_command failed (exit {rc}); this best is not checkpointed")
        if action == "rollback":
            rb = fp.get("rollback_command")
            if rb:
                rc = sh(rb, cwd, rb_t)              # restore the incumbent best
                if rc != 0:                         # unrestored regression = ambiguous state
                    print(f"rollback_command failed (exit {rc}) - cannot restore incumbent; escalating.")
                    return "ESCALATE"
        elif action == "escalate":
            return "ESCALATE"
        # continue / replan: next attempt; maker reads updated state and replans

    if mode == "improve":
        base = baseline
        if base is None:    # legacy ledger without an attempt-0 baseline record:
                            # earliest recorded metric is the best approximation
            base = next((h for h in history if h is not None), None)
        b = _best(history, direction)
        if (base is not None and b is not None and
                ((b < base) if direction == "decrease" else (b > base))):
            print(f"budget exhausted - IMPROVED (baseline {base} -> best {b}).")
            return "IMPROVED"
    print("budget exhausted - UNRESOLVED (kill switch held).")
    return "UNRESOLVED"


def _selftest() -> None:
    import tempfile
    exe = f'"{sys.executable}"'
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        (root / "inc.py").write_text(
            "from pathlib import Path\n"
            "f=Path('n.txt'); n=int(f.read_text()) if f.exists() else 0\n"
            "f.write_text(str(n+1))\n", encoding="utf-8")
        (root / "check.py").write_text(
            "import sys; from pathlib import Path\n"
            "n=int(Path('n.txt').read_text()) if Path('n.txt').exists() else 0\n"
            "sys.exit(0 if n>=2 else 1)\n", encoding="utf-8")
        (root / "metric.py").write_text(
            "from pathlib import Path\n"
            "print(Path('n.txt').read_text() if Path('n.txt').exists() else '0')\n",
            encoding="utf-8")
        win = {"goal": {"direction": "increase"},
               "maker": {"command": f"{exe} inc.py"},
               "success": {"command": f"{exe} check.py"},
               "progress": {"command": f"{exe} metric.py"},
               "budget": {"attempts": 5}}
        (root / "win.json").write_text(json.dumps(win), encoding="utf-8")
        assert run_contract(root / "win.json", root) == "WON", "winnable should win"

        # unwinnable: no maker, checker always fails -> must stop at cap (kill switch)
        lose = {"success": {"command": f"{exe} -c \"import sys; sys.exit(1)\""},
                "budget": {"attempts": 3}}
        (root / "lose.json").write_text(json.dumps(lose), encoding="utf-8")
        assert run_contract(root / "lose.json", root) == "UNRESOLVED", "must stop at cap"
        recs = [json.loads(l) for l in (root / "harness/state/decisions.jsonl")
                .read_text(encoding="utf-8").splitlines() if l.strip()]
        assert sum(r["contract"] == "lose.json" for r in recs) == 3, "cap must bound attempts"
    print("selftest ok")


def main() -> int:
    args = sys.argv[1:]
    if args and args[0] == "--selftest":
        _selftest()
        return 0
    if not args:
        print(__doc__)
        return 2
    cwd = Path.cwd()
    if "--cwd" in args:
        cwd = Path(args[args.index("--cwd") + 1]).resolve()
    result = run_contract(Path(args[0]).resolve(), cwd)
    print(f"RESULT: {result}")
    return 0 if result in ("WON", "IMPROVED") else 1


if __name__ == "__main__":
    raise SystemExit(main())
