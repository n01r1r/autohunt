#!/usr/bin/env python3
"""Scaffold a portable agent harness into a target folder (default: cwd).

Deterministic file writer - refuses to overwrite existing files, so it is safe
to re-run (init-like). Prints what it created and what it skipped.

Usage:
    python scaffold.py [target_dir]
    python scaffold.py --selftest      # scaffold into a temp dir and assert
"""
from __future__ import annotations
import sys
from pathlib import Path

# name -> file body. Dirs are implied by the path. Empty body = empty file.
FILES: dict[str, str] = {
    "harness/README.md": """# Agent harness

Portable harness: invariant = workflow semantics; models/hosts are backends.
Rule: don't invoke an agent when cache/code/tool does the job.

Executor ladder (cheapest sufficient wins):
    cache -> deterministic code -> tool/API -> cheap model -> strong model
    -> agent loop -> human

The agent backend is agent-agnostic: the maker runs through ../agent.py, which
picks a CLI from $HARNESS_AGENT / routing/agent.env / PATH. Swap claude for
gemini/codex/aider/llm without editing a contract.

Layout:
    state/        current run status + memory across sessions (externalized)
    contracts/    CompletionContract per repeated task (goal/verify/progress/budget/recovery)
    routing/      which executor / agent backend runs which op
    principles/   canonical rules; host adapters (CLAUDE.md/AGENTS.md) generate from here

Loop is NOT hand-written. Write a contract; the generic runtime derives it:
    ACT -> VERIFY -> success? stop / progressing? act / stagnating? replan
    / regressing? rollback / budget out? escalate

Run a contract (governor never calls a model; it runs maker + deterministic check):
    python run.py harness/contracts/<x>.contract.yaml

Autonomous: it runs to the budget cap unattended and escalates to a human only
on an `unknown` verdict (invalid/missing signal) or `tampered` (the maker
edited a `protect:`-listed file). A metric tie is `plateau`/`stagnation`,
handled by continue/replan - never a human question. Hung commands are
tree-killed at `timeout_sec`. principles/operating-policy.md = the cost moves
that are host habits (MCP audit, cache prefix order, effort dial), not code.
""",
    "harness/state/goal.yaml": """# What this run must achieve. The invariant the agent must not drift from.
goal: ""            # e.g. "reduce val RMSE below 0.04"
constraints:        # explicit boundaries the agent must preserve
  - ""              # e.g. "no architecture change; loss only"
non_goals:
  - ""
""",
    "harness/state/decisions.jsonl": "",
    "harness/state/rejected_paths.jsonl": "",
    "harness/routing/agent.env": """# Which agent backend agent.py drives. Uncomment / edit one line.
# Overridden by the $HARNESS_AGENT environment variable if set.
# A value that is not a known name is run verbatim as a command prefix.
# Resolved against the maker's working directory - i.e. THIS project's
# harness/routing/agent.env, because run.py runs the maker with cwd = the
# target project. This file is target-project state, not clone state.
# HARNESS_AGENT=claude
# HARNESS_AGENT=gemini
# HARNESS_AGENT=codex
""",
    "harness/contracts/example.contract.yaml": """# One CompletionContract per repeated task. The runtime builds the loop from this.
# You write these blocks; you do NOT write a loop body.
# Script paths are absolute (baked in at scaffold time) so the contract works
# when the harness scripts live in a separate clone (clone-reference deploy).
# Run it FROM THIS PROJECT'S ROOT:  python "__HARNESS_ROOT__/run.py" harness/contracts/example.contract.yaml

goal:
  metric: rmse
  direction: decrease        # or increase
  # mode: improve            # no done-threshold: hunt the best metric until the
                             # budget ends (progress.command required; the run
                             # exits IMPROVED if best beat the baseline).

maker:                       # ONE attempt of work. A SEPARATE process (maker != checker).
  # agent-agnostic: agent.py picks the backend from HARNESS_AGENT / routing/agent.env / PATH.
  command: "python \\"__HARNESS_ROOT__/agent.py\\" 'read harness/state/goal.yaml; make one fix toward the goal'"
  timeout_sec: 600           # hung maker = process tree killed, exit 124 recorded, loop continues
  # git no-op detection: have the maker commit each attempt, then use commit
  # count as the progress metric (goal.direction: increase). An attempt that
  # changes nothing repeats the count -> classify() flags stagnation -> replan.
  # command: "python \\"__HARNESS_ROOT__/agent.py\\" '...' && git add -A && git commit -qm attempt || true"

success:                     # WHO decides done. Deterministic > model self-eval. exit 0 = pass.
  command: "python eval.py --check-threshold"
  timeout_sec: 120

progress:                    # success vs progress are separate. 3->3->3 is stagnation.
  command: "python eval.py --print-metric"   # prints ONE finite number (NaN/inf rejected)
  # command: "git rev-list --count HEAD"     # pairs with the committing maker above
  timeout_sec: 120
  stagnation_window: 2
  # The governor measures this ONCE before the first maker run (attempt 0 =
  # baseline), then compares every attempt against the incumbent best.

protect:                     # checker integrity: the maker may not edit its own judge.
  - eval.py                  # hashed before/after each maker run; change -> tampered -> escalate

budget:                      # move #2 kill switch. attempts is a HARD cap.
  attempts: 6
  # tokens: 500000           # enforce in your maker command / CLI
  # wall_time_min: 30

failure_policy:              # verdict -> action: continue | replan | rollback | escalate
  regression: rollback
  stagnation: replan
  unknown: escalate          # ambiguous signal -> human (tampered escalates by default)
  # NOTE: snapshot/rollback must NEVER touch harness/state - the ledger has to
  # survive resets or resume (durable state) breaks. Hence the exclude pathspec.
  # (double quotes: single-quoted args break under Windows cmd.exe)
  snapshot_command: "git add -A -- . \\":(exclude)harness/state\\" && git commit -qm snapshot || true"   # runs at baseline + every new best
  rollback_command: "git checkout -- . \\":(exclude)harness/state\\""   # restore the incumbent best (last snapshot)
  # Autonomy envelope: run on a dedicated branch (e.g. hunt/<tag>) so snapshot
  # and rollback resets stay scoped there. This contract IS the operator's
  # pre-authorization for exactly these commands - nothing broader.
""",
    "harness/routing/executors.yaml": """# Bind each semantic op to the cheapest sufficient executor.
# Numbers/metrics/thresholds = deterministic. Model only for judgment.

select_best_config:      { prefer: [deterministic] }   # compute from CSV, never eyeball
run_tests:               { prefer: [deterministic] }
parse_paper:             { prefer: [cache, cheap_model] }
retrieve_sources:        { prefer: [search_tool] }
first_principles:        { prefer: [strong_model] }
debug_unknown_failure:   { prefer: [coding_agent] }     # genuine open-ended only
""",
    "harness/principles/research.md": """# Canonical research principles (host-agnostic)

- Externalize state; do not rely on chat history or vendor memory.
- Record rejected hypotheses in state/rejected_paths.jsonl so another
  session does not rediscover them.
- Verify with environment/deterministic signal before model self-judgment
  (self-rated "I improved it" is a progress mirage).
- Numbers are code, judgment is model.
- Autonomy bias: run unattended to the budget cap. Human is the LAST rung of
  the executor ladder, reached only when the signal is ambiguous (an `unknown`
  verdict) - not for routine progress, stagnation, or regression, which the
  governor handles by continue / replan / rollback.

# Host adapters (CLAUDE.md / AGENTS.md / GEMINI.md) should be generated from
# this file, never maintained independently.
""",
    "harness/principles/operating-policy.md": """# Operating policy - the cost/reliability moves that are HABITS, not code.
# (The code ones - kill switch, maker/checker, durable state - live in run.py.)

## #1 Should it even be an agent? (preflight gate, before writing a contract)
Answer four; four yeses or don't build an agent:
  [ ] repeats?         (else: one-shot prompt)
  [ ] outcome checkable? -> if you can't write success.command, it's a vibe. STOP.
  [ ] more than one step?
  [ ] survives a wrong turn?

## #4 MCP / tool token tax   (host-specific; example: Claude Code)
Every connected tool server reloads ALL its tool defs into context EVERY turn.
Wire only what the task needs; drop the rest (Claude Code: /mcp this session).
Generic rule: keep the tool surface minimal - the model already knows git/curl/docker.

## #5 Prompt cache = most of the bill   (any provider with prefix caching)
Cache matches the prefix byte-for-byte from the start. STABLE first, DYNAMIC last.
One early timestamp or reordered tool block busts the whole prefix and re-bills full.
Default TTL is short (minutes) - long gaps re-bill. Watch your context/cache stats.

## #6 Turn effort DOWN   (host-specific dial; example: Claude Code /effort)
High reasoning effort can burn many times the tokens of low. Start default, step
to medium/low, watch the eval - most tasks hold quality at half the spend. Save
the top tier for genuinely hard long jobs. A newer model at a lower dial is often
the cheapest win.

## #3 (habit part) Context is a budget
Don't load a big file to ask one question - grep it. Push heavy work to a subagent
and treat it as a filter (4000 log lines in, 2-line summary out). Compact at a
milestone you pick, before auto-compaction fires.

## #7 (habit part) Don't read the agent's write-up
Its prose "reads smart, teaches nothing" - best hiding spot for a wrong answer.
Trust the checkable artifact (test result, metric, visualization), not the narration.
Every failure -> freeze as a locked eval so no future change silently regresses it.
""",
}


