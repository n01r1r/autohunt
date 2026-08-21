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
    timeouts         -> every command class (maker/success/progress/snapshot/
                        rollback) has a finite default and may override it;
                        a hung process is tree-killed (exit 124) instead of
                        blocking an unattended run
    baseline first   -> with a progress command, the pristine metric is measured
                        BEFORE the first maker run (attempt 0) so attempt 1 has
                        a real comparison and best-state tracking is sound
    incumbent best   -> classification compares against the best metric seen,
                        not the previous one; snapshot_command uses a durable
                        prepare/commit boundary before a new best is admitted,
                        rollback_command restores it
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
import os
import subprocess
import sys
import time
from pathlib import Path

TIMEOUT_EXIT = 124   # GNU timeout convention

LEDGER_KIND = "autohunt-ledger"
LEDGER_VERSION = 1

# A missing timeout remains backwards compatible with older contracts while
# still making every command invocation bounded.  Keep these defaults named so
# contract reports and tests can refer to the actual reliability envelope.
DEFAULT_MAKER_TIMEOUT_SEC = 600.0
DEFAULT_SUCCESS_TIMEOUT_SEC = 120.0
DEFAULT_PROGRESS_TIMEOUT_SEC = 120.0
DEFAULT_SNAPSHOT_TIMEOUT_SEC = 120.0
DEFAULT_ROLLBACK_TIMEOUT_SEC = 120.0
DEFAULT_TIMEOUT_SEC = DEFAULT_SUCCESS_TIMEOUT_SEC

VALID_ACTIONS = frozenset({"continue", "replan", "rollback", "escalate"})
FAILURE_ACTION_KEYS = frozenset({
    "regression", "stagnation", "plateau", "unknown", "tampered",
})
ROOT_KEYS = frozenset({
    "goal", "maker", "success", "progress", "protect", "budget",
    "failure_policy", "state", "run_id",
})
GOAL_KEYS = frozenset({"metric", "direction", "mode"})
COMMAND_KEYS = frozenset({"command", "timeout_sec"})
PROGRESS_KEYS = COMMAND_KEYS | {"stagnation_window"}
BUDGET_KEYS = frozenset({"attempts"})
STATE_KEYS = frozenset({"file"})
FAILURE_POLICY_KEYS = FAILURE_ACTION_KEYS | frozenset({
    "snapshot_command", "rollback_command", "snapshot_timeout_sec",
    "rollback_timeout_sec",
})


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


def _inside_target(path: Path, cwd: Path) -> bool:
    """Return whether *path* resolves inside the target project."""
    try:
        path.resolve(strict=False).relative_to(cwd.resolve())
    except ValueError:
        return False
    return True


def _is_finite_positive(value: object) -> bool:
    return (isinstance(value, (int, float)) and not isinstance(value, bool)
            and math.isfinite(float(value)) and float(value) > 0)


def _command_section(contract: dict, name: str) -> tuple[dict, list[str]]:
    """Read one command section without silently accepting a wrong type."""
    if name not in contract:
        return {}, []
    section = contract[name]
    if not isinstance(section, dict):
        return {}, [f"{name} must be a mapping"]
    errors: list[str] = []
    allowed = PROGRESS_KEYS if name == "progress" else COMMAND_KEYS
    unknown = sorted((key for key in section if key not in allowed), key=str)
    errors.extend(f"{name}.{key} is not a recognized field" for key in unknown)
    if "command" in section:
        command = section["command"]
        if not isinstance(command, str):
            errors.append(f"{name}.command must be a string")
        elif not command.strip():
            errors.append(f"{name}.command must not be empty")
    if "timeout_sec" in section and not _is_finite_positive(section["timeout_sec"]):
        errors.append(f"{name}.timeout_sec must be a finite positive number")
    if name == "maker" and section and "command" not in section:
        errors.append("maker.command is required when maker is supplied")
    if name == "progress" and section and "stagnation_window" in section \
            and "command" not in section:
        errors.append("progress.command is required when progress is supplied")
    return section, errors


