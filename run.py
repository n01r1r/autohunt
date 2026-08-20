#!/usr/bin/env python3
"""Generic contract runtime - the deterministic governor / checker.

ONE runtime for all contracts. It never calls a model. It runs the maker
(a separate subprocess with its own context), checks the result with a
deterministic command, classifies progress, enforces the budget, and writes
durable state so a crashed run resumes.

Autonomous by design: it drives attempts to the budget cap on its own and only
hands control back to a human on an `unknown` verdict (see failure_policy).
That is the whole point - unattended progress, human touched only when the
signal is ambiguous.

Cost/reliability moves that are actually code:
    #2 kill switch   -> budget.attempts is a hard cap; loop cannot run forever
    #7 maker<>checker -> this process is ONLY the checker/governor; the maker is
                         a separate command, so no agent grades its own homework
    #3 durable state -> every attempt appended to state file; resume on restart
    #1 checkable gate -> refuses a contract with no success.command ("it's a vibe")

Moves #4/#5/#6 (MCP audit, cache prefix order, effort dial) are host habits,
not code - see principles/operating-policy.md.

Usage:
    python run.py <contract.yaml> [--cwd DIR]
    python run.py --selftest

Note: success/maker/progress/rollback commands come from the local contract file
(trusted config, like a Makefile) and run via the shell.
"""
from __future__ import annotations
import json
import subprocess
import sys
from pathlib import Path


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


def sh(cmd: str, cwd: Path) -> int:
    """Run a shell command, stream nothing, return exit code."""
    return subprocess.run(cmd, shell=True, cwd=str(cwd)).returncode


def numeric(cmd: str | None, cwd: Path) -> float | None:
    if not cmd:
        return None
    out = subprocess.run(cmd, shell=True, cwd=str(cwd),
                         capture_output=True, text=True,
                         encoding="utf-8", errors="replace")
    for line in reversed(out.stdout.strip().splitlines()):
        try:
            return float(line.strip())
        except ValueError:
            continue
    return None


def classify(passed: bool, metric, history: list, direction: str, window: int) -> str:
    if passed:
        return "won"
    if metric is None:
        return "unknown"
    prev = [h for h in history if h is not None]
    if not prev:
        return "progress"
    better = (metric < prev[-1]) if direction == "decrease" else (metric > prev[-1])
    worse = (metric > prev[-1]) if direction == "decrease" else (metric < prev[-1])
    if worse:
        return "regression"
    if better:
        return "progress"
    # equal: stagnation if the last `window` values showed no improvement
    recent = prev[-(window - 1):] + [metric] if window > 1 else [metric]
    return "stagnation" if len(recent) >= window and len(set(recent)) == 1 else "unknown"


def run_contract(contract_path: Path, cwd: Path) -> str:
    c = load_contract(contract_path)
    success_cmd = (c.get("success") or {}).get("command", "").strip()
    if not success_cmd:                                    # move #1 gate
        sys.exit("REFUSED: no success.command. Not a task, a vibe - "
                 "write a checkable 'done and correct looks like ___' first.")
    cap = int((c.get("budget") or {}).get("attempts", 0))
    if cap < 1:                                            # move #2 kill switch
        sys.exit("REFUSED: budget.attempts must be >= 1 (the kill switch).")

    maker_cmd = (c.get("maker") or {}).get("command")
    prog = c.get("progress") or {}
    prog_cmd = prog.get("command")
    window = int(prog.get("stagnation_window", 2))
    if window < 1:
        sys.exit("REFUSED: progress.stagnation_window must be >= 1.")
    direction = (c.get("goal") or {}).get("direction", "decrease")
    if direction not in ("increase", "decrease"):
        sys.exit(f"REFUSED: goal.direction must be 'increase' or 'decrease', got {direction!r}.")
    fp = c.get("failure_policy") or {}

    state_rel = (c.get("state") or {}).get("file", "harness/state/decisions.jsonl")
    state_file = cwd / state_rel
    state_file.parent.mkdir(parents=True, exist_ok=True)
    tag = contract_path.name

    # resume (move #3): read prior attempts for this contract
    history: list = []
    prior = 0
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
            prior += 1
            history.append(r.get("metric"))

    for i in range(prior, cap):
        attempt = i + 1
        if maker_cmd:                                      # move #7: maker is separate
            sh(maker_cmd, cwd)
        passed = sh(success_cmd, cwd) == 0
        metric = numeric(prog_cmd, cwd)
        verdict = classify(passed, metric, history, direction, window)
        action = "stop" if verdict == "won" else fp.get(verdict, "continue")

        rec = {"contract": tag, "attempt": attempt, "verdict": verdict,
               "metric": metric, "action": action}
        with state_file.open("a", encoding="utf-8") as f:  # durable, one line/attempt
            f.write(json.dumps(rec) + "\n")
        print(f"[{attempt}/{cap}] verdict={verdict} metric={metric} -> {action}")
        history.append(metric)

        if verdict == "won":
            return "WON"
        if action == "rollback":
            rb = fp.get("rollback_command")
            if rb:
                sh(rb, cwd)
        elif action == "escalate":
            return "ESCALATE"
        # continue / replan: next attempt; maker reads updated state and replans

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
    return 0 if result == "WON" else 1


if __name__ == "__main__":
    raise SystemExit(main())
