"""Incident capture: a Story's GitHub state as a replayable fixture (#90, PRD #85).

`ralph --capture STORY [--out DIR]` reads -- read-only, through the real `gh` --
the Story issue (labels, comments), its PRD when it has a `Parent:`, its newest
Ralph-managed pull request (body, head, base, comments, native reviews, inline
review threads, check rollup) and the pull request's commit chain, and writes
them in the scenario harness's fake-GitHub state format
(`test/scenarios/fakes/gh`).  A scenario loads the file as its starting world,
so the Tick right after a production incident replays deterministically.

Git content is deliberately *not* captured: only the chain of commit ids from
the base to the head, with subjects.  A replay rebuilds a synthetic chain of the
same shape and translates every captured id to its replay counterpart, so the
fixture carries no foreign history and the records still line up with commits
that exist.

Pure `to_state(...)` builds the fixture; `capture(number, cwd)` does the reads.
Every `gh` call here is a read (view/list/GET); none mutates GitHub.
"""
import json
import os
import re
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ralph_iterate  # noqa: E402
import ralph_review  # noqa: E402
import ralph_review_round  # noqa: E402
import ralph_story  # noqa: E402

FORMAT = "ralph-capture/v1"
ISSUE_FIELDS = "number,title,labels,body,state,comments"
# No baseRefOid: gh releases before it was added to `pr view --json` refuse the
# whole call over it (2.46 does), so the base commit is read from the REST pull.
PR_FIELDS = ("number,title,body,state,headRefName,baseRefName,headRefOid,"
             "statusCheckRollup,comments,createdAt,mergedAt")
# REST lists page (30 by default); an incident is exactly the Story that
# outgrew a page, so every page is read, one element per line.
PER_PAGE = "?per_page=100"


class CaptureResult:
    def __init__(self, ok, errors, state=None):
        self.ok = ok
        self.errors = errors
        self.state = state


def _database_id(comment, fallback):
    match = re.search(r"(?:issuecomment|discussion_r)-?(\d+)", comment.get("url") or "")
    return int(match.group(1)) if match else fallback


def _login(obj):
    return ((obj or {}).get("login")) or "ghost"


def _comments(raw, counter):
    out = []
    for comment in raw or []:
        counter[0] += 1
        out.append({"id": _database_id(comment, counter[0]),
                    "node_id": comment.get("id") or "IC_%d" % counter[0],
                    "author": _login(comment.get("author")),
                    "body": comment.get("body") or "",
                    "createdAt": comment.get("createdAt"),
                    "url": comment.get("url") or ""})
    return out


def _issue(raw, counter):
    return {"number": raw["number"], "title": raw.get("title") or "",
            "body": raw.get("body") or "", "state": raw.get("state") or "OPEN",
            "labels": ralph_story._label_names(raw),
            "comments": _comments(raw.get("comments"), counter),
            "url": raw.get("url") or ""}


def _split_rollup(rollup):
    """Commit statuses belong to one head; check runs are kept as the world's CI."""
    statuses, checks = [], []
    for entry in rollup or []:
        if "state" in entry and "conclusion" not in entry:
            statuses.append({"context": entry.get("context"),
                             "state": (entry.get("state") or "").lower(),
                             "description": entry.get("description") or "",
                             "at": entry.get("startedAt")})
        else:
            checks.append({k: entry.get(k) for k in
                           ("__typename", "name", "status", "conclusion")})
    return statuses, checks