def validate_contract(contract: object, cwd: Path) -> list[str]:
    """Canonical schema and safety validation shared by CLI and runtime.

    It performs only read-only checks of an existing state ledger.  It validates
    the shape and safety envelope that ``run_contract`` relies on, returning
    all errors so a caller can present them without partially starting a run.
    """
    errors: list[str] = []
    if not isinstance(contract, dict):
        return ["contract root must be a mapping"]
    errors.extend(f"{key} is not a recognized contract field"
                  for key in sorted((key for key in contract if key not in ROOT_KEYS),
                                    key=str))
    target = cwd.resolve()
    if not target.is_dir():
        errors.append("target cwd must be an existing directory")

    goal = contract.get("goal", {})
    if goal is None or not isinstance(goal, dict):
        errors.append("goal must be a mapping")
        goal = {}
    else:
        errors.extend(f"goal.{key} is not a recognized field"
                      for key in sorted((key for key in goal if key not in GOAL_KEYS),
                                        key=str))
    mode = goal.get("mode", "complete")
    if mode not in ("complete", "improve"):
        errors.append("goal.mode must be 'complete' or 'improve'")
    direction = goal.get("direction", "decrease")
    if direction not in ("increase", "decrease"):
        errors.append("goal.direction must be 'increase' or 'decrease'")
    if "mode" in goal and not isinstance(mode, str):
        errors.append("goal.mode must be a string")
    if "direction" in goal and not isinstance(direction, str):
        errors.append("goal.direction must be a string")
    if "metric" in goal and not isinstance(goal["metric"], str):
        errors.append("goal.metric must be a string")

    maker, maker_errors = _command_section(contract, "maker")
    success, success_errors = _command_section(contract, "success")
    progress, progress_errors = _command_section(contract, "progress")
    errors.extend(maker_errors + success_errors + progress_errors)
    success_command = success.get("command")
    progress_command = progress.get("command")
    if mode != "improve" and not (isinstance(success_command, str)
                                   and success_command.strip()):
        errors.append("success.command is required unless goal.mode=improve")
    if mode == "improve" and not (isinstance(progress_command, str)
                                   and progress_command.strip()):
        errors.append("progress.command is required for goal.mode=improve")
    if "stagnation_window" in progress:
        window = progress["stagnation_window"]
        if not isinstance(window, int) or isinstance(window, bool) or window < 1:
            errors.append("progress.stagnation_window must be a positive integer")

    budget = contract.get("budget", {})
    if not isinstance(budget, dict):
        errors.append("budget must be a mapping")
        budget = {}
    else:
        errors.extend(f"budget.{key} is not a recognized field"
                      for key in sorted((key for key in budget if key not in BUDGET_KEYS),
                                        key=str))
    attempts = budget.get("attempts")
    if not isinstance(attempts, int) or isinstance(attempts, bool) or attempts < 1:
        errors.append("budget.attempts must be a positive integer")

    failure_policy = contract.get("failure_policy", {})
    if failure_policy is None or not isinstance(failure_policy, dict):
        errors.append("failure_policy must be a mapping")
        failure_policy = {}
    else:
        errors.extend(f"failure_policy.{key} is not a recognized field"
                      for key in sorted((key for key in failure_policy
                                         if key not in FAILURE_POLICY_KEYS), key=str))
    for key, action in failure_policy.items():
        if (key in FAILURE_ACTION_KEYS
                and (not isinstance(action, str) or action not in VALID_ACTIONS)):
            errors.append(f"failure_policy.{key} must be one of: "
                          "continue, replan, rollback, escalate")
    if "rollback_timeout_sec" in failure_policy and not _is_finite_positive(
            failure_policy["rollback_timeout_sec"]):
        errors.append("failure_policy.rollback_timeout_sec must be a finite positive number")
    if "snapshot_timeout_sec" in failure_policy and not _is_finite_positive(
            failure_policy["snapshot_timeout_sec"]):
        errors.append("failure_policy.snapshot_timeout_sec must be a finite positive number")
    snapshot_command = failure_policy.get("snapshot_command")
    rollback_command = failure_policy.get("rollback_command")
    for key, command in (("snapshot_command", snapshot_command),
                         ("rollback_command", rollback_command)):
        if key in failure_policy and (not isinstance(command, str) or not command.strip()):
            errors.append(f"failure_policy.{key} must be a non-empty string")
    if (snapshot_command is None) != (rollback_command is None):
        errors.append("failure_policy.snapshot_command and rollback_command must be paired")
    if ("rollback" in failure_policy.values()
            and not (isinstance(snapshot_command, str) and snapshot_command.strip()
                     and isinstance(rollback_command, str) and rollback_command.strip())):
        errors.append("failure_policy rollback action requires snapshot_command and rollback_command")

    state = contract.get("state", {})
    if state is None or not isinstance(state, dict):
        errors.append("state must be a mapping")
        state = {}
    else:
        errors.extend(f"state.{key} is not a recognized field"
                      for key in sorted((key for key in state if key not in STATE_KEYS),
                                        key=str))
    state_rel = state.get("file", "harness/state/decisions.jsonl")
    if not isinstance(state_rel, str) or not state_rel.strip():
        errors.append("state.file must be a non-empty string")
    else:
        state_path = target / state_rel
        if not _inside_target(state_path, target):
            errors.append("state.file must stay inside the target project")
        elif state_path.is_symlink():
            errors.append("state.file must name a regular non-symlink file when it exists")
        elif state_path.exists() and not state_path.is_file():
            errors.append("state.file must name a regular file when it exists")
        elif state_path.exists():
            try:
                with state_path.open("rb") as handle:
                    header = json.loads(handle.readline().decode("utf-8"))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError):
                header = None
            if header != _ledger_header():
                errors.append("state.file must be an owned autohunt ledger with a valid header")

    protect = contract.get("protect", [])
    if not isinstance(protect, list):
        errors.append("protect must be a list")
        protect = []
    for rel in protect:
        if not isinstance(rel, str) or not rel.strip():
            errors.append("protect entries must be non-empty strings")
            continue
        path = target / rel
        if not _inside_target(path, target):
            errors.append(f"protected path must stay inside the target project: {rel}")
        elif path.is_symlink() or not path.is_file():
            errors.append(f"protected path must be an existing regular non-symlink file: {rel}")

    run_id = contract.get("run_id")
    if run_id is not None and (not isinstance(run_id, str) or not run_id.strip()):
        errors.append("run_id must be a non-empty string when supplied")
    return errors


