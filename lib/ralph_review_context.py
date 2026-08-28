"""Diff-first, commit-bound context for one Review Agent round (#50).

The bundle deliberately contains durable repository/PR evidence only.  It is
not a session handoff and never reads an implementation-agent transcript.
Review Agents may inspect the checkout separately through the read-only launch
policy in :mod:`ralph_agent`.
"""
import json
import os
import re
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ralph_handoff  # noqa: E402
import ralph_review  # noqa: E402


class ContextResult:
    def __init__(self, ok, errors, text="", head=None, round_no=None):
        self.ok = ok
        self.errors = errors
        self.text = text
        self.head = head
        self.round_no = round_no


def _section(body, heading):
    pattern = (r"^#{1,6}\s+%s\s*$\n(.*?)(?=^#{1,6}\s+|\Z)"
               % re.escape(heading))
    match = re.search(pattern, body or "", re.MULTILINE | re.DOTALL | re.IGNORECASE)
    content = match.group(1).strip() if match else ""
    # Canonical Story metadata follows the final Markdown section without its
    # own heading; it is selection state, not acceptance evidence.
    content = re.split(r"^\s*(?:Parent|Depends on):", content,
                       maxsplit=1, flags=re.MULTILINE | re.IGNORECASE)[0]
    return content.strip()


def _oid(pr, name):
    direct = pr.get(name + "RefOid")
    nested = pr.get(name)
    if direct:
        return direct
    if isinstance(nested, dict):
        return nested.get("oid") or nested.get("sha")
    return None


def _author(item):
    author = (item or {}).get("author")
    return author.get("login") if isinstance(author, dict) else (author or "unknown")


def durable_discussion(pr):
    """Return sanitized, durable PR discussion in chronological input order.

    Story comments are intentionally not accepted here: that is where Handoffs,
    Attempts, and implementation session notes live.  The only collaboration
    evidence admitted is the pull request's ordinary comments and reviews.
    """
    result = []
    for kind, items in (("comment", pr.get("comments") or []),
                        ("review", pr.get("reviews") or [])):
        for item in items:
            body = (item or {}).get("body") or ""
            if ralph_handoff.HANDOFF_MARKER in body:
                continue
            entry = {"kind": kind, "author": _author(item), "body": body}
            for key in ("state", "submittedAt", "createdAt", "path", "line"):
                if (item or {}).get(key) is not None:
                    entry[key] = item[key]
            result.append(entry)
    return result


def build_context(story, pull_request, diff, guidance, domain_decisions,
                  ci_status, round_no):
    """Build one deterministic Markdown review bundle from supplied evidence."""
    errors = []
    if not isinstance(round_no, int) or isinstance(round_no, bool) or round_no < 1:
        errors.append("round: must be a positive integer")
    if not ralph_review.is_managed_pr(pull_request):
        errors.append("pull_request: missing Ralph-managed marker")
    head = _oid(pull_request, "head")
    base = _oid(pull_request, "base")
    if not head:
        errors.append("pull_request/headRefOid: exact head commit is required")
    if not base:
        errors.append("pull_request/baseRefOid: exact base commit is required")
    acceptance = _section(story.get("body") or "", "Acceptance Criteria")
    if not acceptance:
        errors.append("story/body: Acceptance Criteria section is required")
    if errors:
        return ContextResult(False, errors, head=head, round_no=round_no)

    def evidence(items):
        if not items:
            return "(none)"
        chunks = []
        for item in items:
            if isinstance(item, dict):
                name, content = item.get("path", "evidence"), item.get("content", "")
            else:
                name, content = item
            chunks.append("### %s\n\n%s" % (name, content.rstrip()))
        return "\n\n".join(chunks)

    discussion = durable_discussion(pull_request)
    lines = [
        "# Ralph Review Context v1",
        "",
        "Review round: %d" % round_no,
        "Pull request: #%s" % pull_request.get("number", "?"),
        "Story: #%s — %s" % (story.get("number", "?"), story.get("title", "")),
        "Base commit: %s" % base,
        "Exact head commit: %s" % head,
        "",
        "The checkout may be explored read-only. Do not edit files, create commits, "
        "push, or mutate GitHub.",
        "",
        "## Acceptance Criteria", "", acceptance,
        "", "## Base/Head Diff", "", "```diff", (diff or "").rstrip(), "```",
        "", "## Repository Guidance", "", evidence(guidance),
        "", "## Domain Language and Decisions", "", evidence(domain_decisions),
        "", "## Current CI Status", "",
        "```json", json.dumps(ci_status, indent=2, sort_keys=True), "```",
        "", "## Prior Durable Review Discussion", "",
        "```json", json.dumps(discussion, indent=2, sort_keys=True), "```",
        "",
    ]
    return ContextResult(True, [], "\n".join(lines), head=head, round_no=round_no)


