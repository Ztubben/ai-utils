# The Tick runs frozen tooling: an immutable copy of one Tooling Commit

A Tick used to run the Ralph tooling it found next to itself. In a Superproject, that is the mounted submodule, which can move while a Tick works. When `ai-utils` is its own target repository, it is the very checkout the Implementation Agent is editing, so a Tick building a Story that changes `bin/ralph.sh` or `lib/` ran that Story's half-written code. Bash even reads a running script from disk as it goes. We now decide, once at the start of every Tick, which commit of `ai-utils` runs (the **Tooling Commit**), and run it from a copy of that commit that never changes.

## Decisions

- **The Tooling Commit.** In a Superproject it is the gitlink pinned at the `ai-utils` path in `origin/<base>`'s tree, so upgrading stays a person bumping the pin (ADR-0007). When `ai-utils` is its own target repository it is `origin/main` as read at Tick start, so the Loop upgrades itself, at most once per Tick, to code that passed CI and review and was merged. A Feature never runs its own unfinished code, because its Stories merge into its Feature Branch, not `main` (ADR-0006).
- **Immutable per-commit copies.** The Tick makes sure a detached worktree of the Tooling Commit exists in the git common directory, creating it once and never modifying it, and re-execs that copy's Tick script. An upgrade switches to a different copy and never edits one in place, so no running Tick, in any Slot (ADR-0008), ever sees its code change. A copy is pruned once it is neither the current Tooling Commit nor the last-known-good one and no Tick runs from it.
- **The agent runs the same copy.** The frozen copy's `bin/` comes first on the `PATH` of every agent the Tick launches, so the agent's own `ralph` calls run the frozen tooling too, never the checkout it edits.
- **Last-known-good fallback, when self-hosting only.** A new `origin/main` becomes the running Tooling Commit only once it passes the Tick's preflight (config check, subcommand preflight, dry run). Otherwise the Tick runs the last commit that passed, which is recorded in the git common directory, and retries the new one on every later Tick. A fallback opens one machine-managed issue tagging `notify.github`, naming the failing commit, the failed step and its output tail, and the commit running instead. Repeat failures update that issue, and it is closed with a comment once `main` passes again. It carries no `state:` label and is never work.
- **No fallback in a Superproject.** A pin that fails preflight is refused loudly, naming the pinned commit and the failed step, as the Superproject pointer check (#96) already refuses drift. A person chose that pin, and the committed pin is the contract. Running any other version would break the pointer check's meaning. `tooling.allow_pointer_drift` remains the opt-out.

## Considered options

- *A pin ref the operator moves, for self-hosting.* Rejected. It reintroduces the manual step this decision exists to remove, and Feature Branches already keep half-built work off `main`.
- *A commit pinned in ai-utils' own `.ralph.yml`.* Rejected for the same reason: every upgrade becomes a pull request.
- *One mutable tooling worktree per machine, or per Slot.* Rejected. Upgrading a shared copy changes it under another Slot's running Tick, and a per-Slot copy only avoids that by careful ordering. Immutability makes "never changes mid-Tick" true by construction, and keeps last-known-good one directory away.
- *Making all of `lib/**` Control Plane in ai-utils.* Rejected. It would human-gate nearly every Story in the repository. The fallback catches what would otherwise brick the Loop, a crash or a refusal, without slowing anything down.

## Consequences

- A bug on `main` that does not crash, such as selection quietly finding no work, is not caught by the fallback. CI before the merge and the Tick log remain the defence.
- The freeze has to be built before Ralph can safely build its own tooling, so it is implemented by hand. Every later Feature of `ai-utils` is built by the Loop.
- This replaces ADR-0008's re-exec from the Slot's own submodule checkout. That checkout could move while the Slot worked, and an immutable copy cannot.