def _canonical_json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False, default=str)


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _identity(contract_path: Path, contract: dict) -> dict:
    """Build stable resume identity for one canonical contract invocation."""
    resolved = os.path.normcase(str(contract_path.resolve()))
    contract_hash = _sha256_text(_canonical_json(contract))
    evaluator_config = {
        key: contract.get(key)
        for key in ("goal", "success", "progress", "protect", "failure_policy")
    }
    identity = {
        "contract_path": resolved,
        "contract_hash": contract_hash,
        "evaluator_config_hash": _sha256_text(_canonical_json(evaluator_config)),
    }
    if contract.get("run_id") is not None:
        identity["run_id"] = contract["run_id"]
    return identity


def _record_with_identity(record: dict, identity: dict, tag: str) -> dict:
    result = dict(record)
    result["contract"] = tag
    result["identity"] = dict(identity)
    # Keep the fields flat as well: JSONL consumers can filter without knowing
    # the nested envelope, while the envelope is the canonical identity.
    result.update(identity)
    return result


def _matching_record(record: object, identity: dict) -> bool:
    if not isinstance(record, dict):
        raise SystemExit("REFUSED: ledger record is not a mapping")
    nested = record.get("identity")
    if isinstance(nested, dict):
        if nested != identity:
            return False
        return True
    # Filename-only and other pre-identity records cannot be safely assigned to
    # a contract.  Refuse them instead of silently starting a fresh run in the
    # same ledger and losing the old attempt budget/incumbent.
    raise SystemExit("REFUSED: legacy ledger record has no exact identity")


def _ledger_header() -> dict:
    return {"kind": LEDGER_KIND, "version": LEDGER_VERSION}


