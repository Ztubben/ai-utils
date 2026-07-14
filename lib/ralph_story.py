"""Validate a GitHub issue against the canonical Ralph backlog story shape.

The ralph-story formatting skill (a specialization of `to-issues`) emits
stories as GitHub issues encoded in labels + body conventions (ADR-0002).
This is the pure checker for that shape: `validate_story` takes a story dict
shaped like `gh issue view --json number,title,labels,body` output and returns
a StoryResult carrying ok/errors and the normalized fields the selection
engine (US-004) will consume. No network, no side effects.

Canonical shape (ADR-0002, CONTEXT.md):
  - exactly one prio:N label (lower = higher priority)
  - a `Depends on:` body line (`None` or `#12, #34`)
  - a `Parent:` body line linking the story to its Feature's PRD issue
    (`Parent: #N`), or `Parent: None` for an Orphan Story
  - terminology standardized on HIL, never HITL
A PRD issue (carries the `prd` label) is not a story: it is exempt from the
state/type/acceptance/Parent rules and never selected for implementation,
but its own `Depends on:` list is surfaced for cross-Feature ordering.
A design-decision Blocker (carries `ready-for-human`) is not a third story
type: it is kept out of `state:ready` so the loop never picks it up, and is
exempt from the state/type/acceptance rules until a human reclassifies it.
Otherwise a workable story additionally requires:
  - exactly one state:* label (ready|in-progress|awaiting-bench|blocked)
  - exactly one type:* label (afk|hil)
  - a `## Acceptance Criteria` checklist with at least one `- [ ]` item
  - HIL stories also carry a `## Bench Test Procedure` section
"""
import json
import re
import sys

STATES = ("ready", "in-progress", "awaiting-bench", "blocked")
TYPES = ("afk", "hil")
BLOCKER_LABEL = "ready-for-human"
PRD_LABEL = "prd"


class StoryResult:
    def __init__(self, ok, errors, fields=None):
        self.ok = ok
        self.errors = errors
        self.fields = fields or {}


def _label_names(story):
    """Normalize `labels` to a list of names (accepts gh objects or strings)."""
    names = []
    for label in story.get("labels", []) or []:
        if isinstance(label, dict):
            name = label.get("name")
        else:
            name = label
        if name:
            names.append(name)
    return names


def _prefixed(names, prefix):
    return [n[len(prefix):] for n in names if n.startswith(prefix)]


def _has_section(body, heading):
    """True if the body has a `## <heading>` markdown section header."""
    pattern = r"^\s*#{1,6}\s+%s\s*$" % re.escape(heading)
    return re.search(pattern, body, re.MULTILINE) is not None


def _parse_depends_on(body):
    """Return (found, [issue numbers]) from a `Depends on:` line."""
    match = re.search(r"^\s*Depends on:\s*(.*)$", body, re.MULTILINE | re.IGNORECASE)
    if not match:
        return False, []
    return True, [int(n) for n in re.findall(r"#(\d+)", match.group(1))]


def _parse_parent(body):
    """Return (found, parent issue number or None) from a `Parent:` line.

    `Parent: #N` links a story to its Feature's PRD issue; `Parent: None`
    marks an Orphan Story (ADR-0002).
    """
    match = re.search(r"^\s*Parent:\s*(.*)$", body, re.MULTILINE | re.IGNORECASE)
    if not match:
        return False, None
    number = re.search(r"#(\d+)", match.group(1))
    return True, int(number.group(1)) if number else None


def validate_story(story):
    errors = []
    names = _label_names(story)
    body = story.get("body") or ""

    states = _prefixed(names, "state:")
    types = _prefixed(names, "type:")
    prios = _prefixed(names, "prio:")
    is_blocker = BLOCKER_LABEL in names
    is_prd = PRD_LABEL in names

    # --- rules common to every story --------------------------------------
    # prio is optional: a story may carry zero or one prio:N label. With none,
    # prio is None and the selection engine sorts it as lowest priority -- it
    # falls back to FIFO by issue number. Authors add prio:N only to jump the
    # queue (ADR-0002). More than one, or a non-numeric prio:, is still an error.
    if len(prios) > 1:
        errors.append("labels: at most one prio:N label is allowed (lower = higher priority)")
    elif prios and not prios[0].isdigit():
        errors.append("labels: a prio: label must be numeric (prio:N, lower = higher priority)")
    prio = int(prios[0]) if len(prios) == 1 and prios[0].isdigit() else None

    if "HITL" in body or "HITL" in (story.get("title") or ""):
        errors.append("terminology: use HIL, not HITL")

    depends_found, depends_on = _parse_depends_on(body)
    if not depends_found:
        errors.append("body: a `Depends on:` line is required (use `Depends on: None`)")

    parent_found, parent = _parse_parent(body)
    if not is_prd and not parent_found:
        errors.append(
            "body: a `Parent:` line is required (use `Parent: #N` for the Feature's "
            "PRD issue, or `Parent: None` for an Orphan Story)")

    state = states[0] if len(states) == 1 else None

    if is_prd:
        # A PRD issue is not a story (ADR-0002): it is exempt from the
        # state/type/acceptance rules and never selected for implementation.
        # Its `Depends on:` list still matters for cross-Feature ordering.
        pass
    elif is_blocker:
        # A design-decision Blocker is kept out of state:ready so the loop
        # never selects it; it is exempt from the type/acceptance rules.
        if "ready" in states:
            errors.append("blocker: a `ready-for-human` story must not carry state:ready")
    else:
        if len(states) != 1 or states[0] not in STATES:
            errors.append(
                "labels: exactly one state:* label is required (%s)" % "|".join(STATES))
        if len(types) != 1 or types[0] not in TYPES:
            errors.append(
                "labels: exactly one type:* label is required (%s)" % "|".join(TYPES))

        has_acceptance = _has_section(body, "Acceptance Criteria") and "- [ ]" in body
        if not has_acceptance:
            errors.append(
                "body: a `## Acceptance Criteria` checklist with at least one `- [ ]` item is required")

        if types == ["hil"] and not _has_section(body, "Bench Test Procedure"):
            errors.append(
                "body: a HIL story requires a `## Bench Test Procedure` section")

    fields = {
        "number": story.get("number"),
        "title": story.get("title"),
        "state": state,
        "type": types[0] if len(types) == 1 else None,
        "prio": prio,
        "depends_on": depends_on,
        "parent": parent,
        "is_blocker": is_blocker,
        "is_prd": is_prd,
        "has_bench": _has_section(body, "Bench Test Procedure"),
    }
    return StoryResult(not errors, errors, fields)


def _load(path):
    if path == "-":
        return json.load(sys.stdin)
    with open(path) as fh:
        return json.load(fh)


def main(argv):
    if not argv:
        sys.stderr.write("usage: ralph_story.py <story.json | ->\n")
        return 2
    try:
        story = _load(argv[0])
    except (OSError, ValueError) as exc:
        sys.stderr.write("ralph: could not read story: %s\n" % exc)
        return 2
    result = validate_story(story)
    label = story.get("number")
    ident = "#%s" % label if label else argv[0]
    if result.ok:
        f = result.fields
        if f["is_prd"]:
            kind = "prd"
        elif f["is_blocker"]:
            kind = "blocker"
        else:
            kind = f["type"] or "?"
        print("OK: %s [%s] prio:%s depends_on:%s"
              % (ident, kind, f["prio"], f["depends_on"] or "none"))
        return 0
    sys.stderr.write("INVALID: %s\n" % ident)
    for err in result.errors:
        sys.stderr.write("  - %s\n" % err)
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
