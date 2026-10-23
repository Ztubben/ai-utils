#!/usr/bin/env python3
"""Snapshot a Ralph backlog and flag the mechanical findings an audit starts from.

Read-only: it runs `gh issue list`, `gh pr list` and `git log`, never an edit.
Lint and dependency semantics are the Loop's own (`lib/ralph_story.py`,
`lib/ralph_select.py`), imported rather than re-implemented, so "satisfied"
here means exactly what the selection engine would decide.

Usage:
  backlog_snapshot.py [--backlog ISSUES.json] [--prs PRS.json] [--git DIR]
                      [--stale-days N] [--assume-closed N,M] [--json OUT.json]

Prints a markdown digest on stdout; --json also writes the full structured
snapshot. --backlog/--prs take saved `gh ... --json` output (offline runs).
--assume-closed treats the named issues as closed, to preview what proposed
closes would unblock and what the Loop would select next.
"""
import argparse
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path


def _lib_dir():
    home = os.environ.get("RALPH_HOME")
    candidates = [Path(home) / "lib"] if home else []
    candidates.append(Path(__file__).resolve().parents[3] / "lib")
    for c in candidates:
        if (c / "ralph_story.py").is_file():
            return c
    sys.exit("backlog_snapshot: cannot find ai-utils lib/ (set RALPH_HOME)")


sys.path.insert(0, str(_lib_dir()))
import ralph_select  # noqa: E402
import ralph_story  # noqa: E402

ISSUE_FIELDS = "number,title,labels,body,state,stateReason,updatedAt,closedAt"
PR_FIELDS = "number,title,body,state,headRefName,mergedAt,closingIssuesReferences"
CLOSING = re.compile(r"\b(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)\s+#(\d+)", re.I)
MENTION = re.compile(r"#(\d+)\b")


def _gh(args):
    out = subprocess.run(["gh"] + args, stdout=subprocess.PIPE, check=True,
                         text=True).stdout
    return json.loads(out or "[]")


def _load(path, gh_args):
    if path:
        with open(path) as fh:
            return json.load(fh)
    return _gh(gh_args)


def _commit_refs(git_dir):
    """Map issue number -> commits (all refs) whose *subject* names #N.

    Only the subject counts: squash bodies list every issue the branch touched,
    which buries the one the commit is for. A subject seen on several refs
    (a commit on its story branch, the same change squashed onto main) is
    listed once.
    """
    try:
        out = subprocess.run(
            ["git", "-C", git_dir, "log", "--all", "--format=%h%x1f%s%x1e"],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, check=True,
            text=True).stdout
    except (OSError, subprocess.CalledProcessError):
        return {}
    refs, seen = {}, set()
    for record in out.split("\x1e"):
        parts = record.strip().split("\x1f")
        if len(parts) < 2 or parts[1] in seen:
            continue
        sha, subject = parts[0], parts[1]
        seen.add(subject)
        for n in set(MENTION.findall(subject)):
            refs.setdefault(int(n), []).append("%s %s" % (sha, subject))
    return refs


def _pr_refs(prs):
    """Map issue number -> PRs that close or mention it."""
    refs = {}
    for pr in prs:
        text = "%s\n%s" % (pr.get("title") or "", pr.get("body") or "")
        closing = {int(n) for n in CLOSING.findall(text)}
        closing |= {r.get("number") for r in pr.get("closingIssuesReferences") or []
                    if r.get("number")}
        mentioned = {int(n) for n in MENTION.findall(text)}
        for n in closing | mentioned:
            refs.setdefault(n, []).append({
                "pr": pr["number"], "state": pr.get("state"),
                "merged": bool(pr.get("mergedAt")), "closes": n in closing,
                "head": pr.get("headRefName"), "title": pr.get("title")})
    return refs


def _days_since(stamp, now):
    if not stamp:
        return None
    t = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    return (now - t).days


def _find_cycles(stories):
    graph = {s["number"]: list(s["depends_on"]) for s in stories if s["number"]}
    cycles, seen = [], set()

    def walk(node, path):
        if node in path:
            cyc = path[path.index(node):]
            key = frozenset(cyc)
            if key not in seen:
                seen.add(key)
                cycles.append(cyc + [node])
            return
        for nxt in graph.get(node, []):
            if nxt in graph:
                walk(nxt, path + [node])

    for start in graph:
        walk(start, [])
    return cycles


