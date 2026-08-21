#!/usr/bin/env python3
"""Stable command-line entry point for autohunt."""
from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

from agent import AGENTS, tokenize_backend_command
from run import load_contract, run_contract, validate_contract as validate_loaded_contract
from scaffold import scaffold


def _agents() -> list[str]:
    return [name for name in AGENTS if shutil.which(name)]


def _set_agent(target: Path, name: str) -> None:
    tokens = tokenize_backend_command(name)
    if name not in AGENTS and not shutil.which(tokens[0]):
        raise ValueError(f"agent backend is not available: {name}")
    path = target / "harness/routing/agent.env"
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    lines = [line for line in lines if not line.strip().startswith("HARNESS_AGENT=")]
    lines.append(f"HARNESS_AGENT={name}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def validate_contract(path: Path, cwd: Path) -> list[str]:
    errors: list[str] = []
    try:
        c = load_contract(path)
    except Exception as exc:
        return [f"cannot load contract: {exc}"]
    return validate_loaded_contract(c, cwd)


def cmd_init(args: argparse.Namespace) -> int:
    target = Path(args.target).resolve()
    created, skipped = scaffold(target)
    print(f"harness -> {target}")
    for name in created:
        print(f"  created  {name}")
    for name in skipped:
        print(f"  skipped  {name} (exists)")
    found = _agents()
    if args.agent:
        try:
            _set_agent(target, args.agent)
        except ValueError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        print(f"  selected agent: {args.agent}")
    elif len(found) == 1:
        _set_agent(target, found[0])
        print(f"  selected only detected agent: {found[0]}")
    elif found:
        print("  detected agents: " + ", ".join(found))
        print("  no backend selected; rerun with --agent NAME")
    else:
        print("  no known agent CLI detected; use --agent NAME after installation")
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    target = Path(args.target).resolve()
    checks = [
        (sys.version_info >= (3, 10), "Python >= 3.10"),
        ((target / "harness").is_dir(), "harness directory"),
        (shutil.which("git") is not None, "git executable"),
        (bool(_agents()), "known agent CLI on PATH"),
    ]
    try:
        import yaml  # noqa: F401
        checks.append((True, "PyYAML"))
    except ImportError:
        checks.append((False, "PyYAML"))
    for ok, label in checks:
        print(f"[{'PASS' if ok else 'FAIL'}] {label}")
    return 0 if all(ok for ok, _ in checks) else 1


def cmd_validate(args: argparse.Namespace) -> int:
    cwd = Path(args.cwd).resolve()
    errors = validate_contract(Path(args.contract).resolve(), cwd)
    if errors:
        for error in errors:
            print(f"[FAIL] {error}")
        return 1
    print("[PASS] contract is structurally valid")
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    cwd = Path(args.cwd).resolve()
    path = Path(args.contract).resolve()
    errors = validate_contract(path, cwd)
    if errors:
        for error in errors:
            print(f"[FAIL] {error}")
        return 1
    result = run_contract(path, cwd)
    print(f"RESULT: {result}")
    return 0 if result in ("WON", "IMPROVED") else 1


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="autohunt")
    sub = p.add_subparsers(dest="command", required=True)
    init = sub.add_parser("init", help="scaffold a target project")
    init.add_argument("target", nargs="?", default=".")
    init.add_argument("--agent")
    init.set_defaults(func=cmd_init)
    doctor = sub.add_parser("doctor", help="check runtime prerequisites")
    doctor.add_argument("target", nargs="?", default=".")
    doctor.set_defaults(func=cmd_doctor)
    validate = sub.add_parser("validate", help="validate a contract without running it")
    validate.add_argument("contract")
    validate.add_argument("--cwd", default=".")
    validate.set_defaults(func=cmd_validate)
    run = sub.add_parser("run", help="validate and run a contract")
    run.add_argument("contract")
    run.add_argument("--cwd", default=".")
    run.set_defaults(func=cmd_run)
    return p


def main() -> int:
    args = parser().parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
