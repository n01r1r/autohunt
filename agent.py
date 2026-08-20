#!/usr/bin/env python3
"""Agent-agnostic maker dispatch. One prompt in, some agent runs it headless.

This is the ONLY file that knows a specific agent CLI exists. Everything else
in the harness is model/host-neutral. Swap the backend without touching a
contract: set HARNESS_AGENT, or write harness/routing/agent.env.

Backend selection order:
    1. $HARNESS_AGENT              (e.g. "claude", "gemini", or a raw command)
    2. harness/routing/agent.env   line "HARNESS_AGENT=<name>"
    3. first known CLI found on PATH

A value that is not a known name is run verbatim as a command prefix, so
HARNESS_AGENT="mycli --flag" works too.

Usage:  python agent.py "<prompt>"     (or pipe the prompt on stdin)
"""
from __future__ import annotations
import os
import shutil
import subprocess
import sys
from pathlib import Path

# name -> argv builder for "run this one prompt, non-interactive, then exit".
# Add a backend here; nothing else in the harness changes.
AGENTS = {
    "claude": lambda p: ["claude", "-p", p],
    "gemini": lambda p: ["gemini", "-p", p],
    "codex":  lambda p: ["codex", "exec", "--skip-git-repo-check", p],
    "aider":  lambda p: ["aider", "--message", p, "--yes"],
    "llm":    lambda p: ["llm", p],                 # simonw/llm
}


def pick() -> str:
    want = os.environ.get("HARNESS_AGENT", "").strip()
    if not want:
        envf = Path("harness/routing/agent.env")
        if envf.exists():
            for line in envf.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line.startswith("HARNESS_AGENT="):
                    want = line.split("=", 1)[1].strip()
                    break
    if want:
        return want
    for name in AGENTS:
        if shutil.which(name):
            return name
    sys.exit("no agent backend: set HARNESS_AGENT (or agent.env), "
             "or install one of: " + ", ".join(AGENTS))


def build(name: str, prompt: str) -> list[str]:
    if name in AGENTS:
        return AGENTS[name](prompt)
    return [*name.split(), prompt]          # raw command prefix


def main() -> int:
    prompt = " ".join(sys.argv[1:]).strip() or sys.stdin.read().strip()
    if not prompt:
        sys.exit("no prompt given")
    name = pick()
    argv = build(name, prompt)
    # Windows: agent CLIs are usually .cmd/.bat shims (npm), which CreateProcess
    # won't launch without a shell. shell=True lets cmd.exe resolve via PATHEXT.
    return subprocess.run(argv, shell=(os.name == "nt")).returncode


if __name__ == "__main__":
    raise SystemExit(main())