def scaffold(target: Path) -> tuple[list[str], list[str]]:
    # Bake the scripts' real location into generated files so contracts work
    # when the target project is not the clone (clone-reference deploy).
    root = Path(__file__).resolve().parent.as_posix()
    created, skipped = [], []
    for rel, body in FILES.items():
        path = target / rel
        if path.exists():
            skipped.append(rel)
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body.replace("__HARNESS_ROOT__", root), encoding="utf-8")
        created.append(rel)
    return created, skipped


def _selftest() -> None:
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        created, skipped = scaffold(root)
        assert len(created) == len(FILES) and not skipped, "first run should create all"
        contract = (root / "harness/contracts/example.contract.yaml").read_text(encoding="utf-8")
        assert "__HARNESS_ROOT__" not in contract, "placeholder must be substituted"
        assert Path(__file__).resolve().parent.as_posix() in contract, \
            "contract must reference the scripts' real location (clone-reference deploy)"
        created2, skipped2 = scaffold(root)
        assert not created2 and len(skipped2) == len(FILES), "re-run must not overwrite"
    print("selftest ok")


def main() -> int:
    args = sys.argv[1:]
    if args and args[0] == "--selftest":
        _selftest()
        return 0
    target = Path(args[0]).resolve() if args else Path.cwd()
    created, skipped = scaffold(target)
    print(f"harness -> {target}")
    for f in created:
        print(f"  created  {f}")
    for f in skipped:
        print(f"  skipped  {f} (exists)")
    if not created:
        print("nothing to do; edit state/goal.yaml and contracts/ to start.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