def snapshot(raw_issues, prs, commits, stale_days, now):
    stories = ralph_select.normalize(raw_issues)
    by_number = {s["number"]: s for s in stories if s["number"] is not None}
    raw_by_number = {r["number"]: r for r in raw_issues}
    pr_refs = _pr_refs(prs)
    children = {}
    for s in stories:
        if s.get("parent") is not None:
            children.setdefault(s["parent"], []).append(s["number"])

    report = []
    for s in stories:
        if s.get("closed"):
            continue
        n = s["number"]
        raw = raw_by_number[n]
        lint = ralph_story.validate_story(raw)
        kind = ("prd" if s.get("is_prd") else
                "blocker" if s.get("is_blocker") else "story")
        flags = []

        # Shape.
        if not lint.ok:
            flags.append({"check": "lint", "detail": lint.errors})

        # A near-miss heading the linter reads as missing (e.g. lower-case).
        if (not lint.ok and re.search(r"^\s*#{1,6}\s+acceptance criteria\s*$",
                                      raw.get("body") or "", re.I | re.M)
                and not re.search(r"^\s*#{1,6}\s+Acceptance Criteria\s*$",
                                  raw.get("body") or "", re.M)):
            flags.append({"check": "lint", "detail": [
                "body: has an acceptance-criteria heading the linter does not "
                "recognize; it must read exactly `## Acceptance Criteria`"]})

        # Dependencies, own and inherited from the PRD, with the Loop's rules.
        deps = []
        for d in ralph_select._effective_deps(s, by_number):
            target = by_number.get(d)
            inherited = d not in s["depends_on"]
            if d == n:
                status = "self-dependency"
            elif target is None:
                status = "missing (Loop treats as satisfied)"
            elif ralph_select._dep_satisfied(s, target):
                reason = (raw_by_number[d].get("stateReason") or "").upper()
                status = ("closed NOT_PLANNED (Loop treats as satisfied)"
                          if reason == "NOT_PLANNED" else "satisfied")
            elif target.get("closed"):
                status = "unreachable: closed story of another Feature"
            else:
                status = "open (%s)" % (target.get("state") or
                                        ("prd" if target.get("is_prd") else
                                         "blocker" if target.get("is_blocker")
                                         else "no state"))
            deps.append({"dep": d, "inherited": inherited, "status": status,
                         "title": raw_by_number.get(d, {}).get("title")})
        unsatisfied = [x for x in deps if x["status"] != "satisfied"]
        if kind == "story" and s.get("state") == "ready" and unsatisfied:
            flags.append({"check": "waiting",
                          "detail": ["waits on #%d: %s" % (x["dep"], x["status"])
                                     for x in unsatisfied]})
        odd = [x for x in deps if "Loop treats" in x["status"]
               or x["status"].startswith(("unreachable", "self"))]
        if odd:
            flags.append({"check": "dependency-suspect",
                          "detail": ["#%d: %s" % (x["dep"], x["status"]) for x in odd]})
        if deps and not unsatisfied and kind == "blocker":
            flags.append({"check": "blocker-resolved?",
                          "detail": ["every dependency is satisfied"]})

        # Parent.
        parent = s.get("parent")
        if parent is not None:
            p = by_number.get(parent)
            if p is None:
                flags.append({"check": "parent", "detail": ["Parent #%d not found" % parent]})
            elif not p.get("is_prd"):
                flags.append({"check": "parent", "detail": ["Parent #%d is not a PRD" % parent]})
            elif p.get("closed"):
                flags.append({"check": "parent",
                              "detail": ["Parent PRD #%d is closed but this story is open" % parent]})

        # PRD roll-up.
        if kind == "prd":
            kids = children.get(n, [])
            open_kids = [k for k in kids if not by_number[k].get("closed")]
            if kids and not open_kids and s.get("state") != "ready":
                flags.append({"check": "prd-stuck",
                              "detail": ["all %d child stories are closed, but without "
                                         "state:ready the Feature is never integrated" % len(kids)]})
            if not kids:
                flags.append({"check": "prd-empty", "detail": ["no story names it Parent:"]})

        # Evidence the work already landed.
        evidence = {"commits": commits.get(n, []), "prs": pr_refs.get(n, [])}
        merged_closing = [p for p in evidence["prs"] if p["merged"] and p["closes"]]
        if kind != "prd" and (evidence["commits"] or merged_closing):
            flags.append({"check": "maybe-implemented",
                          "detail": evidence["commits"][:5] +
                          ["PR #%d merged (closes #%d)" % (p["pr"], n) for p in merged_closing]})
        if s.get("state") == "in-review":
            open_prs = [p for p in evidence["prs"] if p["state"] == "OPEN"]
            merged = [p for p in evidence["prs"] if p["merged"]]
            if merged and not open_prs:
                flags.append({"check": "state-drift",
                              "detail": ["in-review but its PR is merged (finish: close it)"]})
            elif not open_prs:
                flags.append({"check": "state-drift", "detail": ["in-review with no open PR"]})

        # Staleness and halts.
        age = _days_since(raw.get("updatedAt"), now)
        if age is not None and age >= stale_days:
            flags.append({"check": "stale", "detail": ["untouched for %d days" % age]})
        if s.get("needs_human"):
            flags.append({"check": "halt", "detail": ["carries needs-human: the Loop is halted"]})

        report.append({
            "number": n, "title": raw.get("title"), "kind": kind,
            "state": s.get("state"), "type": s.get("type"), "prio": s.get("prio"),
            "parent": parent, "depends_on": deps, "lint_ok": lint.ok,
            "updated_days_ago": age, "evidence": evidence, "flags": flags})

    leftover = sorted(
        (s["number"], s["state"]) for s in stories
        if s.get("closed") and s.get("state") and s["number"] is not None)
    return {"generated": now.isoformat(), "open": report,
            "closed_with_state": [{"number": n, "state": st} for n, st in leftover],
            "cycles": _find_cycles([s for s in stories if not s.get("closed")]),
            "next_action": _next_action(raw_issues)}


