"""Prompt contracts per role (#92, PRD #85).

Each phase an agent can be launched for has instructions its prompt must carry
and instructions it must never carry.  They are checked against the prompt the
scripted agent *actually received* in a scenario -- template plus the context
bundle the Loop assembled -- never against a template alone, because the
defect behind autopilot_controller PR #94 lived in the composition: the
reviewer's bundle, reused for the responder, told the responder "the checkout
may be explored read-only ... do not create commits", and codex obeyed it.

Checked as the `prompt-contract` loop invariant, so every scenario checks every
prompt it records.
"""

# The bundle's read-only line, and the review prompt's own guardrail.
BUNDLE_READ_ONLY = "The checkout may be explored read-only"
BUNDLE_NO_MUTATION = "Do not edit files, create commits, push, or mutate GitHub"
REVIEW_READ_ONLY = "You are **read-only**"
REVIEW_NO_CREDENTIAL = "You hold **no GitHub credential**"
# Written for whoever *judges* a later round; handing it to the model being
# judged is handing it someone else's instructions (#56).
REVIEWER_SCOPE = "## Scope of This Round"

NO_READ_ONLY = [BUNDLE_READ_ONLY, BUNDLE_NO_MUTATION, REVIEW_READ_ONLY]

CONTRACTS = {
    "iteration": {
        "required": ["# Ralph Iteration Prompt", "Next action:", "RALPH-STORY-COMPLETE",
                     "**Never rewrite history.**", "failed **Attempt**"],
        "forbidden": NO_READ_ONLY,
    },
    "review": {
        "required": ["# Ralph Review Prompt", REVIEW_READ_ONLY, REVIEW_NO_CREDENTIAL,
                     BUNDLE_READ_ONLY, BUNDLE_NO_MUTATION, "Exact head commit:",
                     "Review round:"],
        "forbidden": ["RALPH-STORY-COMPLETE"],
    },
    "response": {
        "required": ["# Ralph Response Prompt", "## Append-only, always", "**new commits**",
                     "## Open findings", "Exact head commit:", "answers nothing"],
        "forbidden": NO_READ_ONLY + [REVIEWER_SCOPE],
    },
    "arbitration": {
        "required": ["# Ralph Human Arbitration Prompt", "## Append-only, always",
                     "**new commits**"],
        "forbidden": NO_READ_ONLY + [REVIEWER_SCOPE],
    },
}


def breaches(phase, prompt):
    """[(kind, phrase)] the prompt breaks for *phase*; [] when it holds."""
    contract = CONTRACTS.get(phase)
    if contract is None:
        return [("unknown-phase", phase)]
    return ([("missing", p) for p in contract["required"] if p not in prompt]
            + [("forbidden", p) for p in contract["forbidden"] if p in prompt])
