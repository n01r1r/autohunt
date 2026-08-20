# Security model

autohunt runs commands. Treat a **contract as executable configuration**, like
a Makefile — running one is running code.

## What executes, and with your privileges

- `run.py` runs the `maker`, `success`, `progress`, and `rollback_command`
  strings from a contract through a shell (`subprocess ... shell=True`).
- `agent.py` runs the backend named by `$HARNESS_AGENT` /
  `harness/routing/agent.env`; a value that is not a known agent name is run
  verbatim as a command prefix.

So opening a contract, or setting `HARNESS_AGENT`, is equivalent to executing
code. **Only run contracts you wrote or trust.** Do not run a contract from an
untrusted source, and do not point `HARNESS_AGENT` at an untrusted value.

## It is not a sandbox

The maker and the checker share one writable working tree. The separation of
maker from checker (`run.py` is only the checker/governor; the maker is a
separate process) is anti-"agent-grades-its-own-homework" hygiene, **not a
security boundary**. A capable or adversarial maker can:

- edit the checker's own inputs (e.g. rewrite `eval.py` to exit 0), or
  fabricate the artifact `success.command` inspects → a false `WON`;
- write `harness/state/decisions.jsonl` to force a `won` record, or pad it with
  records so `prior >= cap` and every attempt is skipped on the next run.

If the maker is not fully trusted, isolate each attempt: run it in a container
or a throwaway git worktree, and run a **pinned checker from outside the
maker-writable tree**. (`isolation: worktree` in the workflow layer, or your own
container, is the place for this.)

## Known sharp edges (by design, documented)

- **`rollback_command`** runs arbitrary shell. The scaffolded example is
  `git checkout -- .`, which discards *all* uncommitted working-tree changes.
  Opt in knowingly; prefer a run-specific checkpoint or worktree.
- **`state.file`** is not traversal-guarded. An absolute path or `..` writes the
  decisions log outside `cwd`. Keep it inside your project.
- **Windows `.cmd` backends**: a prompt dispatched to a `.cmd`/`.bat` agent shim
  is parsed by `cmd.exe`, so `% & | < >` in the prompt may be expanded or
  mangled. `.exe` backends and all POSIX backends run shell-free and are
  unaffected.

## Safe-use checklist

- [ ] I wrote or reviewed this contract's `maker`/`success`/`progress`/`rollback` commands.
- [ ] `HARNESS_AGENT` / `agent.env` names a backend I trust.
- [ ] For an untrusted or highly capable maker, attempts run isolated with an external checker.
- [ ] `state.file` stays inside the project; `rollback_command` scope is understood.

## Reporting

Open an issue for anything that lets an *unmodified* contract escape this model
(e.g. code execution without the shell commands the author wrote).
