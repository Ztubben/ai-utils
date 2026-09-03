"""Provider-reported token usage, normalized into one shape (#62, PRD #42).

Each provider counts context in its own vocabulary.  This module maps what a
provider actually reports onto five neutral categories -- input, cached input,
reasoning, output, total -- so a later script can compare usage across providers
without knowing which one produced a number.

The invariant is that nothing here invents a number.  A category the provider
does not expose is recorded as *unavailable*, not as zero and not as an
estimate, because a ledger that cannot tell a real zero from a missing reading
is worse than one that admits the gap.  Summing several reported sub-counts into
one normalized category is not an estimate -- every term came from the provider
-- but a category with even one of its terms missing is unavailable outright.

Accounting is telemetry only in this version: it gates nothing, and no
invocation is blocked or altered by what it records.
"""
import datetime
import json
import os
import sys

CONTRACT_VERSION = "ralph-usage/v1"

# The neutral vocabulary.  `reasoning` is a *subset* of `output` and
# `cached_input` a subset of `input` for both providers shipped here, so the
# five do not add up to a total -- which is one reason `total` is only ever
# recorded when a provider states one itself.
CATEGORIES = ("input", "cached_input", "reasoning", "output", "total")

# How a category is recorded when the provider does state it, and when it does
# not.  The status rides alongside the value so `null` can never be misread as a
# measured zero.
REPORTED = "reported"
UNAVAILABLE = "unavailable"

# What one invocation was doing.  The role says which agent ran; the phase says
# which part of the loop it ran for, so a review and the adjudication of a
# disputed finding stay distinguishable even though both are the Review Agent.
IMPLEMENTATION = "implementation"
REVIEW = "review"
RESPONSE = "response"
ARBITRATION = "arbitration"
PHASES = (IMPLEMENTATION, REVIEW, RESPONSE, ARBITRATION)

# Which provider keys make up each normalized category.  A dotted path reads a
# nested object.  Several keys are summed; one missing key makes the whole
# category unavailable, because a partial sum would be exactly the invented
# number this module exists to avoid.
#
# The two mappings are not symmetrical, and deliberately so:
#   * Claude reports `input_tokens` as the part that was neither read from nor
#     written to the cache, so normalized input is all three terms together.
#   * Codex reports `input_tokens` inclusive of the cached part, so it is the
#     normalized input on its own.
# Normalizing that difference away is the whole job; leaving it in place would
# make the two providers' "input" mean different things in one ledger.
_MAPPINGS = {
    "claude": {
        "input": ("input_tokens", "cache_creation_input_tokens",
                  "cache_read_input_tokens"),
        "cached_input": ("cache_read_input_tokens",),
        "reasoning": ("output_tokens_details.thinking_tokens",),
        "output": ("output_tokens",),
    },
    "codex": {
        "input": ("input_tokens",),
        "cached_input": ("cached_input_tokens",),
        "reasoning": ("reasoning_output_tokens",),
        "output": ("output_tokens",),
    },
}

# Every provider an adapter can launch must be mapped here, so a config that
# validates always produces usage in the neutral shape (guarded by a test).
MAPPED_PROVIDERS = tuple(sorted(_MAPPINGS))


def _read(raw, path):
    """The integer at a dotted path in *raw*, or None if it is not there."""
    value = raw
    for part in path.split("."):
        if not isinstance(value, dict):
            return None
        value = value.get(part)
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


class Usage:
    """What one invocation cost, in the neutral categories."""

    def __init__(self, provider, values=None):
        self.provider = provider
        values = values or {}
        self.values = {name: values.get(name) for name in CATEGORIES}

    def __getitem__(self, category):
        return self.values[category]

    def available(self, category):
        return self.values[category] is not None

    def status(self, category):
        return REPORTED if self.available(category) else UNAVAILABLE

    def payload(self):
        """The machine-readable half: the counts, and what is actually known."""
        return {"tokens": dict(self.values),
                "availability": {name: self.status(name)
                                 for name in CATEGORIES}}


def normalize(provider, raw):
    """Map one provider's reported usage onto the neutral categories.

    An unrecognized provider, a missing payload, or a payload in a shape this
    does not know yields a Usage where everything is unavailable -- the honest
    reading, and the one that keeps accounting incapable of failing a run.
    """
    mapping = _MAPPINGS.get(provider) or {}
    if not isinstance(raw, dict):
        raw = {}
    values = {}
    for category, paths in mapping.items():
        parts = [_read(raw, path) for path in paths]
        values[category] = None if None in parts else sum(parts)
    return Usage(provider, values)


def run_identity():
    """The identifier tying every invocation of one tick together.

    Exported by the tick so each event can be grouped by the run that made it;
    a hand-run command that has no tick around it gets its own.
    """
    return os.environ.get("RALPH_RUN_ID") or "adhoc-%d" % os.getpid()


def _now():
    return datetime.datetime.now(datetime.timezone.utc).replace(
        microsecond=0).isoformat().replace("+00:00", "Z")


def invocation_event(usage, role, phase, model, provider, story=None,
                     pull_request=None, round_no=None, head=None, run=None,
                     when=None):
    """One versioned, machine-readable record of one agent invocation."""
    return {
        "version": CONTRACT_VERSION,
        "story": story,
        "pull_request": pull_request,
        "role": role,
        "phase": phase,
        "provider": provider,
        "model": model,
        "round": round_no,
        "head": head,
        "time": when or _now(),
        "run": run or run_identity(),
        "usage": usage.payload()["tokens"],
        "availability": usage.payload()["availability"],
    }


# One line per invocation, on the stream the tick already captures. The marker
# makes it greppable out of a transcript that is otherwise agent prose, and the
# rest of the line is the event exactly as a later script wants it.
EVENT_MARKER = "RALPH-USAGE-EVENT "


def emit(event, stream=None):
    """Write one invocation event where the run's log will keep it.

    Telemetry never gets to fail a run, so a stream that cannot be written is
    simply a reading that was not kept.
    """
    if not event:
        return
    try:
        (stream or sys.stderr).write(
            "%s%s\n" % (EVENT_MARKER, json.dumps(event, sort_keys=True)))
    except Exception:                           # noqa: BLE001 - see docstring
        pass


def _cmd_normalize(rest):
    """`ralph --normalize-usage PROVIDER [PATH]`: the raw usage on stdin or at
    PATH, the normalized shape on stdout. A reading aid for an operator; the
    loop calls the module directly."""
    if not 1 <= len(rest) <= 2 or not rest[0]:
        sys.stderr.write("usage: ralph --normalize-usage PROVIDER [PATH]\n")
        return 2
    provider = rest[0]
    try:
        if len(rest) > 1 and rest[1] != "-":
            with open(rest[1]) as fh:
                raw = json.load(fh)
        else:
            raw = json.load(sys.stdin)
    except (OSError, ValueError) as exc:
        sys.stderr.write("ralph: could not read the usage payload: %s\n" % exc)
        return 2
    print(json.dumps(normalize(provider, raw).payload(), indent=2,
                     sort_keys=True))
    return 0


def main(argv):
    if argv and argv[0] == "normalize":
        return _cmd_normalize(argv[1:])
    sys.stderr.write("usage: ralph_usage.py normalize PROVIDER [PATH]\n")
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