def _ensure_owned_ledger(state_file: Path) -> None:
    """Create or verify the first-line ownership marker for a state ledger."""
    if not state_file.exists():
        state_file.parent.mkdir(parents=True, exist_ok=True)
        with state_file.open("x", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(_ledger_header(), separators=(",", ":")) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        return
    if state_file.is_symlink() or not state_file.is_file():
        raise SystemExit("REFUSED: state ledger path is not a regular file")
    with state_file.open("rb") as handle:
        first = handle.readline()
    try:
        header = json.loads(first.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SystemExit(f"REFUSED: state ledger is missing an autohunt header: {exc}")
    if header != _ledger_header():
        raise SystemExit("REFUSED: state ledger is not an owned autohunt ledger")


def _read_jsonl(state_file: Path) -> list[dict]:
    """Read durable JSONL and truncate one torn final nonblank line.

    A malformed earlier line is an explicit refusal: silently skipping it
    could change the incumbent and attempt count.  A malformed final line is
    the expected power-loss/torn-write case, so it is removed and the file is
    fsynced before resume continues.
    """
    _ensure_owned_ledger(state_file)
    raw = state_file.read_bytes()
    lines = raw.splitlines(keepends=True)
    nonblank = [index for index, line in enumerate(lines) if line.strip()]
    if not nonblank:
        raise SystemExit("REFUSED: empty state ledger has no autohunt header")
    try:
        header = json.loads(lines[nonblank[0]].decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SystemExit(f"REFUSED: state ledger header is malformed: {exc}")
    if header != _ledger_header():
        raise SystemExit("REFUSED: state ledger is not an owned autohunt ledger")
    records: list[dict] = []
    record_positions = nonblank[1:]
    for position, index in enumerate(record_positions):
        line = lines[index]
        try:
            value = json.loads(line.decode("utf-8"))
            if not isinstance(value, dict):
                raise ValueError("record is not a mapping")
        except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError) as exc:
            if position != len(record_positions) - 1:
                raise SystemExit(
                    f"REFUSED: malformed earlier ledger record at line {index + 1}: {exc}")
            truncate_at = sum(len(item) for item in lines[:index])
            with state_file.open("r+b") as handle:
                handle.truncate(truncate_at)
                handle.flush()
                os.fsync(handle.fileno())
            break
        records.append(value)
    return records


def _append_jsonl(state_file: Path, record: dict) -> None:
    _ensure_owned_ledger(state_file)
    line = json.dumps(record, ensure_ascii=False, allow_nan=False) + "\n"
    with state_file.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(line)
        handle.flush()
        os.fsync(handle.fileno())


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


def sh(cmd: str, cwd: Path, timeout: float | None = DEFAULT_TIMEOUT_SEC) -> int:
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


def numeric(cmd: str | None, cwd: Path,
            timeout: float | None = DEFAULT_PROGRESS_TIMEOUT_SEC) -> float | None:
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
    """Hash protected files without following replacement symlinks.

    The kind is part of the digest so deletion, directory replacement, and
    symlink retargeting are all tampering even when the replacement happens to
    contain identical bytes.
    """
    result = {}
    for rel in paths:
        path = cwd / rel
        if path.is_symlink():
            result[rel] = {"kind": "symlink", "target": os.readlink(path)}
        elif not path.exists():
            result[rel] = {"kind": "missing"}
        elif not path.is_file():
            result[rel] = {"kind": "nonfile"}
        else:
            result[rel] = {
                "kind": "file",
                "digest": hashlib.sha256(path.read_bytes()).hexdigest(),
            }
    return result


def _git_head(cwd: Path) -> str | None:
    r = subprocess.run("git rev-parse --short HEAD", shell=True, cwd=str(cwd),
                       capture_output=True, text=True)
    if r.returncode != 0:
        return None
    return r.stdout.strip() or None


def run_contract(contract_path: Path, cwd: Path) -> str:
    c = load_contract(contract_path)
    errors = validate_contract(c, cwd)
    if errors:
        # Keep the historic high-signal refusal for the no-success gate while
        # routing all other schema decisions through the canonical validator.
        if errors[0] == "success.command is required unless goal.mode=improve":
            raise SystemExit(
                "REFUSED: no success.command. Not a task, a vibe - write a "
                "checkable 'done and correct looks like ___' first (or declare "
                "goal.mode: improve for a budget-terminated hunt).")
        raise SystemExit("REFUSED: " + errors[0])

    goal = c.get("goal", {})
    mode = goal.get("mode", "complete")
    direction = goal.get("direction", "decrease")
    maker = c.get("maker", {})
    success = c.get("success", {})
    prog = c.get("progress", {})
    fp = c.get("failure_policy", {})
    protect = c.get("protect", [])
    success_cmd = success.get("command")
    maker_cmd = maker.get("command")
    prog_cmd = prog.get("command")
    cap = c["budget"]["attempts"]
    window = prog.get("stagnation_window", 2)
    mk_t = float(maker.get("timeout_sec", DEFAULT_MAKER_TIMEOUT_SEC))
    ok_t = float(success.get("timeout_sec", DEFAULT_SUCCESS_TIMEOUT_SEC))
    pg_t = float(prog.get("timeout_sec", DEFAULT_PROGRESS_TIMEOUT_SEC))
    rb_t = float(fp.get("rollback_timeout_sec", DEFAULT_ROLLBACK_TIMEOUT_SEC))
    snap_t = float(fp.get("snapshot_timeout_sec", DEFAULT_SNAPSHOT_TIMEOUT_SEC))
    snap_cmd = fp.get("snapshot_command")
    rollback_cmd = fp.get("rollback_command")

    contract_path = contract_path.resolve()
    cwd = cwd.resolve()
    state_rel = c.get("state", {}).get("file", "harness/state/decisions.jsonl")
    state_file = cwd / state_rel
    state_file.parent.mkdir(parents=True, exist_ok=True)
    tag = contract_path.name
    identity = _identity(contract_path, c)

    def record(rec: dict) -> None:
        _append_jsonl(state_file, _record_with_identity(rec, identity, tag))

    def terminal(reason: str, result: str, attempt: int | None = None,
                 **details: object) -> None:
        payload: dict[str, object] = {
            "phase": "terminal",
            "terminal": True,
            "terminal_result": result,
            "terminal_reason": reason,
        }
        if attempt is not None:
            payload["attempt"] = attempt
        payload.update(details)
        record(payload)

    # Resume only records belonging to the exact contract identity.  A record
    # from an older version, even with the same filename, is intentionally a
    # fresh run rather than an implicit completion claim.
    history: list = []
    prior = 0
    baseline = None
    committed_snapshot = False
    pending_snapshots: dict[tuple[int, str], dict] = {}
    for raw_record in _read_jsonl(state_file):
        if not _matching_record(raw_record, identity):
            continue
        terminal_result = raw_record.get("terminal_result")
        if terminal_result in {"ESCALATE", "WON"}:
            print(f"already terminal ({terminal_result}) (prior run). {state_file}")
            return terminal_result
        phase = raw_record.get("phase")
        key = (int(raw_record.get("attempt", 0)),
               str(raw_record.get("snapshot_token", "")))
        if phase == "snapshot_prepare":
            pending_snapshots[key] = raw_record
            continue
        if phase in ("snapshot_commit", "snapshot_failed"):
            pending_snapshots.pop(key, None)
        if phase == "snapshot_commit" and raw_record.get("snapshot_status") == "committed":
            committed_snapshot = True
        if raw_record.get("verdict") == "won":
            print(f"already satisfied (prior run). {state_file}")
            return "WON"
        if raw_record.get("verdict") == "baseline":
            if raw_record.get("snapshot_status") != "failed":
                baseline = raw_record.get("metric")
                history.append(baseline)
            continue
        if raw_record.get("attempt", 0) > 0:
            prior += 1
            # Unscored verdicts (unknown/tampered/snapshot failure) never
            # advance the incumbent, including after a restart.
            scored = (raw_record.get("verdict") not in ("unknown", "tampered")
                      and raw_record.get("incumbent", True))
            history.append(raw_record.get("metric") if scored else None)

    if pending_snapshots:
        print("uncommitted snapshot prepare found; refusing to resume ambiguous incumbent")
        terminal("uncommitted_snapshot", "ESCALATE")
        return "ESCALATE"

    if mode == "improve" and prior > 0 and baseline is None:
        raise SystemExit(
            "REFUSED: improve ledger has no durable attempt-0 baseline; "
            "legacy state cannot establish a committed incumbent")

    # Baseline: pristine metric BEFORE the maker ever runs, once per contract.
    if prog_cmd and prior == 0 and baseline is None:
        baseline_metric = numeric(prog_cmd, cwd, pg_t)
        snapshot_status = "not_requested"
        if snap_cmd and baseline_metric is not None:
            token = "baseline-0"
            record({"phase": "snapshot_prepare", "snapshot_token": token,
                    "attempt": 0, "verdict": "baseline",
                    "metric": baseline_metric, "candidate_metric": baseline_metric,
                    "snapshot_status": "prepared", "incumbent": False})
            rc = sh(snap_cmd, cwd, snap_t)
            if rc != 0:
                record({"phase": "snapshot_failed", "snapshot_token": token,
                        "attempt": 0, "verdict": "baseline", "metric": baseline_metric,
                        "best": None, "action": "escalate", "incumbent": False,
                        "snapshot_status": "failed", "snapshot_exit": rc,
                        "reason": "baseline snapshot did not complete"})
                print(f"[0/{cap}] baseline snapshot failed (exit {rc}) -> escalate")
                terminal("baseline_snapshot_failed", "ESCALATE", 0,
                         snapshot_exit=rc)
                return "ESCALATE"
            snapshot_status = "committed"
            committed_snapshot = True
            record({"phase": "snapshot_commit", "snapshot_token": token,
                    "attempt": 0, "verdict": "baseline", "metric": baseline_metric,
                    "best": baseline_metric, "action": "none", "incumbent": True,
                    "snapshot_status": snapshot_status, "snapshot_exit": rc,
                    "commit": _git_head(cwd)})
        else:
            record({"attempt": 0, "verdict": "baseline", "metric": baseline_metric,
                    "best": baseline_metric, "action": "none", "incumbent": True,
                    "snapshot_status": snapshot_status, "commit": _git_head(cwd)})
        baseline = baseline_metric
        print(f"[0/{cap}] baseline metric={baseline_metric}")
        history.append(baseline_metric)

    for i in range(prior, cap):
        attempt = i + 1
        t0 = time.monotonic()
        pre = _digest(protect, cwd) if protect else {}
        maker_exit = None
        if maker_cmd:                               # maker is a separate process
            maker_exit = sh(maker_cmd, cwd, mk_t)
        tampered = [k for k, v in pre.items()
                    if _digest([k], cwd)[k] != v]

        note_file = state_file.parent / "attempt_note.txt"
        note = None
        if note_file.exists():
            note = note_file.read_text(encoding="utf-8").strip() or None
            note_file.unlink()

        success_exit = None
        metric = None
        if tampered:                                # maker edited its own judge
            passed, verdict = False, "tampered"
        elif maker_exit == TIMEOUT_EXIT:
            # A timed-out maker may have left a partial mutation.  Do not run
            # checker or metric against that state; it is an invalid signal.
            passed, verdict = False, "unknown"
        else:
            if success_cmd:
                success_exit = sh(success_cmd, cwd, ok_t)
            passed = success_exit == 0
            metric = numeric(prog_cmd, cwd, pg_t)
            if success_exit == TIMEOUT_EXIT:        # hung checker = invalid signal
                verdict = "unknown"
            else:
                verdict = classify(passed, metric, history, direction, window)
        default_action = "escalate" if verdict in ("unknown", "tampered") else "continue"
        action = "stop" if verdict == "won" else fp.get(verdict, default_action)

        scored = verdict not in ("unknown", "tampered")
        snapshot_status = "not_requested"
        snapshot_exit = None
        if verdict == "progress" and snap_cmd:
            # A prepare line is durable before the external snapshot runs.  A
            # later commit line is the only point at which this metric enters
            # history as an incumbent.  Resume escalates on a prepare without
            # a commit, which covers a crash in this interval.
            token = f"attempt-{attempt}"
            record({"phase": "snapshot_prepare", "snapshot_token": token,
                    "attempt": attempt, "verdict": "progress", "metric": metric,
                    "candidate_metric": metric, "snapshot_status": "prepared",
                    "incumbent": False})
            snapshot_exit = sh(snap_cmd, cwd, snap_t)
            if snapshot_exit != 0:
                verdict = "unknown"
                action = "escalate"
                scored = False
                snapshot_status = "failed"
                best = _best(history, direction)
                rec = {"phase": "snapshot_failed", "snapshot_token": token,
                       "attempt": attempt, "verdict": verdict,
                       "candidate_verdict": "progress", "metric": metric,
                       "best": best, "action": action, "incumbent": False,
                       "snapshot_status": snapshot_status, "snapshot_exit": snapshot_exit,
                       "maker_exit": maker_exit, "success_exit": success_exit,
                       "duration_sec": round(time.monotonic() - t0, 1),
                       "commit": _git_head(cwd),
                       "reason": "candidate metric was not admitted: snapshot failed"}
                if note:
                    rec["note"] = note
                if tampered:
                    rec["tampered"] = tampered
                record(rec)
                print(f"[{attempt}/{cap}] verdict=unknown metric={metric} best={best} -> escalate")
                terminal("snapshot_failed", "ESCALATE", attempt,
                         snapshot_exit=snapshot_exit, maker_exit=maker_exit,
                         success_exit=success_exit, metric=metric,
                         verdict="unknown", best=best,
                         snapshot_status="failed", incumbent=False,
                         action="escalate")
                return "ESCALATE"
            snapshot_status = "committed"
            committed_snapshot = True
            # The commit record is the final attempt record for this branch.
            history.append(metric if scored else None)
            best = _best(history, direction)
            rec = {"phase": "snapshot_commit", "snapshot_token": token,
                   "attempt": attempt, "verdict": verdict, "metric": metric,
                   "best": best, "action": action, "incumbent": True,
                   "snapshot_status": snapshot_status, "snapshot_exit": snapshot_exit,
                   "maker_exit": maker_exit, "success_exit": success_exit,
                   "duration_sec": round(time.monotonic() - t0, 1),
                   "commit": _git_head(cwd)}
        else:
            history.append(metric if scored else None)
            best = _best(history, direction)
            rec = {"attempt": attempt, "verdict": verdict,
                   "metric": metric, "best": best, "action": action,
                   "incumbent": scored, "snapshot_status": snapshot_status,
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
        if action == "escalate" and verdict in ("unknown", "tampered"):
            terminal(verdict, "ESCALATE", attempt, metric=metric,
                     maker_exit=maker_exit, success_exit=success_exit,
                     tampered=tampered, verdict=verdict, best=best)
            return "ESCALATE"
        if action == "rollback":
            rc = sh(rollback_cmd, cwd, rb_t)
            if rc != 0:                         # unrestored regression is ambiguous
                print(f"rollback_command failed (exit {rc}) - cannot restore incumbent; escalating.")
                terminal("rollback_failed", "ESCALATE", attempt,
                         rollback_exit=rc, maker_exit=maker_exit,
                         success_exit=success_exit, metric=metric,
                         verdict=verdict, best=best)
                return "ESCALATE"
        elif action == "escalate":
            terminal(verdict, "ESCALATE", attempt, metric=metric,
                     maker_exit=maker_exit, success_exit=success_exit,
                     verdict=verdict, best=best)
            return "ESCALATE"
        # continue / replan: next attempt; maker reads updated state and replans

    if mode == "improve":
        base = baseline
        b = _best(history, direction)
        if (base is not None and b is not None and
                ((b < base) if direction == "decrease" else (b > base))):
            if committed_snapshot and rollback_cmd:
                restore_exit = sh(rollback_cmd, cwd, rb_t)
                if restore_exit != 0:
                    print("improve incumbent restore failed "
                          f"(exit {restore_exit}) - escalating.")
                    terminal("improve_restore_failed", "ESCALATE",
                             rollback_exit=restore_exit, best=b, baseline=base)
                    return "ESCALATE"
                record({"phase": "incumbent_restore", "restore_status": "committed",
                        "rollback_exit": restore_exit, "best": b, "baseline": base})
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
                "budget": {"attempts": 3},
                "failure_policy": {"unknown": "continue"}}
        (root / "lose.json").write_text(json.dumps(lose), encoding="utf-8")
        assert run_contract(root / "lose.json", root) == "UNRESOLVED", "must stop at cap"
        recs = [json.loads(l) for l in (root / "harness/state/decisions.jsonl")
                .read_text(encoding="utf-8").splitlines()
                if l.strip() and json.loads(l).get("kind") != LEDGER_KIND]
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
