# Parallel Loops: a Slot per Loop, Claims as git-ref leases on the remote

The Tick used to run in the operator's own checkout, guarded by one `flock` in `.git/` so that only one Tick per target repository ran. That made the checkout unusable for anything else while a Tick ran: it changed branches under the operator, and the operator's half-finished state could stop a Tick. It also made the Loop strictly serial. We now let several Loops run at once, on one machine or several, without two of them ever working the same issue, and without disturbing a human authoring stories or editing docs in their own checkout.

## Decisions

- **Every Tick runs in a Slot.** A Slot is a git worktree dedicated to one Loop (`<repo>.ralph-slot-N/` by default). The Tick provisions it on first use and repairs it if it is missing, so schedulers keep launching `ralph.sh` from the target repository and nothing about their install changes. The tooling the Tick runs never comes from the Slot or from the operator's checkout. It runs from an immutable copy of its Tooling Commit (ADR-0009). The operator's checkout is never a Slot. The Tick lock becomes per Slot: an overlapping Tick in the *same* Slot still exits immediately.
- **A Claim is a lease held as a git ref on `origin`.** `refs/ralph/claims/<issue#>` points at a small commit naming the holder (`<host>:<slot>`) and an expiry. Taking, renewing and taking over a Claim are all `git push --force-with-lease` against the value last seen, an atomic compare-and-swap performed by the server. Releasing it deletes the ref. These refs are not branches: they trigger no CI, are not covered by branch protection, and do not show in the GitHub UI. The default lease is 10 minutes, renewed about every 2 minutes (`concurrency.claim_ttl`).
- **A Claim covers any issue Ralph mutates, for one unit of work.** That means a Story for an Iteration, a Negotiation Round, a bounded review wait or an arbitration, and a PRD for its Feature's integration. The Claim is released at the end of the unit, so the next unit may be taken by any Slot on any machine and resumes from the pushed Handoff. There is no affinity between a Story and a Slot.
- **One Slot per Feature by default.** A Feature Story's unit of work also holds its PRD's Claim, so sibling Stories never run in parallel. `concurrency.feature_exclusive: false` lifts that. Orphan Stories hold only their own Claim.
- **Mutations are fenced.** Every mutating `bin/ralph` subcommand (label moves, Handoffs, Attempts, review publication, merges, pushes) first re-verifies, by compare-and-swap, that the caller's Slot (`RALPH_SLOT`) still holds the issue's Claim. This fences the agent's own calls (`--checkpoint`, `--record-attempt`, ...) as well as the Tick's. A refusal ends the unit with a dedicated exit code: no Attempt is spent and nothing more is written. A subcommand run by hand without `RALPH_SLOT` is not fenced.
- **Concurrency is a per-machine operator choice, under a committed ceiling.** Each Slot is a systemd template instance (`ralph@N.timer`), so a burst is enabling one more instance. `.ralph.yml` commits `concurrency.max_slots` (default 1), which no machine may exceed. Slots and Claims are always on, even single-threaded, so the claim path is exercised on every Tick and a second machine is coordinated without any configuration.
- **Branches checked out elsewhere.** A Slot detaches HEAD whenever it releases a Claim, and at the start of every Tick, so another Slot can always check out a Story's branch. If the operator's own checkout holds a Story's branch, the Slot skips that Story for this Tick and logs it, spending no Attempt.

## Considered options

- *A single machine first, distributed later.* Rejected. Cross-machine Loops are wanted, and a lease on the shared remote costs barely more than a local `flock` while making one mechanism serve both cases.
- *Claims on GitHub issue fields (an assignee, a label, a claim comment).* Rejected. Issues have no compare-and-swap, so every claim would be read-write-reread with tie-breaks. Every Slot runs as the same account, so an assignee cannot tell Slots apart. A claim label would also add backlog vocabulary that ADR-0002 keeps fixed. Labels stay the backlog's truth and Claims are runtime coordination.
- *Story affinity (a Story stays with the Slot that started it).* Rejected. A Story would stall whenever its owner's machine is off, and Handoffs already make every Story resumable anywhere.
- *Separate clones instead of worktrees.* Rejected for the per-machine Slot. Worktrees share history and refs at no disk cost. Clones would only avoid git's "checked out in another worktree" refusal, which detaching on release already removes.
- *Fencing only in `ralph.sh`.* Rejected. The agent itself calls mutating subcommands, so a Slot that had silently lost its Claim, such as a suspended laptop, could still post a Handoff or record an Attempt.

## Consequences

- Every Tick makes a few small pushes to `refs/ralph/claims/*`, so the pushing credential needs nothing beyond the push access it already has.
- Alternation state moves to the git common dir and is shared by the Slots of one machine. Balance is kept per machine, which is acceptable because alternation is fairness, not correctness.
- Every Slot learns about an exhausted session window for itself. Nothing shares an "exhausted until" marker.
- The scenario tier models several Slots against the one bare remote, with deterministic interleaving. A new invariant checks that no Slot mutates an issue or pushes for it without holding its Claim.
- Which tooling a Slot runs, including when `ai-utils` is its own target repository, is decided by ADR-0009, not by what the Slot has checked out.
