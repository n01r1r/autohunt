#!/usr/bin/env python3
"""One-touch harness setup: scaffold files, detect an agent CLI, seed config.

Autonomous by design: after this, `python run.py <contract>` hunts to the
budget cap on its own and only escalates to you on an `unknown` verdict.

Usage:  python setup.py [target_dir]
"""
from __future__ import annotations
import shutil
import subprocess
import sys
from pathlib import Path

from scaffold import scaffold, FILES
from agent import AGENTS


def detect() -> str | None:
    for name in AGENTS:
        if shutil.which(name):
            return name
    return None


def behind(root: Path) -> int | None:
    """Commits behind upstream, if these scripts live in a git clone.

    None = unknown (not a clone, no upstream, offline) - stay silent then.
    Advisory only, at setup time; the governor never checks or updates itself
    mid-run (network dependence + code swap would break resume determinism).
    """
    try:
        fetched = subprocess.run(["git", "fetch", "-q"], cwd=root, timeout=15,
                                 capture_output=True)
        if fetched.returncode != 0:      # offline / no remote: a count against
            return None                  # stale refs would mislead - stay silent
        out = subprocess.run(["git", "rev-list", "--count", "HEAD..@{u}"],
                             cwd=root, timeout=15, capture_output=True, text=True)
        return int(out.stdout.strip()) if out.returncode == 0 else None
    except Exception:
        return None


def active_agent(env_file: Path) -> str | None:
    """The uncommented HARNESS_AGENT= line in agent.env, if any."""
    if not env_file.exists():
        return None
    for line in env_file.read_text(encoding="utf-8").splitlines():
        s = line.strip()
        if s.startswith("HARNESS_AGENT="):
            return s.split("=", 1)[1].strip()
    return None


def main() -> int:
    target = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else Path.cwd()
    created, skipped = scaffold(target)
    print(f"harness -> {target}")
    for f in created:
        print(f"  created  {f}")
    for f in skipped:
        print(f"  skipped  {f} (exists)")

    agent = detect()
    env_file = target / "harness/routing/agent.env"
    if agent and active_agent(env_file):
        print(f"  detected agent: {agent}  (agent.env already set; left as-is)")
    elif agent:
        with env_file.open("a", encoding="utf-8") as f:
            f.write(f"HARNESS_AGENT={agent}\n")
        print(f"  detected agent: {agent}  (wrote harness/routing/agent.env)")
    else:
        print("  no known agent CLI on PATH; set HARNESS_AGENT before a real maker runs")

    root = Path(__file__).resolve().parent
    n = behind(root)
    if n:
        print(f"  note: harness scripts are {n} commit(s) behind upstream - "
              f"update with: git -C \"{root}\" pull --ff-only")

    print("\nnext:")
    print("  1. edit harness/state/goal.yaml         (the invariant)")
    print("  2. edit harness/contracts/example.contract.yaml  (maker/success/progress)")
    print("  3. python run.py harness/contracts/example.contract.yaml")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
