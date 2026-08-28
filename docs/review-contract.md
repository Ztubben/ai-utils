# The structured-review contract

A Review Agent does not talk to GitHub. It emits one machine-readable result per
Negotiation Round; a trusted Ralph-side wrapper validates that result and, only
if it validates, renders it as an ordinary GitHub review. This document is the
contract that gate enforces.

The contract is versioned by the `contract` field. The current version is
**`ralph-review/v1`**, defined by `schema/review.schema.json` and enforced by
`ralph --validate-review PAYLOAD [DIFF]`. A reader that does not recognise the
exact version string refuses the payload rather than guessing at its shape.

## Why validation is a refusal, never a repair

The reviewing model is read-only and holds no GitHub credential, so the only way
its judgement reaches a pull request is through this payload. A payload that
does not validate is rejected whole: nothing in it is posted, no round is
consumed, and the wrapper reports the offending field paths. Partially trusting
a malformed result is exactly how a hallucinated finding would land in a review
thread.

## The result

| Field | Required | Meaning |
| --- | --- | --- |
| `contract` | yes | Exactly `ralph-review/v1`. |
| `verdict` | yes | `approve`, `request_changes`, or `comment` — rendered as the corresponding GitHub review. |
| `head` | yes | The exact 40-character commit the review judged. A result for a head that is no longer current is discarded, not posted. |
| `model` | yes | The exact reviewing model identity, as recorded on the Story's `model:review:` label — never a profile key, which is configuration-local. |
| `round` | yes | Which Negotiation Round produced the result, counting from 1. |
| `summary` | yes | Prose for the review body: what was reviewed, and why the verdict follows. |
| `findings` | yes | Every objection raised, blocking or not. May be empty. |

The verdict and the findings must agree: `request_changes` carries at least one
blocking finding, and `approve` and `comment` carry none. A blocker that does
not request changes would be a finding nothing acts on; requested changes with
no blocker would block a pull request for a preference.

## A finding

| Field | Required | Meaning |
| --- | --- | --- |
| `id` | yes | Stable identifier, unique within the result. Fixes and disputes cite it across commits and rounds. |
| `blocking` | yes | Whether this finding blocks the pull request. Explicit; see below. |
| `category` | yes | Why it blocks, or why it does not. |
| `claim` | yes | What is wrong, stated once. |
| `evidence` | yes | What in the diff, the checkout, or the checks supports the claim. |
| `requirement` | yes | The acceptance criterion, documented rule, or decision the claim is measured against — or an explicit statement that there is none, which only a non-blocking finding may say. |
| `verification` | yes | How the Implementation Agent can check the claim itself: the test to run, the input to try, the caller to inspect. |
| `location` | no | `path` plus `line` (and optionally `end_line`) where the finding applies. |

`location` is present when the finding applies to changed lines, and is rendered
as an inline review thread. A cross-cutting finding — one about the change as a
whole, or about something absent from it — carries no location and is rendered
in the review body instead. Both are first-class; a reviewer must not invent a
location to make a cross-cutting point fit an inline thread.

Given the reviewed base/head diff, the validator checks each location against
it: the path must be one the diff touched, and the line (and `end_line`) must be
a line the diff shows on the new side. Context lines inside a hunk count,
because GitHub accepts an inline thread on them and a finding about an added
line often reads best on the line above it. Removed lines do not: they no longer
exist at the reviewed head.

## The blocking policy

Blocking is **declared, never inferred**. `blocking` is required and has no
default: a finding that omits it has not been classified, and the payload is
rejected. The `category` must then agree with the classification, so a
preference cannot be dressed up as a blocker and a defect cannot be waved
through as a preference.

A finding may block only for these reasons:

| Blocking category | Blocks because |
| --- | --- |
| `acceptance_criteria` | The change does not satisfy an acceptance criterion of the Story. |
| `defect` | A demonstrable defect: wrong output, a crash, a broken invariant, with the evidence to show it. |
| `safety_regression` | The change makes a safety-relevant behaviour worse. |
| `explicit_rule` | It violates a rule the repository states explicitly — `AGENTS.md`, an ADR, `CONTEXT.md` vocabulary. |
| `missing_tests` | Behaviour the Story adds is materially untested, not merely testable in more ways. |
| `scope_creep` | The change carries risky work the Story did not ask for. |

Everything else is non-blocking, and is recorded so the author can take it or
leave it:

| Non-blocking category | Why it never blocks |
| --- | --- |
| `style_preference` | Taste, naming, and formatting the repository does not mandate. |
| `speculative_improvement` | A change that might pay off later, unsupported by present evidence. |
| `preexisting_issue` | A problem the reviewed diff neither introduced nor worsened. |

Deterministic checks — build, lint, format, tests — belong to the target
repository's CI, which owns their verdict. The reviewer interprets evidence; it
does not re-run the gate in prose.

## Size

One result renders into one GitHub review body, and GitHub caps that at 65,536
characters. A payload is therefore rejected above **60000 bytes**, leaving the
margin to Ralph's own framing, and above 50 findings. Individual fields carry
their own caps in the schema. A reviewer that restates the diff back at the
author hits these limits; one that reports what it found does not.

## How a validated result is rendered

`ralph --render-review REVIEW PR [DIFF]` re-validates the result and then posts
it. Located findings become inline review threads anchored on the new side of
the reviewed commit; a multi-line finding is anchored at its last line with
`start_line` above it. Cross-cutting findings are written into the review body
under the header naming the reviewing model, the round, and the reviewed
commit. Nothing appears twice: a located finding is never repeated in the body.

The review is always posted with `event: COMMENT`. GitHub refuses APPROVE and
REQUEST_CHANGES on a pull request the same account authored, and Ralph's pull
requests are opened with the operator's own credential, so a verdict-shaped
event would fail exactly when Ralph needs it most. The verdict is carried
instead by one stable commit-status context, **`ralph/model-review`**:
`request_changes` sets it to `failure`, `approve` and `comment` set it to
`success`, and its description repeats the round, the model, and the number of
blocking findings. A target repository requires that one context in branch
protection and never has to track a per-round or per-model check name.

A result is bound to the commit it judged. If the pull request head has moved
on, the rendering is refused before any request is made: nothing is posted, the
check is left alone, and the reason names both commits. Stale findings describe
code that is no longer there.

## Validating a payload

```
ralph --validate-review review.json head.diff
```

`PAYLOAD` may be `-` to read stdin. `DIFF` is the reviewed base/head diff; it is
optional, and without it locations are checked for shape but not for range.
Exit 0 means the result may be posted, 1 means it may not (the offending field
paths are printed to stderr, e.g. `findings/1/id: duplicate finding id 'F-1'`),
and 2 means the input could not be read.