def _next_action(raw_issues):
    action = ralph_select.next_action(raw_issues)
    return "%s #%s" % (action.kind, action.number) if action.number else action.kind


def render(snap):
    lines = ["# Backlog snapshot (%s)" % snap["generated"][:10], "",
             "Loop would do next: **%s**" % snap["next_action"], ""]
    if snap["cycles"]:
        lines += ["## Dependency cycles", ""]
        lines += ["- " + " -> ".join("#%d" % n for n in c) for c in snap["cycles"]]
        lines.append("")
    if snap["closed_with_state"]:
        lines += ["## Closed issues still carrying a state: label", "",
                  ", ".join("#%d (state:%s)" % (c["number"], c["state"])
                            for c in snap["closed_with_state"]), ""]
    flagged = [r for r in snap["open"] if r["flags"]]
    clean = [r for r in snap["open"] if not r["flags"]]
    lines += ["## Flagged (%d of %d open)" % (len(flagged), len(snap["open"])), ""]
    for r in sorted(flagged, key=lambda r: r["number"]):
        lines.append("### #%d %s  `%s/%s/%s prio:%s`" % (
            r["number"], r["title"], r["kind"], r["state"], r["type"], r["prio"]))
        for f in r["flags"]:
            for d in f["detail"]:
                lines.append("- **%s**: %s" % (f["check"], d))
        lines.append("")
    if clean:
        lines += ["## No mechanical flags", "",
                  ", ".join("#%d" % r["number"] for r in sorted(clean, key=lambda r: r["number"])),
                  ""]
    return "\n".join(lines)


def main(argv):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--backlog", help="saved `gh issue list --state all --json %s`" % ISSUE_FIELDS)
    ap.add_argument("--prs", help="saved `gh pr list --state all --json %s`" % PR_FIELDS)
    ap.add_argument("--git", default=".", help="repository whose history to scan")
    ap.add_argument("--stale-days", type=int, default=45)
    ap.add_argument("--assume-closed", default="",
                    help="comma-separated issues to treat as closed (what-if)")
    ap.add_argument("--json", dest="json_out", help="also write the full snapshot here")
    args = ap.parse_args(argv)

    issues = _load(args.backlog, ["issue", "list", "--state", "all", "--limit", "1000",
                                  "--json", ISSUE_FIELDS])
    assumed = {int(n.strip().lstrip("#")) for n in args.assume_closed.split(",") if n.strip()}
    for issue in issues:
        if issue["number"] in assumed:
            issue["state"], issue["stateReason"] = "CLOSED", "COMPLETED"
    prs = _load(args.prs, ["pr", "list", "--state", "all", "--limit", "500",
                           "--json", PR_FIELDS])
    snap = snapshot(issues, prs, _commit_refs(args.git), args.stale_days,
                    datetime.now(timezone.utc))
    if args.json_out:
        with open(args.json_out, "w") as fh:
            json.dump(snap, fh, indent=2)
    print(render(snap))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
