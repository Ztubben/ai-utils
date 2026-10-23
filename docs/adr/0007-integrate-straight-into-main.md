# Integrate straight into main; no develop, no promotion step

ai-utils retires `develop` and renames `master` to `main`. `main` is the only long-lived branch and the default branch. Ralph integrates into `main` and may auto-merge there on ADR-0006's terms: an Orphan Story squash-merges, a complete Feature lands by rebase. There is no promotion step. `main` carries the protection `develop` had: the `test` and `ralph/model-review` checks are required, no approving review is, `enforce_admins` stays false, and history is linear.

autopilot_controller made the same change first, in its ADR-0011 (PR #153). This record follows that precedent.

## Why

- **The promotion checked nothing, so it drifted.** Promoting `develop` → `master` was a human chore that ran no gate of its own. It got done badly: `master` and `develop` ended up carrying the same commits twice, as rebased copies with identical trees and divergent history, and every promotion either conflicted or needed a history-sync pull request (#81, #104).
- **`Closes #N` only fires on merges into the default branch.** Ralph merged into `develop`, so issues stayed open after their fix had landed (#86–#100 were fixed on `master` and still open).
- **The superproject's pointer bump is the real release gate.** No superproject follows an ai-utils branch. Each one pins a commit, so a bad commit on ai-utils' trunk reaches no one until a person bumps that pin. #96 enforces this at Tick time: a Tick refuses an ai-utils that differs from the committed pointer.
- **The trunk is named `main` so Ralph's guards name it.** `PROTECTED_BRANCH` (`lib/ralph_handoff.py`, `lib/ralph_init.py`) and the schema's default base are both `main`. A trunk under another name would sit outside the Handoff guard.

## Considered options

- *Keep `master` as the trunk with `base: master`*: rejected. The Handoff guard names `main`, so the trunk would be unguarded.
- *Keep `develop` and automate the promotion*: rejected. It keeps a branch whose only job is a gate that checks nothing.
- *A person on every merge into `main`*: rejected. The pin bump already puts a person between ai-utils and every consumer, and a required approval would human-gate every AFK Story (Ralph cannot approve its own pull requests).

## Consequences

- Named releases, if ever needed, are tags on `main`. No release branch is kept.
- A superproject that wants a change bumps its pin. Nothing in a superproject names an ai-utils branch, so the rename changes no superproject.
- ai-utils' own instance (`.ralph.yml`, `.github/workflows/test.yml`, README's *Self-hosting* section) names `main`. Historical records (`ralph/prd.json`, `ralph/progress.txt`, closed issues, and the unamended text of ADR-0001 and ADR-0006) still say `develop` and stay as written.
