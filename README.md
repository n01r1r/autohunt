# autohunt

autohunt runs a task loop that hunts a goal until a deterministic check says
done. You write down what "done" means and how many attempts it may cost; the
harness drives any agent CLI there unattended, and interrupts you only when
the signal is ambiguous.

What makes it portable: the invariant lives in files (goal, verification
command, budget, memory) and the variable is just which model or CLI happens
to run. Swap the model; keep the contract.

> Rule: don't invoke an agent when cache / code / tool does the job.

## Why agent-agnostic

The runtime (`run.py`) never calls a model. It runs a **maker** as a separate
process, checks the result with a deterministic command, and classifies
progress. Exactly one file knows that specific agent CLIs exist — `agent.py` —
and it picks a backend from `$HARNESS_AGENT`, `harness/routing/agent.env`, or
`PATH`:

```
claude | gemini | codex | aider | llm | any "mycli --flag"
```

Swap the backend without touching a contract. The test suite proves the point
by driving the loop to WON with a plain shell command as the "agent" — no
model involved (`test_agnostic.py`, Part A).

## Autonomous by design ("auto-hunt")

The governor runs attempts to the **budget cap** on its own:

```
ACT -> VERIFY -> won? stop | progressing? act | stagnating? replan
                | regressing? rollback | budget out? escalate
```

The only path back to a human is an `unknown` verdict. Routine progress,
stagnation, and regression are handled by continue / replan / rollback —
no check-ins, no approval prompts.

## Quickstart

```bash
python setup.py                 # scaffold harness/, detect an agent CLI, seed config
# edit harness/state/goal.yaml            (the invariant)
# edit harness/contracts/example.contract.yaml  (maker / success / progress)
python run.py harness/contracts/example.contract.yaml
```

Pick a backend explicitly if you want:

```bash
HARNESS_AGENT=codex python run.py harness/contracts/example.contract.yaml
```

YAML contracts need `pyyaml` (`pip install pyyaml`); `.json` contracts need nothing.

## If you are an agent

Two different jobs bring an agent here — know which one you're doing:

- **You are the maker inside a harness loop** (a contract's `maker.command`
  invoked you): your operating rules are [AGENTS.md](AGENTS.md). Short
  version: read `harness/state/goal.yaml` first, make ONE attempt, check
  `harness/state/rejected_paths.jsonl` before trying a hypothesis, and never
  grade your own work — a deterministic `success.command` decides, not your
  write-up.
- **You are working on this repo itself** (fixing, extending, reviewing):
  host-specific setup lives in `adapters/<host>/`. Run every command under
  [Test](#test) before committing; a change that skips the gate is not done.

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
| `adapters/<host>/` | host adapters: `claude/` (Claude Code skill), `codex/` (Codex AGENTS.md, live-verified: drove a contract to WON). |

## Updating deployed copies

Deploy as a **git clone**, not file copies — git is the version system, so no
custom update machinery exists or is needed:

```bash
git clone https://github.com/n01r1r/autohunt.git ~/tools/autohunt
```

Point host adapters (skill dirs, AGENTS.md references) at the clone instead of
copying files out of it.

- **Am I stale?** `git fetch -q && git rev-list --count HEAD..origin/main` —
  prints commits behind; `0` = current. `setup.py` prints this advisory
  automatically when the scripts live in a clone.
- **Update:** `git pull --ff-only`. Local edits make the ff fail — that IS the
  "not cleanly updatable" signal; resolve by hand.
- **Never mid-run:** the governor never checks or updates itself while a
  contract runs — network dependence and code swap under a running loop would
  break resume determinism. Check at setup/session start; update on human
  trigger only.
- Per-project `harness/` files (goal.yaml, contracts, state) are local state
  the user edits — never update targets. Only scripts + adapters version-track.

If a file-copy deployment is unavoidable, stamp it at copy time
(`git rev-parse HEAD > .version`) and compare against
`git ls-remote https://github.com/n01r1r/autohunt.git HEAD` — no clone needed.

## Test

```bash
python scaffold.py --selftest       # create-all + no-overwrite-on-rerun
python run.py --selftest            # winnable -> WON; unwinnable -> stops at cap
python test_agnostic.py             # Part A: runtime drives an arbitrary backend
python test_governor.py             # resume across crash, classify logic, refusal gates
python test_setup.py                # behind() silent on non-repo / no-upstream / offline
python test_agnostic.py --live      # + Part B: smoke-test installed real agents (costs tokens)
```

Version axis for Part B: `HARNESS_SMOKE_EXTRA` adds pinned backends as raw
command prefixes, e.g.
`HARNESS_SMOKE_EXTRA="npx -y @openai/codex@2.1.0 exec --skip-git-repo-check"`.
Backends are independent (one maker per contract), so versions form a list to
sweep, never a matrix to cross.

CI (`.github/workflows/ci.yml`) runs the deterministic parts on Linux + Windows.

## License

MIT.
