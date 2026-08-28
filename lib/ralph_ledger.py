"""The per-Story token ledger and the pull request's usage footers (#63).

Two views of the same readings, each answering a different question.

The **ledger** is the canonical record: one machine-managed comment on the
Story, carrying a table a person can read and a versioned payload a script can
aggregate by Story, role, round, provider and model.  It is created once and
updated in place, because a Story that posted a fresh comment per invocation
would bury its own negotiation.

The **footer** is the immediate signal: what this one invocation cost, on the
agent response it belongs to, so a human reading the pull request sees the cost
of the thing they are reading without opening anything.

Both views obey one rule: a category the provider never reported is shown as a
gap, never as a zero and never as a number this made up.
"""
import json
import os
import re
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ralph_init  # noqa: E402
import ralph_usage  # noqa: E402

CONTRACT_VERSION = "ralph-usage-ledger/v1"

# The durable handle on the one comment that *is* the ledger.  Ralph posts with
# the operator's own credential, so "who wrote it" cannot identify it; the
# marker can, and it survives a re-clone the way every other loop fact does.
LEDGER_MARKER = "<!-- ralph-usage-ledger:v1 -->"

# How the table says "the provider never told us".  Deliberately not `0`: a
# model that did no reasoning and a provider that does not count reasoning are
# different facts, and the whole point of the ledger is to keep them apart.
UNAVAILABLE_CELL = "—"

_PAYLOAD_PATTERN = re.compile(
    re.escape(LEDGER_MARKER) + r"\s*```json\s*(.*?)```", re.DOTALL)

_COLUMNS = (("round", "Round"), ("phase", "Phase"), ("provider", "Provider"),
            ("model", "Model"))


def _cell(event, category):
    value = (event.get("usage") or {}).get(category)
    return UNAVAILABLE_CELL if value is None else "{:,}".format(value)


def _row(event):
    cells = [str(event.get(name) if event.get(name) is not None else "—")
             for name, _ in _COLUMNS]
    cells += [_cell(event, category) for category in ralph_usage.CATEGORIES]
    return "| " + " | ".join(cells) + " |"


# GitHub accepts 65536 characters in a comment body; the margin is Ralph's own
# framing. A Story can outrun that -- one row per invocation, and a hard Story
# runs many -- so the oldest rows are dropped rather than the write failing, and
# the count of what was dropped is stated in both views. A ledger that silently
# posted fewer rows would be quietly wrong, which is the one thing a record of
# what is known must never be.
MAX_BODY = 60000


def _rendered(story_number, events, dropped):
    headers = [label for _, label in _COLUMNS] + [
        "Input", "Cached", "Reasoning", "Output", "Total"]
    lines = [
        "## Token ledger — #%s" % story_number,
        "",
        "Provider-reported usage, one row per Implementation Agent or Review "
        "Agent invocation. Telemetry only: it gates nothing. `%s` means the "
        "provider does not report that category — never that it was zero."
        % UNAVAILABLE_CELL,
        "",
        "| " + " | ".join(headers) + " |",
        "|" + "|".join(["---"] * len(headers)) + "|",
    ]
    lines += [_row(event) for event in events]
    if dropped:
        lines += ["",
                  "_%d earlier reading%s dropped: this Story has run more "
                  "invocations than one comment holds._"
                  % (dropped, "" if dropped == 1 else "s")]
    lines += [
        "",
        LEDGER_MARKER,
        "",
        "```json",
        json.dumps({"version": CONTRACT_VERSION, "story": story_number,
                    "dropped": dropped, "events": list(events)},
                   indent=2, sort_keys=True),
        "```",
    ]
    return "\n".join(lines)


