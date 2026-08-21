#!/usr/bin/env python3
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path

from autohunt_cli import validate_contract
from scaffold import scaffold


def installed_e2e() -> None:
    """Exercise the installed console scripts against an external target."""
    executable = sys.executable
    with tempfile.TemporaryDirectory(prefix="autohunt e2e ") as d:
        target = Path(d) / "target with spaces"
        init = subprocess.run(["autohunt", "init", str(target)],
                              capture_output=True, text=True)
        assert init.returncode == 0, init.stderr or init.stdout
        (target / "inc.py").write_text(
            "from pathlib import Path\n"
            "p=Path('n.txt'); p.write_text(str(int(p.read_text()) + 1) if p.exists() else '1')\n",
            encoding="utf-8")
        (target / "check.py").write_text(
            "import sys\nfrom pathlib import Path\n"
            "sys.exit(0 if int(Path('n.txt').read_text()) >= 2 else 1)\n",
            encoding="utf-8")
        (target / "metric.py").write_text(
            "from pathlib import Path\n"
            "print(int(Path('n.txt').read_text()) if Path('n.txt').exists() else 0)\n",
            encoding="utf-8")
        contract = {
            "maker": {"command": f'"{executable}" inc.py'},
            "success": {"command": f'"{executable}" check.py'},
            "progress": {"command": f'"{executable}" metric.py'},
            "budget": {"attempts": 3},
        }
        path = target / "harness/contracts/e2e.json"
        path.write_text(json.dumps(contract), encoding="utf-8")
        validate = subprocess.run(
            ["autohunt", "validate", str(path), "--cwd", str(target)],
            capture_output=True, text=True)
        assert validate.returncode == 0, validate.stderr or validate.stdout
        run = subprocess.run(
            ["autohunt", "run", str(path), "--cwd", str(target)],
            capture_output=True, text=True)
        assert run.returncode == 0, run.stderr or run.stdout
        assert "RESULT: WON" in run.stdout, run.stdout


def main() -> int:
    installed_requested = "--installed-e2e" in sys.argv[1:]
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
    if installed_requested:
        installed_e2e()
        print("installed CLI E2E ok")
    print("test_cli ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
