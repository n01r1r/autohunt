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
import shlex
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


def tokenize_backend_command(name: str) -> list[str]:
    """Tokenize a backend name/prefix exactly as the runtime will execute it."""
    if not isinstance(name, str) or not name.strip():
        raise ValueError("agent backend must be a non-empty string")
    if name in AGENTS:
        return [name]
    try:
        tokens = shlex.split(name, posix=False)
    except ValueError as exc:
        raise ValueError(f"invalid agent backend command: {exc}") from exc
    if not tokens:
        raise ValueError("agent backend must contain an executable")
    # posix=False preserves Windows backslashes while retaining quoted paths;
    # strip only the quote characters that delimit the executable token.
    return [token.strip('"') for token in tokens]


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
    return [*tokenize_backend_command(name), prompt]


def main() -> int:
    if sys.argv[1:] in (["--help"], ["-h"]):
        print(__doc__)
        return 0
    prompt = " ".join(sys.argv[1:]).strip() or sys.stdin.read().strip()
    if not prompt:
        sys.exit("no prompt given")
    name = pick()
    argv = build(name, prompt)
    prog = shutil.which(argv[0]) or argv[0]
    argv = [prog, *argv[1:]]
    # A .cmd/.bat shim (npm-installed CLIs on Windows) can't be launched by
    # CreateProcess directly, so it needs a shell. Resolve the program first so
    # .exe backends (and all POSIX backends) run shell-free and are immune to
    # metacharacter parsing. Only true batch shims fall back to the shell.
    # ponytail: cmd.exe still parses % & | < > in the prompt for .cmd backends
    # on Windows; keep prompts plain there, or use an .exe backend, if that bites.
    is_shim = os.name == "nt" and str(prog).lower().endswith((".cmd", ".bat"))
    return subprocess.run(argv, shell=is_shim).returncode


if __name__ == "__main__":
    raise SystemExit(main())
