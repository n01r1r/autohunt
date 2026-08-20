# Harness adapter for Gemini CLI

This is the **Gemini CLI adapter** for a host-agnostic harness. The harness
itself does not depend on Gemini: the runtime never calls a model, and the
agent backend is chosen from `HARNESS_AGENT`, `harness/routing/agent.env`, or
`PATH`. This file only teaches Gemini CLI how to drive it.

To install it in a target project, copy this file into the project's
`GEMINI.md` (Gemini CLI's context file), or reference its contents from an
existing one. Keep the harness scripts (`scaffold.py`, `run.py`, `agent.py`,
`setup.py`) reachable — check out the whole harness repository and run from
there, or copy the scripts alongside.

## How to run

Scaffolding is deterministic — run the script, do not hand-write the files.

```bash
python setup.py [target_dir]     # scaffold + detect an agent CLI + seed config
# or, files only:
python scaffold.py [target_dir]
```

- No `target_dir` → scaffolds into the current working directory.
- Existing files are **skipped, never overwritten** (safe to re-run, like `init`).
- Report to the user exactly what was created vs skipped (the script prints it).

For Gemini makers, select the backend explicitly:

```text
HARNESS_AGENT=gemini
```

`agent.py` maps that name to one non-interactive attempt: `gemini -p <prompt>`.
Contracts stay agent-agnostic; changing the backend never edits a contract.

**Auth note:** a Google Workspace account requires `GOOGLE_CLOUD_PROJECT` (or
`GOOGLE_CLOUD_PROJECT_ID`) in the environment before `gemini -p` will run
headless. Without it the maker exits with an auth error and the governor sees
a plain failed attempt — set the variable before starting a contract.

## Running a contract (the generic runtime)

`run.py` is the deterministic **governor / checker** — ONE runtime for all
contracts. It never calls a model. It runs the maker (a separate process via
`agent.py`), checks the result with a deterministic command, classifies
progress vs stagnation vs regression, enforces the budget cap, and appends
durable state so a crashed run resumes.

```bash
python run.py harness/contracts/<x>.contract.yaml
```

Needs `pyyaml` for YAML contracts (or write the contract as `.json`).

## The 7 cost/reliability moves — where each lives

Code moves are enforced by `run.py`; the rest are host habits documented in
`harness/principles/operating-policy.md`:

- **#1 should it be an agent** → CODE gate: `run.py` refuses a contract with no
  `success.command` ("a vibe, not a task").
- **#2 kill switch** → CODE: `budget.attempts` is a hard cap.
- **#3 durable state** → CODE: every attempt appended to `state/decisions.jsonl`;
  `run.py` resumes from it.
- **#4 MCP token tax** → GEMINI HABIT: audit configured servers with
  `gemini mcp list`; keep only what the task needs (servers live in
  `settings.json`).
- **#5 prompt cache** → GEMINI HABIT: provider-managed. Keep stable
  instructions first, changing state and logs last; avoid churn in the prefix.
- **#6 effort down** → GEMINI HABIT: pick the cheaper model tier with `-m`
  (e.g. a flash-class model) and raise only when a deterministic eval shows it
  falls short.
- **#7 maker ≠ checker** → CODE: `run.py` is only the checker/governor; the
  maker is a separate `gemini -p` process, so no agent grades its own homework.

## After scaffolding — tell the user the next 3 steps

1. Fill `harness/state/goal.yaml` — the goal + hard constraints the agent must
   not drift from.
2. Copy `harness/contracts/example.contract.yaml` to a real task; set
   `maker.command` (one attempt of work via `agent.py`), `success.command`
   (deterministic check, exit 0 = pass), and optionally `progress.command`.
   Then run it with `run.py`.
3. Append to `harness/state/rejected_paths.jsonl` as dead ends are found.

Do not add more structure until an actual failure needs it. Start at `state/`
plus one `contract`.

## Self-check

- `python scaffold.py --selftest`
- `python run.py --selftest`
- `python test_agnostic.py` — proves the runtime drives an arbitrary backend.
- `python test_agnostic.py --live` — smoke-tests real installed agents (costs tokens).
