"""Validate a Review Agent's structured result against the shipped contract (#51).

Pure logic: no network, no side effects.  `validate_review` returns a
ReviewValidation carrying ok/errors and, when valid, the accepted payload;
`bin/ralph --validate-review` is a thin wrapper over `main`.

This module is the gate between a model's output and a pull request.  A payload
that does not validate is refused whole and never repaired: partially trusting
a malformed result is how a hallucinated finding would reach a review thread.
Rejections name the offending field path (`findings/1/id: ...`) so the caller
can say what was wrong without re-deriving it from the payload.
"""
import json
import os
import re
import sys

try:
    import jsonschema
except ImportError:  # pragma: no cover - environment guard
    sys.stderr.write("ralph: jsonschema is required (pip install jsonschema)\n")
    raise

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ralph_config  # noqa: E402

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_SCHEMA = os.path.join(REPO_ROOT, "schema", "review.schema.json")

CONTRACT_VERSION = "ralph-review/v1"

# The narrow blocking policy (CONTEXT.md, "Finding").  A finding is blocking
# only for one of these reasons; everything else is the author's judgement.
BLOCKING_CATEGORIES = ("acceptance_criteria", "defect", "safety_regression",
                       "explicit_rule", "missing_tests", "scope_creep")
NON_BLOCKING_CATEGORIES = ("style_preference", "speculative_improvement",
                           "preexisting_issue")

# The only two reasons a round after the first may raise a *new* blocker: a
# regression the fixes themselves introduced, and a serious correctness or
# safety defect that was missed.  Anything else raised late is the goalposts
# moving -- a round that could keep inventing acceptance-criteria or
# missing-tests blockers would never converge, and the Implementation Agent
# would be answering a different review each time.
LATE_BLOCKING_CATEGORIES = ("defect", "safety_regression")

# One result is rendered into one GitHub review body, and GitHub caps that at
# 65536 characters.  The margin below it belongs to Ralph's own framing.
MAX_PAYLOAD_BYTES = 60000
MAX_FINDINGS = 50


class Adjudication:
    """What a later round did to each finding an earlier round raised.

    Keyed by the stable identifier throughout: that is the only thing that
    survives a fresh reviewer, a new commit, and a re-rendered review body.
    """

    def __init__(self, upheld, withdrawn, raised):
        self.upheld = upheld
        self.withdrawn = withdrawn
        self.raised = raised

    def outcomes(self):
        """Each earlier finding's outcome, in the order it was first raised."""
        return ([(ident, "upheld") for ident in self.upheld]
                + [(ident, "withdrawn") for ident in self.withdrawn])


def adjudicate(prior_findings, findings):
    """Read a later round's verdict on the findings that preceded it.

    A reviewer withdraws a finding by not repeating it: silence about an
    objection someone answered with evidence is the withdrawal.  Nothing here
    interprets prose -- the identifiers a round does and does not restate are
    the whole signal, which is why they have to be stable.
    """
    prior = [f.get("id") for f in prior_findings or []]
    current = [f.get("id") for f in findings or []]
    return Adjudication(
        upheld=[i for i in prior if i in current],
        withdrawn=[i for i in prior if i not in current],
        raised=[i for i in current if i not in prior])


class ReviewValidation:
    def __init__(self, ok, errors, review=None):
        self.ok = ok
        self.errors = errors
        self.review = review or {}

    def blocking(self):
        """The accepted findings that block the pull request."""
        return [f for f in self.review.get("findings", []) if f.get("blocking")]

    def summary(self):
        """One-block human summary of an accepted result."""
        r = self.review
        findings = r.get("findings", [])
        blocking = len(self.blocking())
        return "\n".join([
            "contract: %s" % r.get("contract"),
            "verdict: %s" % r.get("verdict"),
            "head: %s" % r.get("head"),
            "model: %s" % r.get("model"),
            "round: %s" % r.get("round"),
            "findings: %d (%d blocking, %d non-blocking)"
            % (len(findings), blocking, len(findings) - blocking),
        ])


def changed_lines(diff):
    """Map each path the diff touches to the new-side lines a finding may cite.

    Context lines count: GitHub accepts an inline thread anywhere inside a hunk,
    and a finding about an added line often reads on the line above it.  Removed
    lines do not, because they no longer exist at the reviewed head.
    """
    lines, path, new_line = {}, None, None
    for raw in (diff or "").splitlines():
        if raw.startswith("+++ "):
            target = raw[4:].strip()
            path = None if target == "/dev/null" else re.sub(r"^b/", "", target)
            if path is not None:
                lines.setdefault(path, set())
            new_line = None
        elif raw.startswith("@@"):
            match = re.match(r"^@@ -\d+(?:,\d+)? \+(\d+)", raw)
            new_line = int(match.group(1)) if match else None
        elif path is not None and new_line is not None:
            if raw.startswith("+") or raw.startswith(" ") or raw == "":
                lines[path].add(new_line)
                new_line += 1
    return lines


def _load_schema(schema_path):
    with open(schema_path) as fh:
        return json.load(fh)


def _payload_bytes(payload, raw):
    if raw is not None:
        return len(raw.encode("utf-8"))
    try:
        return len(json.dumps(payload).encode("utf-8"))
    except (TypeError, ValueError):
        return 0


def _identity_errors(findings):
    errors, seen = [], set()
    for index, finding in enumerate(findings):
        ident = finding.get("id")
        if ident in seen:
            errors.append("findings/%d/id: duplicate finding id %r" % (index, ident))
        seen.add(ident)
    return errors


