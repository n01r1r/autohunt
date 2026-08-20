# Harness adapter for Codex CLI

This is the **Codex CLI adapter** for a host-agnostic harness. The harness
itself does not depend on Codex: the runtime never calls a model, and the agent
backend is chosen from `HARNESS_AGENT`, `harness/routing/agent.env`, or `PATH`.
This file only teaches Codex how to drive it.

To install it in a target project, copy this file into the project's
`AGENTS.md`, or reference its contents from an existing `AGENTS.md`. Keep the
harness scripts (`scaffold.py`, `run.py`, `agent.py`, and `setup.py`) reachable
in a **clone of the harness repository** (a clone stays updatable with
`git pull --ff-only`; see the harness README "Updating deployed copies"). Use
paths relative to wherever those scripts live.

## How to run

Scaffolding is deterministic. Run the script; do not hand-write the generated
files.

```bash
python setup.py [target_dir]     # scaffold + detect an agent CLI + seed config
# or, files only:
python scaffold.py [target_dir]
```

- With no `target_dir`, the scripts scaffold into the current working directory.
- Existing files are **skipped, never overwritten**, so re-running is safe.
- Report exactly what was created and skipped; the script prints both lists.

For Codex makers, select the backend explicitly in the shell or in
`harness/routing/agent.env`:

```text
HARNESS_AGENT=codex
```

`agent.py` maps that name to one non-interactive attempt:
`codex exec --skip-git-repo-check <prompt>`. Contracts remain agent-agnostic;
changing the backend does not require editing them.

## Running a contract (the generic runtime)

`run.py` is the deterministic **governor / checker**: one runtime for every
contract. It never calls a model directly. It launches the maker as a separate
process through `agent.py`, checks the result with a deterministic command,
classifies progress, stagnation, or regression, enforces the budget cap, and
appends durable state so an interrupted run can resume.

Run it from the target project's root, with `run.py` addressed in the clone
(scaffolded contracts already carry the clone's absolute script paths, baked
in at scaffold time):

```bash
python <clone>/run.py harness/contracts/<x>.contract.yaml
```

`harness/routing/agent.env` resolves against the maker's working directory --
the target project -- so it is target-project state, never clone state.

YAML contracts require `pyyaml`; a contract may instead be written as JSON.

## The 7 cost/reliability moves and where each lives

Code moves are enforced by `run.py`. The remaining moves are Codex CLI habits
described more fully in `harness/principles/operating-policy.md`.

- **#1 should it be an agent** -- CODE gate: `run.py` refuses a contract with
  no `success.command` ("a vibe, not a task").
- **#2 kill switch** -- CODE: `budget.attempts` is a hard cap.
- **#3 durable state** -- CODE: every attempt is appended to
  `harness/state/decisions.jsonl`; `run.py` resumes from it.
- **#4 MCP token tax** -- CODEX HABIT: use `codex mcp list` to audit configured
  servers and keep only the servers needed for the task. MCP management and its
  exact enable/disable controls are host/version-specific.
- **#5 prompt cache** -- CODEX HABIT: prefix caching is provider-managed. Keep
  stable instructions and repeated context first; put changing state, logs, and
  the current attempt last. Avoid needless changes to the stable prefix.
- **#6 effort down** -- CODEX HABIT: where the selected model supports it, use
  the `model_reasoning_effort` setting in Codex configuration or a profile.
  Start low or medium and raise it only when deterministic evaluation shows the
  lower setting is insufficient; available values are model/version-specific.
- **#7 maker / checker** -- CODE: `run.py` is only the checker/governor; the
  maker is a separate `codex exec` process, so the agent does not grade its own
  work.

## After scaffolding: tell the user the next 3 steps

1. Fill `harness/state/goal.yaml` with the goal and hard constraints the maker
   must not drift from.
2. Copy `harness/contracts/example.contract.yaml` to a real task. Set
   `maker.command` to one attempt of work through `agent.py`, set
   `success.command` to a deterministic check where exit code 0 means pass, and
   optionally set `progress.command`. Then run the contract with `run.py`.
3. Append dead ends to `harness/state/rejected_paths.jsonl` as they are found so
   later attempts do not rediscover them.

Do not add more structure until an actual failure requires it. Begin with
`state/` and one contract.

## Self-check

- `python scaffold.py --selftest`
- `python run.py --selftest`
- `python test_agnostic.py` -- proves the runtime can drive an arbitrary backend.