def to_state(repo, story, pull_request=None, reviews=(), threads=(), commits=(),
             merge_base=None, prd=None):
    """The fake-GitHub state for one captured Story (see module docstring)."""
    counter = [0]
    issues = {str(story["number"]): _issue(story, counter)}
    if prd is not None:
        issues[str(prd["number"])] = _issue(prd, counter)
    labels = sorted({name for issue in issues.values() for name in issue["labels"]})
    state = {"format": FORMAT,
             "repo": {"owner": repo["owner"], "name": repo["name"],
                      "defaultBranch": repo.get("defaultBranch") or "main"},
             "labels": [{"name": name} for name in labels],
             "issues": issues, "pulls": {}, "statuses": {}, "ci": [],
             "git": None}
    numbers = [int(n) for n in issues]
    ids = [c["id"] for issue in issues.values() for c in issue["comments"]]
    if pull_request is not None:
        pr = pull_request
        head = pr.get("headRefOid")
        statuses, checks = _split_rollup(pr.get("statusCheckRollup"))
        if statuses:
            state["statuses"][head] = statuses
        state["ci"] = checks
        entry = {"number": pr["number"], "title": pr.get("title") or "",
                 "body": pr.get("body") or "", "state": pr.get("state") or "OPEN",
                 "headRefName": pr["headRefName"], "baseRefName": pr["baseRefName"],
                 "url": pr.get("url") or "",
                 "comments": _comments(pr.get("comments"), counter),
                 "reviews": [{"id": r["id"], "node_id": r.get("node_id") or "PRR_%s" % r["id"],
                              "author": _login(r.get("user")), "body": r.get("body") or "",
                              "state": r.get("state") or "COMMENTED",
                              "submittedAt": r.get("submitted_at"),
                              "commit_id": r.get("commit_id")} for r in reviews],
                 "reviewComments": [{"id": c["id"], "author": _login(c.get("user")),
                                     "body": c.get("body") or "", "path": c.get("path"),
                                     "line": c.get("line"), "start_line": c.get("start_line"),
                                     "side": c.get("side"), "commit_id": c.get("commit_id"),
                                     "in_reply_to_id": c.get("in_reply_to_id"),
                                     "pull_request_review_id": c.get("pull_request_review_id"),
                                     "createdAt": c.get("created_at")} for c in threads],
                 "createdAt": pr.get("createdAt"), "mergedAt": pr.get("mergedAt")}
        if entry["state"] != "OPEN":
            entry["mergedHeadOid"] = head
            entry["mergedBaseOid"] = pr.get("baseRefOid")
        state["pulls"][str(pr["number"])] = entry
        numbers.append(pr["number"])
        ids += [c["id"] for c in entry["comments"]]
        ids += [r["id"] for r in entry["reviews"]] + [c["id"] for c in entry["reviewComments"]]
        state["git"] = {"base": pr.get("baseRefOid"), "merge_base": merge_base,
                        "head": head, "commits": list(commits)}
    state["next_number"] = max(numbers) + 1
    state["next_id"] = max([i for i in ids if isinstance(i, int)] + [1000])
    state["clock"] = 0
    return state


# --- redaction ----------------------------------------------------------------
#
# A capture of a private repository must be committable to a public one.
# Redaction keeps what the Loop *parses* and drops what people wrote: every
# marker, every fenced JSON record (with only its structural fields' strings
# kept), the generated header lines, the finding headings and the ledger rows.
# It is a whitelist -- a line nobody listed here is prose, and prose goes.

REDACTED = "[redacted]"
_KEEP_LINE = [re.compile(p) for p in (
    r"^\s*<!--.*-->\s*$",
    r"^Refs #\d+$", r"^Closes #\d+$",
    r"^Parent: .*$", r"^Depends on: .*$",
    r"^(Implementing|Reviewing) model: `[^`]*`$",
    r"^(Reviewed commit|New head|Head|Base): [0-9a-f]{7,40}$",
    r"^Ralph [a-z ]+— round \d+$",
    r"^## Token ledger — #\d+$",
    r"^\|.*\|$",
    r"^(/approve|/request-changes)$",
    r"^-{3,}$",
)]
_HEADINGS = {"What to build", "Acceptance Criteria", "Bench Test Procedure",
             "Problem Statement", "Solution", "User Stories",
             "Implementation Decisions", "Testing Decisions", "Out of Scope",
             "Further Notes", "Earlier findings"}
_FINDING_HEAD = re.compile(
    r"^(\*\*[A-Z]+-\d+\*\*(?: \([a-z_]+, (?:non-)?blocking\))?"
    r"(?: — (?:accepted|disputed|unresolved))?)")
_CHECKBOX = re.compile(r"^(\s*- \[[ xX]\] )")
_TOKEN = re.compile(r"^\S{0,64}$")
# String values of these record fields are identifiers, never prose.
_KEEP_KEYS = {"contract", "verdict", "head", "model", "provider", "id", "category",
              "disposition", "phase", "role", "kind", "status", "state", "run_id",
              "event", "decision", "review", "at", "recorded_at", "timestamp",
              "new_head", "reviewed_head", "outcome", "login", "author", "source"}