def _read(path):
    with open(path) as fh:
        return fh.read()


def repository_evidence(root, changed_paths):
    """Collect scoped guidance plus the repository's domain/decision sources."""
    root = os.path.abspath(root)
    guidance_paths = set()
    root_agents = os.path.join(root, "AGENTS.md")
    if os.path.isfile(root_agents):
        guidance_paths.add(root_agents)
    for changed in changed_paths:
        current = os.path.dirname(os.path.join(root, changed))
        while os.path.commonpath([root, os.path.abspath(current)]) == root:
            candidate = os.path.join(current, "AGENTS.md")
            if os.path.isfile(candidate):
                guidance_paths.add(candidate)
            if os.path.abspath(current) == root:
                break
            current = os.path.dirname(current)
    guidance = [(os.path.relpath(path, root), _read(path))
                for path in sorted(guidance_paths)]

    domain = []
    context = os.path.join(root, "CONTEXT.md")
    if os.path.isfile(context):
        domain.append(("CONTEXT.md", _read(context)))
    adr_dir = os.path.join(root, "docs", "adr")
    if os.path.isdir(adr_dir):
        for name in sorted(os.listdir(adr_dir)):
            path = os.path.join(adr_dir, name)
            if name.endswith(".md") and os.path.isfile(path):
                domain.append((os.path.relpath(path, root), _read(path)))
    return guidance, domain


def _git(args, root):
    proc = subprocess.run(["git"] + list(args), cwd=root,
                          stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                          text=True)
    if proc.returncode:
        raise RuntimeError(proc.stdout.strip() or "git command failed")
    return proc.stdout


def _load(path):
    with open(path) as fh:
        return json.load(fh)


def _cmd_bundle(rest):
    if len(rest) not in (3, 4):
        sys.stderr.write(
            "usage: ralph --review-context STORY PR ROUND [ROOT]\n")
        return 2
    story_path, pr_path, raw_round = rest[:3]
    root = os.path.abspath(rest[3] if len(rest) == 4 else os.getcwd())
    try:
        story, pr = _load(story_path), _load(pr_path)
        round_no = int(raw_round)
        head, base = _oid(pr, "head"), _oid(pr, "base")
        if head and _git(["rev-parse", head], root).strip() != head:
            raise RuntimeError("pull request head did not resolve exactly")
        changed = _git(["diff", "--name-only", base, head], root).splitlines()
        diff = _git(["diff", "--no-ext-diff", base, head], root)
        guidance, domain = repository_evidence(root, changed)
    except (OSError, ValueError, RuntimeError) as exc:
        sys.stderr.write("ralph: could not assemble review context: %s\n" % exc)
        return 2
    result = build_context(
        story, pr, diff, guidance, domain, pr.get("statusCheckRollup") or [],
        round_no)
    if not result.ok:
        sys.stderr.write("REFUSED: review context\n")
        for error in result.errors:
            sys.stderr.write("  - %s\n" % error)
        return 2
    sys.stdout.write(result.text)
    return 0


def main(argv):
    if argv and argv[0] == "bundle":
        return _cmd_bundle(argv[1:])
    sys.stderr.write("usage: ralph_review_context.py bundle STORY PR ROUND [ROOT]\n")
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
