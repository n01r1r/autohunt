#!/usr/bin/env python3
"""Agent-agnostic proof + multi-agent smoke test.

Part A (default, no agent, deterministic, free, runs in CI):
    A1  run.py drives an ARBITRARY maker command to WON -> the runtime needs no
        specific agent.
    A2  agent.py maps any backend NAME to the right command -> the dispatch
        layer is the only agent-aware seam, and it is generic.

Part B (--live, opportunistic, costs tokens):
    For each of claude/gemini/codex/aider/llm found on PATH, run one trivial
    contract that asks the agent to create a file, and check it appeared.
    Absent agents are skipped and reported. Off by default so nobody burns
    tokens by accident.

Usage:
    python test_agnostic.py            # Part A only
    python test_agnostic.py --live     # Part A + Part B
"""
from __future__ import annotations
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from run import run_contract
from agent import AGENTS, build

ROOT = Path(__file__).resolve().parent
RUN_PY = ROOT / "run.py"
AGENT_PY = ROOT / "agent.py"
PY = sys.executable


def _seed(root: Path) -> None:
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


def part_a() -> None:
    # A1: the loop reaches WON with a plain command as the "agent" backend.
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        _seed(root)
        contract = {
            "goal": {"direction": "increase"},
            "maker": {"command": f'"{PY}" inc.py'},
            "success": {"command": f'"{PY}" check.py'},
            "progress": {"command": f'"{PY}" metric.py'},
            "budget": {"attempts": 5},
        }
        cpath = root / "agnostic.json"
        cpath.write_text(json.dumps(contract), encoding="utf-8")
        assert run_contract(cpath, root) == "WON", "runtime failed to drive an arbitrary backend to WON"
    print("part A1 ok  (runtime drives an arbitrary maker -> agent-agnostic)")

    # A2: agent.py maps any backend name to a command; nothing else is agent-aware.
    assert build("claude", "hi") == ["claude", "-p", "hi"]
    assert build("gemini", "hi") == ["gemini", "-p", "hi"]
    assert build("codex", "hi") == ["codex", "exec", "--skip-git-repo-check", "hi"]
    assert build("mycli --flag", "hi") == ["mycli", "--flag", "hi"]   # raw command prefix
    assert build('"C:\\my tools\\cli.exe" --flag', "hi") == \
        ["C:\\my tools\\cli.exe", "--flag", "hi"]   # quoted path with spaces (Windows)
    print("part A2 ok  (agent.py dispatch is generic across backends)")


def part_b_one(name: str) -> str:
    """Run one trivial real contract against agent `name`. Returns a status word."""
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        contract = {
            "maker": {"command": f'"{PY}" "{AGENT_PY}" '
                      f'"create an empty file named AGENT_OK in this directory"'},
            "success": {"command": f'"{PY}" -c "import os,sys;'
                        f'sys.exit(0 if os.path.exists(\'AGENT_OK\') else 1)"'},
            "budget": {"attempts": 2},
        }
        cpath = root / "smoke.json"
        cpath.write_text(json.dumps(contract), encoding="utf-8")
        env = os.environ.copy()
        env["HARNESS_AGENT"] = name
        try:
            r = subprocess.run(
                [PY, str(RUN_PY), str(cpath), "--cwd", str(root)],
                env=env, capture_output=True, text=True,
                encoding="utf-8", errors="replace", timeout=180)
        except subprocess.TimeoutExpired:
            return "TIMEOUT"
        return "WON" if "RESULT: WON" in (r.stdout or "") else "FAIL"


def part_b() -> None:
    # Version axis: HARNESS_SMOKE_EXTRA adds raw backend prefixes, one per
    # comma, e.g. "npx -y @openai/codex@2.1.0 exec --skip-git-repo-check".
    # agent.py runs an unknown name verbatim as a command prefix, so any
    # pinned CLI version is testable without code changes. Backends are
    # independent (one maker per contract) - a list, never a cross product.
    extra = [s.strip() for s in
             os.environ.get("HARNESS_SMOKE_EXTRA", "").split(",") if s.strip()]
    ran = False
    for name in [*AGENTS, *extra]:          # single source of truth: agent.py
        if not shutil.which(name.split()[0]):
            print(f"part B  {name:8} SKIP (not on PATH)")
            continue
        ran = True
        print(f"part B  {name:8} {part_b_one(name)}")
    if not ran:
        print("part B  no agent CLI on PATH; nothing exercised")


def main() -> int:
    part_a()
    if "--live" in sys.argv[1:]:
        part_b()
    else:
        print("part B skipped (pass --live to smoke-test real agents; costs tokens)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