def _redact_json(value, paths, key=None):
    if isinstance(value, dict):
        return {k: _redact_json(v, paths, k) for k, v in value.items()}
    if isinstance(value, list):
        return [_redact_json(v, paths, key) for v in value]
    if isinstance(value, str) and key == "path":
        return paths.setdefault(value, "redacted/file-%d" % (len(paths) + 1))
    # A space-free token (a version, a status, a timestamp, an identifier) is
    # structure; prose has words.
    if isinstance(value, str) and key not in _KEEP_KEYS and not _TOKEN.match(value):
        return REDACTED
    return value


def redact_text(text, paths):
    """*text* reduced to the structure the Loop reads (see the block comment)."""
    out, fence, dropped = [], None, False
    for line in (text or "").splitlines():
        if fence is not None:
            if line.strip().startswith("```"):
                try:
                    body = json.dumps(_redact_json(json.loads("\n".join(fence)), paths),
                                      indent=2, sort_keys=True)
                except ValueError:
                    body = REDACTED
                out += ["```json", body, "```"]
                fence = None
            else:
                fence.append(line)
            continue
        if line.strip() == "```json":
            fence = []
            continue
        stripped = line.strip()
        heading = re.match(r"^#{1,6} (.*)$", stripped)
        finding = _FINDING_HEAD.match(stripped)
        box = _CHECKBOX.match(line)
        if any(p.match(stripped) for p in _KEEP_LINE) or (
                heading and heading.group(1) in _HEADINGS) or not stripped:
            kept = line
        elif finding:
            kept = finding.group(1)
        elif box:
            kept = box.group(1) + REDACTED
        else:
            if not dropped:
                out.append(REDACTED)
            dropped = True
            continue
        dropped = False
        out.append(kept)
    return "\n".join(out)


def redact(state):
    """A copy of a captured *state* safe to publish: prose out, structure kept.

    Titles become "Story N" / "PRD N" and branch names are renamed to what the
    Loop derives from those titles, so a replay recomputes the same branches.
    """
    state = json.loads(json.dumps(state))
    paths, renames = {}, {}
    for issue in state["issues"].values():
        issue["title"] = ("PRD %d" if "prd" in issue["labels"] else "Story %d") % issue["number"]
    stories = {int(n): i for n, i in state["issues"].items()}
    for pr in state["pulls"].values():
        story = next((stories[n] for n in stories
                      if re.search(r"Refs #%d\b" % n, pr["body"])), None)
        if story is not None:
            _, parent = ralph_story._parse_parent(story["body"])
            new = ralph_iterate.branch_name(story)
            renames[pr["headRefName"]] = new
            if parent in stories:
                renames[pr["baseRefName"]] = ralph_iterate.branch_name(
                    stories[parent], ralph_iterate.DEFAULT_FEATURE_PATTERN)
        pr["title"] = REDACTED
    for issue in list(state["issues"].values()) + list(state["pulls"].values()):
        issue["body"] = redact_text(issue["body"], paths)
        for comment in issue.get("comments", []):
            comment["body"] = redact_text(comment["body"], paths)
        for review in issue.get("reviews", []):
            review["body"] = redact_text(review["body"], paths)
        for comment in issue.get("reviewComments", []):
            comment["body"] = redact_text(comment["body"], paths)
            if comment.get("path"):
                comment["path"] = paths.setdefault(comment["path"],
                                                   "redacted/file-%d" % (len(paths) + 1))
    for i, commit in enumerate((state.get("git") or {}).get("commits", [])):
        commit["subject"] = "commit %d" % (i + 1)
    text = json.dumps(state)
    for old, new in sorted(renames.items(), key=lambda kv: -len(kv[0])):
        text = text.replace(old, new)
    return json.loads(text)


# --- the reads ---------------------------------------------------------------

def _run(args, cwd):
    proc = subprocess.run(args, cwd=cwd, stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, text=True)
    if proc.returncode:
        raise RuntimeError("%s: %s" % (" ".join(args[:3]),
                                       proc.stderr.strip() or "failed"))
    return proc.stdout


def _gh_json(args, cwd):
    return json.loads(_run(["gh"] + args, cwd) or "null")


