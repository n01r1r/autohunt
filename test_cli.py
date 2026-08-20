#!/usr/bin/env python3
from __future__ import annotations

import json
import tempfile
from pathlib import Path

from autohunt_cli import validate_contract
from scaffold import scaffold


def main() -> int:
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        scaffold(root)
        example = root / "harness/contracts/example.contract.yaml"
        text = example.read_text(encoding="utf-8")
        assert "autohunt-agent" in text
        assert str(Path(__file__).resolve().parent) not in text
        active = [line for line in text.splitlines() if not line.lstrip().startswith("#")]
        assert not any("rollback_command:" in line for line in active)

        valid = {"success": {"command": "python check.py"}, "budget": {"attempts": 2}}
        path = root / "valid.json"
        path.write_text(json.dumps(valid), encoding="utf-8")
        assert validate_contract(path, root) == []

        valid["state"] = {"file": "../escape.jsonl"}
        path.write_text(json.dumps(valid), encoding="utf-8")
        assert any("state.file" in e for e in validate_contract(path, root))

        # mode vocabulary must match run.py's authority: validate has to reject
        # what run_contract would REFUSE, or it hands a false PASS to the user.
        bad_mode = {"goal": {"mode": "threshold"},
                    "success": {"command": "x"}, "budget": {"attempts": 1}}
        path.write_text(json.dumps(bad_mode), encoding="utf-8")
        assert any("goal.mode" in e for e in validate_contract(path, root)), \
            "unknown mode must fail validate (run.py would REFUSE it)"

        improve_no_prog = {"goal": {"mode": "improve"}, "budget": {"attempts": 1}}
        path.write_text(json.dumps(improve_no_prog), encoding="utf-8")
        assert any("progress.command" in e for e in validate_contract(path, root)), \
            "improve mode needs progress.command"
    print("test_cli ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
