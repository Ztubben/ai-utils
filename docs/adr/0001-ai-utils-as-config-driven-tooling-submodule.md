# ai-utils is a config-driven tooling submodule; superprojects own their specifics

`ai-utils` ships only reusable, project-agnostic machinery (the Ralph Loop script, agent prompt, and issue-formatting skill) and is normally mounted as a git submodule. It carries no consuming project's source, issues, or CI, and Ralph only ever modifies its **target repository** — which, for a submodule mount, is the superproject (see the amendment below).

Anything project-specific is declared by the superproject in a single committed **`.ralph.yml`** at its root, validated at tick start against a JSON-schema shipped in `ai-utils`; an invalid or missing config fails loud rather than defaulting. It carries: a `version` (contract version), ordered named **gating** steps Ralph runs locally, the **branching** strategy (base branch — default `develop` — branch pattern, AFK auto-merge policy; Ralph integrates into the base branch and never touches `main`, and the human-owned `develop → main` promotion and its integration/regression bench testing are out of scope), **limits** (`max_attempts`, `circuit_breaker`), and the `notify` handle. The canonical `state:`/`type:`/`prio:` label scheme (ADR-0002) is **mandated, not overridable** — chosen for simplicity over drop-in flexibility with an existing label vocabulary.

We chose this config-driven split over baking conventions into the tooling because the same tooling must serve many embedded projects with different toolchains and branching rules; the cost is that every superproject must supply a valid config, and the config schema becomes a contract we can't change lightly.

## Amendment (2026-08-27, #43): the target-repository model

The original invariant tied Ralph's target to an embedding superproject, excluding `ai-utils`
itself. It conflated two separate things: *which repository Ralph modifies*, and *which
repository owns project-specific configuration*. Only the second is load-bearing. We replace the first with a
**target repository** model: Ralph works the backlog of, and modifies, exactly one target
repository, and that repository owns its `.ralph.yml`, gating steps, CI, credentials, model
profiles, protected control-plane policy, and notify handle.

Two arrangements follow from one rule:

- **Submodule mount.** The target repository is the superproject. Ralph never mutates the mounted
  `ai-utils` checkout: the tooling is read from it, never written to it, and a superproject's
  choices never flow back into the shipped machinery.
- **Checkout root.** When `ai-utils` is itself the checkout root — not mounted inside anything —
  it is its own target repository and may target itself, so the reusable tooling can be developed
  through its own backlog (self-hosting).

The configuration boundary is unchanged and is what keeps self-hosting honest: any
target-repository configuration `ai-utils` carries for its own loop is one *instance* of the
shipped contracts, sitting alongside them, and is never a default inherited by embedding
repositories. Deciding the two cases by checkout topology rather than by a flag costs a little
ambiguity for a reader who sees only one repository, which is why the vocabulary lives in
`CONTEXT.md` (Target Repository, Superproject).
