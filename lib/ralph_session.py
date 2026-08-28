"""Session-limit detection: did this agent run out of session, or just finish? (#65)

The tick has to tell three things apart in a launched agent's exit code and
output: it finished, it ran out of session, or it broke. Only the middle one may
end the tick -- a session-limit hit is checkpointed via a Handoff and resumed
next tick, never retried inside the same tick.

This used to be one exit code (91) and one literal substring
("usage limit reached"), matched in `bin/ralph.sh`. The claude CLI emits
neither today; it says::

    You've hit your session limit - resets 9pm (Europe/Stockholm)

so every limit hit was misread as partial progress and the tick relaunched the
same story until its iteration budget ran out -- a retry-storm observed live on
2026-08-27. One literal string is a single point of failure: the provider owns
the wording and can change it in any release, and the failure mode is silent.

**The robustness strategy, and why.** Detection is layered, so no single signal
changing breaks it:

1. *Exit code* -- a **set**, not one number (`RALPH_SESSION_LIMIT_EXIT` takes a
   comma-separated list), because a provider may add or renumber its code.
2. *A pattern family, not a string* -- several regexes for the **shape** of a
   limit notice rather than one sentence: a limit verb near a quota noun near
   "limit", or a "limit ... resets" clause. Rewordings within that shape are
   already covered; a genuinely new shape is one regex to add. The legacy
   literal is kept as its own pattern so old wordings never regress.
3. *An operator escape hatch* -- `RALPH_SESSION_LIMIT_MARKER` **adds** a marker
   to the family. It deliberately cannot replace the built-ins: an override
   that silently disabled the shipped patterns is how one stale literal became
   the only detector in the first place.

**Why matching is anchored to the tail.** The tick greps the agent's whole
transcript, so widening the patterns creates the opposite hazard: an agent
working on session-limit code (this very story) writes these phrases in prose
and would checkpoint itself. A provider's limit notice is *terminating* output
-- the CLI stops there -- so a match only counts on the final non-empty line,
or, when the process also failed, within the last few lines. That distinguishes
"the CLI said it" from "the agent mentioned it" structurally, rather than by
hoping the wordings never collide.

`bin/ralph.sh` cannot import Python, so the verdict is also an exit-code
contract (`EXIT_*`) that `ralph --classify-session` returns and the tick reads.
`EXIT_SESSION_EXHAUSTED` is 10, the tick's own `RC_SESSION_LIMIT`.
"""
import os
import re
import sys

NORMAL = "normal"
SESSION_EXHAUSTED = "session-exhausted"

# The verdict as the exit-code contract bin/ralph.sh reads.
EXIT_NORMAL = 0
EXIT_SESSION_EXHAUSTED = 10

# Layer 1: exit codes that mean exhaustion on their own, whatever the output says.
DEFAULT_SESSION_LIMIT_EXITS = (91,)

# Quota nouns a provider puts next to "limit". `\d+-hour` covers rolling windows
# ("5-hour limit") without enumerating the number.
_QUOTA = r"(?:session|usage|weekly|monthly|daily|hourly|rate|quota|token|message|request|\d+[- ]hour)"
_SPENT = r"(?:reached|exceeded|exhausted|hit|used up|run out of|out of)"

# Layer 2: the shape of a limit notice, not one sentence. Each pattern is an
# independent way the same event gets spelled; matching is case-insensitive and
# `[^.\n]{0,40}` keeps a match inside one clause of one line.
SESSION_LIMIT_PATTERNS = (
    # The pre-#65 literal, pinned so old wordings can never regress.
    r"usage limit reached",
    # "You've hit your session limit", "You have reached your weekly limit".
    r"\b" + _SPENT + r"\b[^.\n]{0,40}?\b" + _QUOTA + r"\b[^.\n]{0,20}?\blimit\b",
    # "Session limit reached", "5-hour limit exceeded", "usage limit - resets 9pm".
    r"\b" + _QUOTA + r"[ -]limit\b[^.\n]{0,40}?\b(?:" + _SPENT + r"|resets?)\b",
    # The reset clause: whatever the noun, a limit that names when it resets.
    r"\b(?:limit|quota)\b[^.\n]{0,40}?\bresets?\b",
)

