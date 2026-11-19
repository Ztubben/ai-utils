# A Feature pull request inherits its Story pull requests' model reviews

Branch protection on the base branch requires `ralph/model-review` (PRD #42). Since #171 the completion pass waits for that context on the Feature pull request before merging. Nothing could produce it, though. The review waiver skips `feature/*` heads because they are Ralph's own. Ralph runs a review round only on Story pull requests. A complete Feature could therefore reach the base only through an admin bypass of branch protection, which is how PR #170 landed. This amends the model-review contract (PRD #42) and ADR-0006's decision that the Feature integration pull request is not model-reviewed.

## Decisions

- **The Feature pull request inherits its Stories' reviews. It is never re-reviewed and never waived.** Before merging, the completion pass confirms that the Feature's diff is made only of reviewed work. Then it writes `ralph/model-review` = success itself, on the exact Feature pull request head it is about to merge with `--match-head-commit`. The status description names the Story pull requests the review is inherited from. The pass never writes the context on any other head.
- **Every Story was reviewed.** Each Story of the Feature, open or closed, must have merged into the Feature branch through a Ralph-managed pull request whose merged head carried a passing `ralph/model-review`, or a recorded human approval of that head (the same two things that satisfy a Story's own completion gate).
- **Integration changed nothing.** The Feature head has been rebased and autosquashed, so its commits are never the commits a reviewer saw. Inheritance is therefore judged by patch, not by commit: every commit on the rebased Feature branch (`origin/<base>..feature`) must have the same `git patch-id --stable` as some reviewed Story pull request's merged change. There are two ways to fail this. A commit that no Story pull request carried has no counterpart. A Story change that the rebase onto the base had to alter no longer matches its counterpart. Both look the same from here (a patch nobody reviewed), and both are named in the refusal by short sha and subject.
- **Failing either check writes nothing.** The Feature takes the existing Feature-level blocker path (ADR-0006): a PRD comment naming the offending Stories and commits, `ready-for-human`, and no circuit breaker. A human resolves it either by taking the named work through a reviewed Story pull request or by reviewing the Feature head themselves, then removes `ready-for-human` to retry.
- **Unread facts never inherit.** A backlog that was not read, or a `gh`/`git` call that fails, refuses. It is never read as "a Feature with nothing to review".

## Considered options

- *A review round on the Feature pull request.* Rejected. It is an extra, large review of work that has already been reviewed, and its findings would arrive after every Story is closed, when there is nowhere left to fix them.
- *Widening the waiver to `feature/*` heads.* Rejected. It skips the gate rather than satisfying it, and anyone can name a branch `feature/...`.
- *Matching by commit id, or trusting that the Feature branch only received Story merges.* Rejected. The completion rebase rewrites every commit id, and a direct push to the Feature branch would be indistinguishable from a reviewed merge.

## Consequences

- A Story pull request is compared as one patch, its whole diff. That matches the default squash Story merge (`branching.afk_merge: squash`), which lands exactly one commit per Story. A Story merged with `rebase` as several commits has no single-commit counterpart, so it fails closed and asks a human.
- A rebase that only shifts line numbers still matches, because `patch-id --stable` ignores them. A rebase that resolves a conflict inside a Story's change does not match, and that is intended: the resolved code is code nobody reviewed.
- The pass now reads merged Story pull requests (`gh pr list --state merged --base <feature>`) and their diffs. Test harnesses that drive a Feature to completion must answer those calls.