def _policy_errors(payload, findings):
    """The blocking policy and the verdict it implies.

    Blocking is declared per finding *and* constrained by category, so neither a
    preference dressed up as a blocker nor a defect quietly waved through as a
    preference validates.
    """
    errors = []
    for index, finding in enumerate(findings):
        blocking, category = finding.get("blocking"), finding.get("category")
        allowed = BLOCKING_CATEGORIES if blocking else NON_BLOCKING_CATEGORIES
        if category not in allowed:
            errors.append(
                "findings/%d/category: %r is not a %s category (%s)"
                % (index, category, "blocking" if blocking else "non-blocking",
                   ", ".join(allowed)))

    verdict = payload.get("verdict")
    blockers = sum(1 for f in findings if f.get("blocking"))
    if verdict == "request_changes" and not blockers:
        errors.append("verdict: %r requires at least one blocking finding" % verdict)
    if verdict != "request_changes" and blockers:
        errors.append("verdict: %r cannot carry %d blocking finding(s); a blocker "
                      "must request changes" % (verdict, blockers))
    return errors


def _location_errors(findings, changed):
    errors = []
    for index, finding in enumerate(findings):
        location = finding.get("location")
        if not location:
            continue
        field = "findings/%d/location" % index
        path, line = location["path"], location["line"]
        end_line = location.get("end_line")
        if os.path.isabs(path) or ".." in path.split("/"):
            errors.append("%s/path: %r must be a repository-relative path inside "
                          "the reviewed head" % (field, path))
            continue
        if end_line is not None and end_line < line:
            errors.append("%s/end_line: %d precedes line %d" % (field, end_line, line))
            continue
        if changed is None:
            continue
        if path not in changed:
            errors.append("%s/path: the reviewed diff does not touch %r" % (field, path))
            continue
        bounds = [("line", line)]
        if end_line is not None:
            bounds.append(("end_line", end_line))
        for name, value in bounds:
            if value not in changed[path]:
                errors.append("%s/%s: %s has no changed line %d in the reviewed diff"
                              % (field, name, path, value))
    return errors


def _late_blocker_errors(payload, findings, prior_findings):
    """A round after the first may not widen what blocks the pull request.

    Only two kinds of blocker can legitimately appear late: one the fixes
    themselves caused, and one serious enough that having missed it once is no
    reason to keep missing it.  Everything else raised for the first time in a
    later round is the goalposts moving, and is refused here rather than left
    to the Implementation Agent to argue about.
    """
    if prior_findings is None or (payload.get("round") or 1) < 2:
        return []
    prior = {f.get("id") for f in prior_findings}
    errors = []
    for index, finding in enumerate(findings):
        if not finding.get("blocking") or finding.get("id") in prior:
            continue
        if finding.get("category") not in LATE_BLOCKING_CATEGORIES:
            errors.append(
                "findings/%d/category: %r is raised for the first time in round "
                "%s, where a new blocker may only be %s"
                % (index, finding.get("id"), payload.get("round"),
                   " or ".join(LATE_BLOCKING_CATEGORIES)))
    return errors


def validate_review(payload, changed=None, raw=None, prior_findings=None,
                    schema_path=DEFAULT_SCHEMA):
    """Validate one review result.

    `changed` is `changed_lines(diff)` for the reviewed head; without it source
    locations are still checked for shape, but not for range.  `raw` is the
    payload exactly as the model emitted it, so the size limit measures what was
    actually produced rather than a re-serialization of it.  `prior_findings`
    are the findings of the round before this one; without them a result is
    judged on its own terms, with them a late blocker is bounded.
    """
    size = _payload_bytes(payload, raw)
    if size > MAX_PAYLOAD_BYTES:
        return ReviewValidation(False, [
            "(root): review payload is %d bytes, over the %d-byte limit"
            % (size, MAX_PAYLOAD_BYTES)])
    if not isinstance(payload, dict):
        return ReviewValidation(False, ["(root): review payload must be an object"])

    validator = jsonschema.Draft7Validator(_load_schema(schema_path))
    errors = sorted(validator.iter_errors(payload), key=lambda e: list(e.absolute_path))
    if errors:
        return ReviewValidation(False, [ralph_config.format_error(e) for e in errors])

    findings = payload["findings"]
    errors = (_identity_errors(findings)
              + _policy_errors(payload, findings)
              + _late_blocker_errors(payload, findings, prior_findings)
              + _location_errors(findings, changed))
    if errors:
        return ReviewValidation(False, errors)
    return ReviewValidation(True, [], payload)


def _read(path):
    if path == "-":
        return sys.stdin.read()
    with open(path) as fh:
        return fh.read()


def _cmd_validate(rest):
    if not rest or len(rest) > 2:
        sys.stderr.write("usage: ralph --validate-review PAYLOAD [DIFF]\n")
        return 2
    payload_path = rest[0]
    try:
        raw = _read(payload_path)
        payload = json.loads(raw)
        changed = changed_lines(_read(rest[1])) if len(rest) == 2 else None
    except (OSError, ValueError) as exc:
        sys.stderr.write("ralph: could not read review payload: %s\n" % exc)
        return 2

    result = validate_review(payload, changed=changed, raw=raw)
    if result.ok:
        print("OK: %s" % payload_path)
        print(result.summary())
        return 0
    sys.stderr.write("INVALID: %s\n" % payload_path)
    for error in result.errors:
        sys.stderr.write("  - %s\n" % error)
    return 1


def main(argv):
    if argv and argv[0] == "validate":
        return _cmd_validate(argv[1:])
    sys.stderr.write("usage: ralph_review_result.py validate PAYLOAD [DIFF]\n")
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
