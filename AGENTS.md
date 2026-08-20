# AGENTS.md — operating rules for any agent working in this repo

This file is the host-agnostic adapter. It is generated from
`harness/principles/*` (once scaffolded); do not maintain rules independently
here — change the principles and regenerate.

You are one executor on a ladder, not the default tool:

    cache -> deterministic code -> tool/API -> cheap model -> strong model
    -> agent loop -> human

Climb only as far as needed. If cache, a script, or a tool answers, do that.

## When you are the maker

- Read `harness/state/goal.yaml` first. It is the invariant. Do not drift from
  its `constraints`; do not pursue its `non_goals`.
- Make **one** attempt of work per invocation. You are a separate process from
  the checker — you do not decide whether you succeeded. A deterministic
  `success.command` does.
- Before trying a hypothesis, check `harness/state/rejected_paths.jsonl`. If it
  is there, it already failed — pick something else.
- When you rule an approach out, append it to `rejected_paths.jsonl` so no
  future session rediscovers it.

## Verification beats self-report

Never claim "I improved it." Numbers are code; judgment is model. The harness
trusts the checkable artifact (exit code, metric, test), not your write-up.

## Autonomy

The governor runs to the budget cap unattended. It escalates to a human only on
an `unknown` verdict. Do not ask a human for routine progress, stagnation, or
regression — those are handled by continue / replan / rollback.

## Cost habits

Keep the tool surface minimal. Put stable context first, dynamic last (prefix
caching). Prefer a lower reasoning effort until the eval says you need more.
See `harness/principles/operating-policy.md`.
