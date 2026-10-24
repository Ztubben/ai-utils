"""The one issue that tells the operator a Tick fell back (#149, ADR-0009).

When a self-hosted Tick's new `origin/main` fails preflight, the Tick runs the
Last-Known-Good Tooling Commit instead (#148) -- which keeps the Loop alive, and
would hide the breakage if nobody were told. So the first fallback opens one
machine-managed issue tagging `notify.github`, naming the failing commit, the
failed step and its output tail, and the commit running instead. Later
fallbacks update that issue (and comment when a *different* commit fails), so
a long outage is one issue, not one per Tick. When a new `main` passes again,
the issue gets a recovery comment and is closed.

The issue is found by the hidden marker in its body, never by its title. It
carries no `state:` label, so selection never takes it for work.

`plan(...)` is pure: the gh argv lists for one event, given the open issue (or
None). The CLI finds the issue and runs the plan, best-effort -- a GitHub error
is reported and never fails the Tick:

  ralph_fallback.py fallback FAILED STEP RUNNING TAIL_FILE [CONFIG]
  ralph_fallback.py recovered COMMIT [CONFIG]
"""
import json
import os
import re
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ralph_config  # noqa: E402

MARKER = "ralph-tooling-fallback"
MARKER_RE = re.compile(r"<!-- %s failed=([0-9a-f]+) -->" % MARKER)
TITLE = "Ralph is running Last-Known-Good tooling: origin/main fails preflight"
FALLBACK = "fallback"
RECOVERED = "recovered"


def body(failed, step, tail, running, notify):
    """The issue body for a fallback from *failed* to *running*."""
    handle = (notify or "").lstrip("@")
    tag = "@%s " % handle if handle else ""
    return "\n".join([
        "<!-- %s failed=%s -->" % (MARKER, failed),
        "%sthe Tooling Commit `%s` (origin/main) fails the tick's preflight at the "
        "**%s**, so every tick runs the Last-Known-Good commit `%s` instead "
        "(ADR-0009)." % (tag, failed, step, running),
        "",
        "Each tick retries the newest origin/main. Merge a fix to `main` and this "
        "issue closes itself once a tick's preflight passes again.",
        "",
        "Output tail of the failed %s:" % step,
        "",
        "```",
        (tail or "(no output)").rstrip("\n"),
        "```",
        "",
        "_Managed by Ralph: it is not work, carries no `state:` label, and is "
        "never selected._",
    ]) + "\n"


def failed_commit(issue_body):
    """The failing commit an issue body records, if it is the fallback issue."""
    match = MARKER_RE.search(issue_body or "")
    return match.group(1) if match else None


def find(issues):
    """The open fallback issue among *issues* (gh --json number,body), or None."""
    marked = [i for i in issues if failed_commit(i.get("body"))]
    return min(marked, key=lambda i: i["number"]) if marked else None


def plan(event, issue, notify=None, failed=None, step=None, tail=None,
         running=None, recovered=None):
    """The gh calls for *event* given the open fallback *issue* (or None)."""
    if event == FALLBACK:
        text = body(failed, step, tail, running, notify)
        if issue is None:
            return [["gh", "issue", "create", "--title", TITLE, "--body", text]]
        calls = [["gh", "issue", "edit", str(issue["number"]), "--body", text]]
        if failed_commit(issue.get("body")) != failed:
            calls.append(["gh", "issue", "comment", str(issue["number"]), "--body",
                          "The newest origin/main, `%s`, fails preflight too, at "
                          "the %s. Still running Last-Known-Good `%s`."
                          % (failed, step, running)])
        return calls
    if event == RECOVERED:
        if issue is None:
            return []
        number = str(issue["number"])
        return [["gh", "issue", "comment", number, "--body",
                 "Recovered: origin/main `%s` passes the tick's preflight, and "
                 "ticks run it again." % recovered],
                ["gh", "issue", "close", number]]
    raise ValueError(event)


def _open_issues():
    out = subprocess.run(["gh", "issue", "list", "--state", "open", "--limit", "1000",
                          "--json", "number,body"],
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if out.returncode != 0:
        raise RuntimeError(out.stderr.strip() or "gh issue list failed")
    return json.loads(out.stdout or "[]")


def run(event, config_path, **facts):
    """Find the issue and apply the plan. (ok, message)."""
    validated = ralph_config.load_and_validate(config_path)
    notify = validated.config["notify"]["github"] if validated.ok else None
    try:
        issue = find(_open_issues())
    except (RuntimeError, ValueError) as exc:
        return False, "cannot look for the fallback issue: %s" % exc
    calls = plan(event, issue, notify=notify, **facts)
    for argv in calls:
        proc = subprocess.run(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              text=True)
        if proc.returncode != 0:
            return False, "%s failed: %s" % (" ".join(argv[:3]), proc.stderr.strip())
    if event == FALLBACK:
        return True, ("opened the fallback issue" if issue is None
                      else "updated fallback issue #%s" % issue["number"])
    return True, ("closed fallback issue #%s" % issue["number"] if issue
                  else "no fallback issue was open")


def main(argv):
    if argv[:1] == [FALLBACK] and len(argv) >= 5:
        try:
            with open(argv[4]) as fh:
                tail = fh.read()
        except OSError:
            tail = ""
        ok, message = run(FALLBACK, argv[5] if len(argv) > 5 else ".ralph.yml",
                          failed=argv[1], step=argv[2], running=argv[3], tail=tail)
    elif argv[:1] == [RECOVERED] and len(argv) >= 2:
        ok, message = run(RECOVERED, argv[2] if len(argv) > 2 else ".ralph.yml",
                          recovered=argv[1])
    else:
        sys.stderr.write("usage: ralph_fallback.py fallback FAILED STEP RUNNING TAIL_FILE "
                         "[CONFIG] | recovered COMMIT [CONFIG]\n")
        return 2
    print(("OK: %s" if ok else "ERROR: %s") % message)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