# How far back from the end of the output a match still counts. A limit notice
# terminates the run, so the final line is the strong signal; a failed process
# gets a little more slack for trailing hint lines the CLI may append.
TAIL_LINES_CLEAN_EXIT = 1
TAIL_LINES_FAILED_EXIT = 3


def _env(env):
    return os.environ if env is None else env


def session_limit_exits(env=None):
    """The exit codes that mean exhaustion (layer 1).

    `RALPH_SESSION_LIMIT_EXIT` is a comma-separated list so a superproject can
    teach the loop a new code without a release. An unparseable value falls back
    to the shipped default rather than disabling the signal.
    """
    raw = _env(env).get("RALPH_SESSION_LIMIT_EXIT", "")
    codes = set()
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            codes.add(int(part))
        except ValueError:
            return set(DEFAULT_SESSION_LIMIT_EXITS)
    return codes or set(DEFAULT_SESSION_LIMIT_EXITS)


def session_limit_patterns(env=None):
    """The compiled pattern family (layer 2), plus any operator-added marker
    (layer 3). The override appends; it never replaces the built-ins."""
    patterns = list(SESSION_LIMIT_PATTERNS)
    extra = _env(env).get("RALPH_SESSION_LIMIT_MARKER", "").strip()
    if extra:
        patterns.append(re.escape(extra))
    return [re.compile(p, re.IGNORECASE) for p in patterns]


def _tail(output, count):
    lines = [ln.strip() for ln in (output or "").splitlines()]
    lines = [ln for ln in lines if ln]
    return lines[-count:] if count else []


def looks_like_limit_notice(output, tail_lines, env=None):
    """Does the tail of `output` end in a provider's limit notice?"""
    patterns = session_limit_patterns(env)
    return any(p.search(line) for line in _tail(output, tail_lines) for p in patterns)


def classify(exit_code, output, env=None):
    """`SESSION_EXHAUSTED` if this launch hit its session limit, else `NORMAL`.

    Deliberately not a three-way verdict: whether a non-zero exit is a crash or
    a finished-but-unhappy run is the caller's business (`bin/ralph.sh` treats
    it as partial progress). This answers only the question that must never be
    wrong, because getting it wrong retries instead of checkpointing.
    """
    if exit_code in session_limit_exits(env):
        return SESSION_EXHAUSTED
    tail = TAIL_LINES_FAILED_EXIT if exit_code not in (0, None) else TAIL_LINES_CLEAN_EXIT
    if looks_like_limit_notice(output, tail, env=env):
        return SESSION_EXHAUSTED
    return NORMAL


def _cmd_classify(rest):
    """`ralph --classify-session RC`: the agent's output on stdin, the verdict as
    the exit code (`EXIT_SESSION_EXHAUSTED` / `EXIT_NORMAL`)."""
    if not rest or not rest[0].strip():
        sys.stderr.write("ralph: --classify-session requires the agent's exit code\n")
        return 2
    try:
        exit_code = int(rest[0])
    except ValueError:
        sys.stderr.write("ralph: --classify-session: not an exit code: %s\n" % rest[0])
        return 2
    verdict = classify(exit_code, sys.stdin.read())
    return EXIT_SESSION_EXHAUSTED if verdict == SESSION_EXHAUSTED else EXIT_NORMAL


def main(argv):
    if not argv:
        sys.stderr.write("usage: ralph_session.py classify RC  (output on stdin)\n")
        return 2
    mode, rest = argv[0], argv[1:]
    if mode == "classify":
        return _cmd_classify(rest)
    sys.stderr.write("ralph_session.py: unknown mode: %s\n" % mode)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
