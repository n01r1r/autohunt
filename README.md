# autohunt

A portable, **agent-agnostic** harness for autonomous task loops — it *hunts*
a goal on its own. It separates the invariant (goal, verification, budget,
memory) from the variable (which model/CLI runs, loop mechanics). One
deterministic runtime drives any agent backend to a checkable finish,
unattended, and pulls in a human only when the signal is ambiguous.

> Rule: don't invoke an agent when cache / code / tool does the job.

## Why agent-agnostic

The runtime (`run.py`) never calls a model. It runs a **maker** (a separate
process), checks the result with a **deterministic command**, and classifies
progress. The only file that knows a specific agent CLI exists is `agent.py`,
which picks a backend from `$HARNESS_AGENT`, `harness/routing/agent.env`, or
`PATH`:

```
claude | gemini | codex | aider | llm | any "mycli --flag"
```

Swap the backend without touching a contract. The test suite proves this: the
loop reaches a WON state driven by a plain shell command as the "agent" — no
model involved (`test_agnostic.py`, Part A).

## Autonomous by design ("auto-hunt")

The governor runs attempts to the **budget cap** on its own:

```
ACT -> VERIFY -> won? stop | progressing? act | stagnating? replan
                | regressing? rollback | budget out? escalate
```

The **only** path back to a human is an `unknown` verdict (ambiguous signal).
Routine progress, stagnation, and regression are handled automatically by
continue / replan / rollback. Minimal touches; maximum unattended progress.

## Quickstart

```bash
python setup.py                 # scaffold harness/, detect an agent CLI, seed config
# edit harness/state/goal.yaml            (the invariant)
# edit harness/contracts/example.contract.yaml  (maker / success / progress)
python run.py harness/contracts/example.contract.yaml
```

Pick a backend explicitly if you want:

```bash
HARNESS_AGENT=gemini python run.py harness/contracts/example.contract.yaml
```

YAML contracts need `pyyaml` (`pip install pyyaml`); `.json` contracts need nothing.

## The pieces

| file | role |
|------|------|
| `run.py` | deterministic governor / checker. Never calls a model. Kill switch, maker≠checker, durable resume. |
| `agent.py` | the agnostic seam. One prompt in, the chosen agent CLI runs it headless. |
| `scaffold.py` | writes the `harness/` template (never overwrites; safe to re-run). |
| `setup.py` | scaffold + detect an agent + seed `agent.env` in one shot. |
| `harness/contracts/*.yaml` | one CompletionContract per task: goal / success / progress / budget / failure_policy. |
| `harness/state/` | externalized memory: decisions + rejected paths, so a fresh session never rediscovers a dead end. |
| `harness/principles/` | canonical, host-agnostic rules. Host adapters generate from here. |
| `adapters/claude/` | example host adapter (install as a Claude Code skill). |

## Test

```bash
python scaffold.py --selftest       # create-all + no-overwrite-on-rerun
python run.py --selftest            # winnable -> WON; unwinnable -> stops at cap
python test_agnostic.py             # Part A: runtime drives an arbitrary backend
python test_agnostic.py --live      # + Part B: smoke-test installed real agents (costs tokens)
```

CI (`.github/workflows/ci.yml`) runs the deterministic parts on Linux + Windows.

## License

MIT.
