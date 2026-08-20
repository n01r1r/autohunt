#!/usr/bin/env python3
"""behind() edge cases: must return None (stay silent), never crash or hang.

Covers the states a deployed machine actually hits: not a repo, a repo with no
upstream, and a repo whose remote is unreachable (offline). A reachable-and-
behind clone is exercised manually (needs network); the advisory line itself
is one print in setup.py.

Usage:
    python test_setup.py
"""
from __future__ import annotations
import subprocess
import tempfile
from pathlib import Path

from setup import behind


def _git(root: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=root, capture_output=True, check=True)


def main() -> int:
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)

        # not a repo -> None
        assert behind(root) is None, "non-repo must be silent"

        # repo, commits, but no upstream -> None (rev-list @{u} fails)
        _git(root, "init", "-q")
        _git(root, "-c", "user.email=t@t", "-c", "user.name=t",
             "commit", "-q", "--allow-empty", "-m", "x")
        assert behind(root) is None, "no-upstream repo must be silent"

        # unreachable remote -> fetch fails -> None (never a stale count)
        _git(root, "remote", "add", "origin",
             "file:///nonexistent/definitely/not/a/repo")
        assert behind(root) is None, "failed fetch must be silent, not stale"

    print("test_setup ok  (behind() silent on non-repo / no-upstream / offline)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