def _gh_list(route, cwd):
    out = _run(["gh", "api", "--paginate", "--jq", ".[]", route], cwd)
    return [json.loads(line) for line in out.splitlines() if line.strip()]


def _story_pull_request(number, cwd):
    """The newest Ralph-managed pull request that references the Story, any state."""
    prs = _gh_json(["pr", "list", "--state", "all", "--limit", "100",
                    "--json", "number,body"], cwd) or []
    managed = [pr for pr in ralph_review.review_candidates(prs)
               if ralph_review_round.references_story(pr, number)]
    return max((pr["number"] for pr in managed), default=None)


def _has(oid, cwd):
    return subprocess.run(["git", "cat-file", "-e", "%s^{commit}" % oid], cwd=cwd,
                          stderr=subprocess.DEVNULL).returncode == 0


def _chain(pr, cwd):
    """(merge base, first-parent commits after it up to the head), oldest first."""
    head, base = pr["headRefOid"], pr["baseRefOid"]
    if not _has(head, cwd):
        _run(["git", "fetch", "-q", "origin", "refs/pull/%s/head" % pr["number"]], cwd)
    if not _has(base, cwd):
        _run(["git", "fetch", "-q", "origin", pr["baseRefName"]], cwd)
    merge_base = _run(["git", "merge-base", base, head], cwd).strip()
    out = _run(["git", "log", "--first-parent", "--reverse", "--format=%H %s",
                "%s..%s" % (merge_base, head)], cwd)
    commits = [{"oid": line[:40], "subject": line[41:]}
               for line in out.splitlines() if line.strip()]
    return merge_base, commits


def capture(number, cwd=None):
    try:
        repo_view = _gh_json(["repo", "view", "--json",
                              "name,owner,defaultBranchRef"], cwd)
        repo = {"owner": repo_view["owner"]["login"], "name": repo_view["name"],
                "defaultBranch": (repo_view.get("defaultBranchRef") or {}).get("name")}
        story = _gh_json(["issue", "view", str(number), "--json", ISSUE_FIELDS], cwd)
        _, parent = ralph_story._parse_parent(story.get("body") or "")
        prd = (_gh_json(["issue", "view", str(parent), "--json", ISSUE_FIELDS], cwd)
               if parent is not None else None)
        pr_number = _story_pull_request(story["number"], cwd)
        if pr_number is None:
            return CaptureResult(True, [], to_state(repo, story, prd=prd))
        pr = _gh_json(["pr", "view", str(pr_number), "--json", PR_FIELDS + ",url"], cwd)
        rest = _gh_json(["api", "repos/{owner}/{repo}/pulls/%s" % pr_number], cwd)
        pr["baseRefOid"] = rest["base"]["sha"]
        route = "repos/{owner}/{repo}/pulls/%s/%%s%s" % (pr_number, PER_PAGE)
        reviews = _gh_list(route % "reviews", cwd)
        threads = _gh_list(route % "comments", cwd)
        merge_base, commits = _chain(pr, cwd)
    except (RuntimeError, ValueError, KeyError) as exc:
        return CaptureResult(False, ["capture: %s" % exc])
    return CaptureResult(True, [], to_state(repo, story, pr, reviews, threads,
                                            commits, merge_base, prd))


def main(argv):
    positional, out_dir, redacting = [], ".", False
    i = 0
    while i < len(argv):
        if argv[i] == "--redact":
            redacting = True
            i += 1
            continue
        if argv[i] == "--out":
            if i + 1 >= len(argv):
                sys.stderr.write("ralph: --out requires a DIR\n")
                return 2
            out_dir = argv[i + 1]
            i += 2
            continue
        positional.append(argv[i])
        i += 1
    if len(positional) != 1 or not positional[0].lstrip("#").isdigit():
        sys.stderr.write("usage: ralph --capture STORY [--out DIR] [--redact]\n")
        return 2
    number = int(positional[0].lstrip("#"))
    result = capture(number)
    if not result.ok:
        for error in result.errors:
            sys.stderr.write("ralph: %s\n" % error)
        return 1
    state = redact(result.state) if redacting else result.state
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, "story-%d.json" % number)
    with open(path, "w") as fh:
        json.dump(state, fh, indent=1, sort_keys=True)
        fh.write("\n")
    print(path)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