def ledger_body(story_number, events):
    """The whole ledger comment: the readable table, then the payload."""
    events, dropped = list(events), 0
    body = _rendered(story_number, events, dropped)
    while len(body) > MAX_BODY and events:
        # Drop roughly the overshoot's worth in one step rather than one row at
        # a time: a Story with hundreds of readings would otherwise re-render
        # the whole comment once per dropped row.
        over = len(body) - MAX_BODY
        drop = max(1, min(len(events), over * len(events) // len(body)))
        events, dropped = events[drop:], dropped + drop
        body = _rendered(story_number, events, dropped)
    return body


def parse_payload(body):
    """The machine-readable half of a ledger comment, or None."""
    match = _PAYLOAD_PATTERN.search(body or "")
    if not match:
        return None
    try:
        return json.loads(match.group(1))
    except ValueError:
        return None


def usage_footer(event):
    """What one invocation cost, in one line, for the response it belongs to."""
    if not event:
        return ""
    usage = event.get("usage") or {}
    who = "`%s` via %s, round %s" % (event.get("model"), event.get("provider"),
                                     event.get("round"))
    if all(usage.get(category) is None for category in ralph_usage.CATEGORIES):
        # Saying "unavailable" five times says nothing five times. The reading
        # that matters here is that this provider reported no counts at all.
        return ("_Usage — %s: the provider reported no token counts._" % who)

    def show(category):
        value = usage.get(category)
        return ralph_usage.UNAVAILABLE if value is None else "{:,}".format(value)

    return ("_Usage — %s: input %s (%s cached) · reasoning %s · output %s · "
            "total %s. Provider-reported; telemetry only._"
            % (who, show("input"), show("cached_input"), show("reasoning"),
               show("output"), show("total")))


def find_ledger(comments):
    """Return (the comment carrying the ledger or None, the events in it)."""
    for comment in comments or []:
        body = comment if isinstance(comment, str) else (comment or {}).get("body")
        if LEDGER_MARKER not in (body or ""):
            continue
        payload = parse_payload(body) or {}
        return comment, list(payload.get("events") or [])
    return None, []


def comment_id(comment):
    """The numeric id the REST comment endpoint needs, or None.

    `gh issue view --json comments` reports `id` as a GraphQL node id, which
    that endpoint will not take; the database id only survives in the comment's
    own URL, so that is where it is read from.
    """
    if not isinstance(comment, dict):
        return None
    database_id = comment.get("databaseId")
    if database_id:
        return str(database_id)
    match = re.search(r"issuecomment-(\d+)", comment.get("url") or "")
    return match.group(1) if match else None


class LedgerPlan:
    def __init__(self, ok, errors, commands, created=False, events=None):
        self.ok = ok
        self.errors = errors
        self.commands = commands
        self.created = created
        self.events = events or []


def ledger_plan(story_number, comments, event):
    """The plan adding one invocation to this Story's ledger.

    Pure: computes commands, runs nothing.  The first invocation creates the
    comment; every later one edits that same comment, so the Story keeps exactly
    one ledger however many rounds it takes.
    """
    existing, events = find_ledger(comments)
    events = events + [event]
    body = ledger_body(story_number, events)
    if existing is None:
        return LedgerPlan(True, [],
                          [["gh", "issue", "comment", str(story_number),
                            "--body", body]], created=True, events=events)
    ident = comment_id(existing)
    if ident is None:
        # Editing needs the comment's own id. Posting a second ledger instead
        # would leave two comments both claiming to be canonical, so this
        # reports the gap rather than papering over it.
        return LedgerPlan(False, [
            "ledger: the existing ledger comment on #%s carries no id to update"
            % story_number], [], events=events)
    return LedgerPlan(True, [],
                      [["gh", "api", "--method", "PATCH",
                        "repos/{owner}/{repo}/issues/comments/%s" % ident,
                        "-f", "body=" + body]], events=events)


def _gh_comments(story_number, cwd):
    proc = subprocess.run(
        ["gh", "issue", "view", str(story_number), "--json", "comments"],
        cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    if proc.returncode:
        raise RuntimeError(proc.stdout.strip() or "gh issue view failed")
    return (json.loads(proc.stdout or "{}") or {}).get("comments") or []


def record(story_number, event, cwd=None, comments=None):
    """Add one invocation to the Story's ledger.  Best-effort by design.

    Accounting is telemetry: a gh blip loses a row, and losing a row must never
    cost the invocation it was describing.  Returns whether the row landed.

    A caller that has already read the Story's comments passes them in: the
    stages that record usage all read them for the negotiation anyway, and only
    a *ledger* comment could have appeared since -- which none of them writes.
    """
    if not event or not story_number:
        return False
    if comments is None:
        try:
            comments = _gh_comments(story_number, cwd)
        except (OSError, ValueError, RuntimeError) as exc:
            sys.stderr.write("note: could not read the token ledger: %s\n" % exc)
            return False
    plan = ledger_plan(story_number, comments, event)
    if not plan.ok:
        for error in plan.errors:
            sys.stderr.write("note: %s\n" % error)
        return False
    run = ralph_init.run_plan(plan.commands, cwd=cwd)
    if not run.ok:
        sys.stderr.write("note: could not record token usage on #%s (exit %d)\n"
                         % (story_number, run.failed.returncode))
        return False
    return True
