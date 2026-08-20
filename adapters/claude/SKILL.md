---
name: harness-init
description: Scaffold a portable, agent-agnostic harness into the current folder (init-like). Creates state/ (externalized goal + decisions + rejected_paths), contracts/ (CompletionContract per repeated task — goal/success/progress/budget/recovery, so loops are derived not hand-written), routing/ (executor ladder + agent backend), and principles/ (canonical, host-agnostic). Use when the user wants to set up an agent harness, agentic workflow scaffold, model-agnostic agent structure, or says "harness init", "set up the harness here", "scaffold the agent harness", or invokes /harness-init. Runs a deterministic script; refuses to overwrite existing files so it is safe to re-run.
---

# harness-init (Claude Code adapter)

This is the **Claude Code adapter** for a host-agnostic harness. The harness
itself does not depend on Claude — the runtime never calls a model, and the
agent backend is chosen from `$HARNESS_AGENT` / `harness/routing/agent.env` /
`PATH`. This file just teaches Claude Code how to drive it.

To install as a skill, copy this folder to `~/.claude/skills/harness-init/`
**and** keep the harness scripts (`scaffold.py`, `run.py`, `agent.py`,
`setup.py`) reachable — either check out the whole repo and run from there, or
copy the scripts alongside. Then invoke the commands below with paths relative
to wherever the scripts live.

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
- **#4 MCP token tax** → HABIT: audit `/context`, drop unused with `/mcp`.
- **#5 prompt cache** → HABIT: stable prefix first, dynamic last.
- **#6 effort down** → HABIT: `/effort` medium/low.
- **#7 maker ≠ checker** → CODE: `run.py` is only the checker/governor; the maker
  is a separate command, so no agent grades its own homework.

## After scaffolding — tell the user the next 3 steps

1. Fill `harness/state/goal.yaml` — the goal + hard constraints the agent must not drift from.
2. Copy `harness/contracts/example.contract.yaml` to a real task; set `maker.command`
   (one attempt of work, agent-agnostic via `agent.py`), `success.command`
   (deterministic check, exit 0 = pass), and optionally `progress.command`.
   Then run it with `run.py`.
3. Append to `harness/state/rejected_paths.jsonl` as dead ends are found.

Do not add more structure until an actual failure needs it. Start at `state/`
plus one `contract`.

## Self-check

- `python scaffold.py --selftest`
- `python run.py --selftest`
- `python test_agnostic.py` — proves the runtime drives an arbitrary backend.
