---
name: harness-init
description: Initialize, validate, and run a portable autohunt agent harness. Use when the user asks to scaffold an agent harness, create a deterministic agent loop, configure a completion contract, or run an autohunt task.
---

# Harness init

Use the installed CLI; do not hand-write generated files or embed the clone's
absolute path in a project.

1. Run `autohunt doctor <target>` and report failed prerequisites.
2. Run `autohunt init <target> --agent codex`. Existing files are preserved.
3. Fill `harness/state/goal.yaml` and a task contract.
4. Run `autohunt validate <contract> --cwd <target>` before execution.
5. Summarize maker, checker, protected paths, budget, and any snapshot or
   rollback command. Do not enable broad rollback commands on a dirty tree.
6. Run `autohunt run <contract> --cwd <target>` only within the authorized
   scope, then report the deterministic result and state path.

The maker performs one attempt. The checker determines success. Record rejected
approaches in `harness/state/rejected_paths.jsonl`; do not rediscover them.
