"""Unit tests for the freshness-merge decision (US-030, ADR-0006).

When starting a Feature story with an out-of-Feature dependency (a closed
Orphan Story or closed PRD), Ralph merges the base branch into the feature
branch before the iteration begins — a merge, never a rebase, so bench
anchors survive. A story with only same-Feature dependencies (or none) emits
no merge.

The pure decision logic lives in `ralph_select.needs_freshness_merge`; the
orchestrator (`bin/ralph.sh`) acts on it by running `git merge <base>`.
"""
import json
import os
import stat
import subprocess
import sys
import tempfile
import textwrap
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
LIB_DIR = os.path.join(REPO_ROOT, "lib")
RALPH_SH = os.path.join(REPO_ROOT, "bin", "ralph.sh")
FULL_CONFIG = os.path.join(REPO_ROOT, "test", "fixtures", "config", "valid", "full.yml")

sys.path.insert(0, LIB_DIR)
import ralph_select  # noqa: E402


# ---------------------------------------------------------------------------
# Fixture helpers (same shape as test_select_story.py)
# ---------------------------------------------------------------------------

def story(number, state=None, type_=None, prio=1, depends=None, closed=False,
          blocker=False, needs_human=False, bench=False, parent=None, prd=False):
    """Build a story in `gh issue --json number,title,labels,body,state` shape."""
    labels = []
    if state:
        labels.append("state:" + state)
    if type_:
        labels.append("type:" + type_)
    if prio is not None:
        labels.append("prio:%d" % prio)
    if blocker:
        labels.append("ready-for-human")
    if needs_human:
        labels.append("needs-human")
    if prd:
        labels.append("prd")
    body = "## Acceptance Criteria\n- [ ] do the thing\n"
    if bench:
        body += "\n## Bench Test Procedure\n1. flash and observe\n"
    dep_str = ", ".join("#%d" % d for d in depends) if depends else "None"
    parent_str = "#%d" % parent if parent is not None else "None"
    body += "\nParent: %s\nDepends on: %s\n" % (parent_str, dep_str)
    return {
        "number": number,
        "title": "S%d" % number,
        "labels": [{"name": n} for n in labels],
        "body": body,
        "state": "CLOSED" if closed else "OPEN",
    }


# ---------------------------------------------------------------------------
# Pure logic tests
# ---------------------------------------------------------------------------

class NeedsFreshnessMerge(unittest.TestCase):
    """ralph_select.needs_freshness_merge(story, backlog) returns True only
    when the story is a Feature story with at least one out-of-Feature
    dependency that is closed (code landed in base)."""

    def needs(self, target, *all_stories):
        backlog = ralph_select.normalize(list(all_stories))
        target_norm = [s for s in backlog if s["number"] == target["number"]][0]
        return ralph_select.needs_freshness_merge(target_norm, backlog)

    # -- AC: story with out-of-Feature dep emits merge --

    def test_feature_story_depending_on_closed_orphan(self):
        # Orphan #4 is closed (merged into base); Feature story #11 (parent #10)
        # depends on it => freshness merge needed.
        self.assertTrue(self.needs(
            story(11, state="ready", type_="afk", prio=1, parent=10, depends=[4]),
            story(10, prio=None, prd=True),
            story(4, state="ready", type_="afk", prio=9, closed=True),
            story(11, state="ready", type_="afk", prio=1, parent=10, depends=[4]),
        ))

    def test_feature_story_depending_on_closed_prd(self):
        # PRD #5 closed = its Feature merged into base; Feature story #11
        # (parent #10) depends on it => freshness merge needed.
        self.assertTrue(self.needs(
            story(11, state="ready", type_="afk", prio=1, parent=10, depends=[5]),
            story(5, prio=None, prd=True, closed=True),
            story(10, prio=None, prd=True, depends=[5]),
            story(11, state="ready", type_="afk", prio=1, parent=10, depends=[5]),
        ))

    def test_feature_story_with_inherited_prd_dep_on_closed_orphan(self):
        # PRD #10 depends on Orphan #4 (closed); Feature story #11 inherits
        # that dep => freshness merge needed.
        self.assertTrue(self.needs(
            story(11, state="ready", type_="afk", prio=1, parent=10),
            story(10, prio=None, prd=True, depends=[4]),
            story(4, state="ready", type_="afk", prio=9, closed=True),
            story(11, state="ready", type_="afk", prio=1, parent=10),
        ))

    # -- AC: same-Feature deps or no deps => no merge --

    def test_feature_story_with_no_deps(self):
        self.assertFalse(self.needs(
            story(11, state="ready", type_="afk", prio=1, parent=10),
            story(10, prio=None, prd=True),
            story(11, state="ready", type_="afk", prio=1, parent=10),
        ))

    def test_feature_story_depending_on_same_feature_sibling(self):
        # #12 is a closed sibling on the same feature branch (parent #10) —
        # its code is already on the feature branch, no merge needed.
        self.assertFalse(self.needs(
            story(13, state="ready", type_="afk", prio=1, parent=10, depends=[12]),
            story(10, prio=None, prd=True),
            story(12, state="ready", type_="afk", prio=9, parent=10, closed=True),
            story(13, state="ready", type_="afk", prio=1, parent=10, depends=[12]),
        ))

    # -- AC: Orphan Stories never need freshness merge (they work on base) --

    def test_orphan_story_with_closed_dep_does_not_need_freshness(self):
        # An Orphan Story branches off base directly; no feature branch to freshen.
        self.assertFalse(self.needs(
            story(3, state="ready", type_="afk", prio=1, depends=[4]),
            story(4, state="ready", type_="afk", prio=9, closed=True),
            story(3, state="ready", type_="afk", prio=1, depends=[4]),
        ))

    # -- Edge: dep is open (not yet satisfied) => no merge (code not in base) --

    def test_feature_story_with_open_orphan_dep_no_merge(self):
        # Orphan #4 is open (not merged) — nothing to merge in.
        self.assertFalse(self.needs(
            story(11, state="ready", type_="afk", prio=1, parent=10, depends=[4]),
            story(10, prio=None, prd=True),
            story(4, state="ready", type_="afk", prio=9),
            story(11, state="ready", type_="afk", prio=1, parent=10, depends=[4]),
        ))

    # -- Edge: dep absent from backlog => no merge (treated as done, not fresh) --

    def test_dep_absent_from_backlog_does_not_trigger_merge(self):
        # A dep absent from the scanned backlog is treated as "already done" by
        # the selection engine; we cannot know if it's an Orphan or Feature story,
        # so we conservatively skip the merge (it may have been satisfied long ago).
        self.assertFalse(self.needs(
            story(11, state="ready", type_="afk", prio=1, parent=10, depends=[999]),
            story(10, prio=None, prd=True),
            story(11, state="ready", type_="afk", prio=1, parent=10, depends=[999]),
        ))


# ---------------------------------------------------------------------------
# Orchestration tests (bin/ralph.sh freshness merge)
# ---------------------------------------------------------------------------

def _write_exec(path, contents):
    with open(path, "w") as fh:
        fh.write(contents)
    os.chmod(path, os.stat(path).st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)


class FreshnessTickHarness:
    """A throwaway superproject with mock CLIs that logs git merge calls."""

    def __init__(self, tmp):
        self.tmp = tmp
        self.log = os.path.join(tmp, "ralph.log")
        self.queue = os.path.join(tmp, "ghq")
        os.makedirs(self.queue)
        os.makedirs(os.path.join(tmp, ".git"))
        os.makedirs(os.path.join(tmp, "mockbin"))
        with open(FULL_CONFIG) as fh:
            with open(os.path.join(tmp, ".ralph.yml"), "w") as out:
                out.write(fh.read())
        self._write_mocks()

    def set_backlogs(self, *backlogs):
        for i, bl in enumerate(backlogs):
            with open(os.path.join(self.queue, "%d.json" % i), "w") as fh:
                json.dump(bl, fh)

    def set_view_story(self, s):
        with open(os.path.join(self.queue, "story.json"), "w") as fh:
            json.dump(s, fh)

    def _write_mocks(self):
        mb = os.path.join(self.tmp, "mockbin")
        _write_exec(os.path.join(mb, "gh"), textwrap.dedent("""\
            #!/usr/bin/env bash
            echo "gh $*" >> "$RALPH_LOG"
            if [[ "$1 $2" == "issue list" ]]; then
              n=$(cat "$RALPH_GH_QUEUE_DIR/counter" 2>/dev/null || echo 0)
              echo $((n + 1)) > "$RALPH_GH_QUEUE_DIR/counter"
              f="$RALPH_GH_QUEUE_DIR/$n.json"
              if [[ -f "$f" ]]; then cat "$f"; else echo "[]"; fi
            elif [[ "$1 $2" == "issue view" ]]; then
              cat "$RALPH_GH_QUEUE_DIR/story.json"
            fi
            """))
        _write_exec(os.path.join(mb, "claude"), textwrap.dedent("""\
            #!/usr/bin/env bash
            cat > /dev/null
            echo "claude action=${RALPH_ITERATION_ACTION:-} issue=${RALPH_ITERATION_ISSUE:-}" >> "$RALPH_LOG"
            [[ -n "${RALPH_CLAUDE_EMIT:-}" ]] && printf '%s\\n' "$RALPH_CLAUDE_EMIT"
            exit "${RALPH_CLAUDE_EXIT:-0}"
            """))
        _write_exec(os.path.join(mb, "git"), textwrap.dedent("""\
            #!/usr/bin/env bash
            echo "git $*" >> "$RALPH_LOG"
            exit 0
            """))

    def env(self, claude_exit="0", claude_emit=""):
        e = dict(os.environ)
        e["PATH"] = os.path.join(self.tmp, "mockbin") + os.pathsep + e["PATH"]
        e["RALPH_LOG"] = self.log
        e["RALPH_GH_QUEUE_DIR"] = self.queue
        e["RALPH_SESSION_LIMIT_EXIT"] = "91"
        e["RALPH_CLAUDE_EXIT"] = claude_exit
        e["RALPH_CLAUDE_EMIT"] = claude_emit
        return e

    def run(self, claude_exit="0", claude_emit=""):
        return subprocess.run([RALPH_SH], cwd=self.tmp,
                              env=self.env(claude_exit, claude_emit),
                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)

    def log_lines(self):
        if not os.path.exists(self.log):
            return []
        with open(self.log) as fh:
            return [ln.rstrip("\n") for ln in fh if ln.strip()]


def _feature_story(number, state, type_="afk", prio=1, parent=10, depends=None,
                   closed=False):
    """A Feature story (Parent: #N) in gh --json shape."""
    return story(number, state=state, type_=type_, prio=prio, parent=parent,
                 depends=depends, closed=closed)


class FreshnessOrchestration(unittest.TestCase):
    """Verify that bin/ralph.sh merges base into the feature branch when a
    Feature story has out-of-Feature dependencies, and skips it otherwise."""

    def harness(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(tmp, ignore_errors=True))
        return FreshnessTickHarness(tmp)

    def test_start_with_out_of_feature_dep_merges_base(self):
        """Starting a Feature story that depends on a closed Orphan #4 must
        run `git merge develop` before the iteration."""
        h = self.harness()
        backlog = [
            story(10, prio=None, prd=True),                                  # PRD
            story(4, type_="afk", prio=9, closed=True),                      # closed Orphan
            _feature_story(11, "ready", depends=[4]),                        # story to start
        ]
        # Queue: 0=dry-run, 1=needs-freshness, 2=next dry-run (no-work),
        # 3=ready-features scan
        h.set_backlogs(backlog, backlog, [], [])
        proc = h.run()
        self.assertEqual(proc.returncode, 0, proc.stdout)
        log = h.log_lines()
        # Must see a git merge of the base branch (develop) before claude runs.
        merge_lines = [ln for ln in log if "git merge" in ln and "develop" in ln]
        self.assertTrue(merge_lines, "expected 'git merge develop' in log: %s" % log)
        # The merge must come before the claude call.
        merge_idx = next(i for i, ln in enumerate(log) if "git merge" in ln and "develop" in ln)
        claude_idx = next(i for i, ln in enumerate(log) if ln.startswith("claude "))
        self.assertLess(merge_idx, claude_idx,
                        "merge must precede iteration: %s" % log)

    def test_start_with_no_out_of_feature_deps_skips_merge(self):
        """Starting a Feature story with only same-Feature deps does not merge."""
        h = self.harness()
        backlog = [
            story(10, prio=None, prd=True),                                  # PRD
            _feature_story(12, "ready", closed=True),                        # closed sibling
            _feature_story(13, "ready", depends=[12]),                       # story to start
        ]
        # Queue: 0=dry-run, 1=needs-freshness, 2=next dry-run (no-work),
        # 3=ready-features scan
        h.set_backlogs(backlog, backlog, [], [])
        proc = h.run()
        self.assertEqual(proc.returncode, 0, proc.stdout)
        log = h.log_lines()
        merge_lines = [ln for ln in log if "git merge" in ln]
        self.assertFalse(merge_lines,
                         "no merge expected for same-Feature deps: %s" % log)

    def test_orphan_story_never_triggers_freshness_merge(self):
        """An Orphan Story branches off base; no feature branch to freshen."""
        h = self.harness()
        backlog = [
            story(4, type_="afk", prio=9, closed=True),
            story(3, state="ready", type_="afk", prio=1, depends=[4]),
        ]
        # Queue: 0=dry-run, 1=needs-freshness, 2=next dry-run (no-work),
        # 3=ready-features scan
        h.set_backlogs(backlog, backlog, [], [])
        proc = h.run()
        self.assertEqual(proc.returncode, 0, proc.stdout)
        log = h.log_lines()
        merge_lines = [ln for ln in log if "git merge" in ln]
        self.assertFalse(merge_lines,
                         "no merge expected for Orphan story: %s" % log)

    def test_resume_with_out_of_feature_dep_merges_base(self):
        """Resuming a Feature story with an out-of-Feature dep that became
        satisfied since the last checkpoint also merges base."""
        h = self.harness()
        backlog = [
            story(10, prio=None, prd=True),
            story(4, type_="afk", prio=9, closed=True),
            _feature_story(11, "in-progress", depends=[4]),
        ]
        # Queue: 0=dry-run, 1=needs-freshness, 2=next dry-run (no-work),
        # 3=ready-features scan
        h.set_backlogs(backlog, backlog, [], [])
        proc = h.run()
        self.assertEqual(proc.returncode, 0, proc.stdout)
        log = h.log_lines()
        merge_lines = [ln for ln in log if "git merge" in ln and "develop" in ln]
        self.assertTrue(merge_lines, "expected freshness merge on resume: %s" % log)


if __name__ == "__main__":
    unittest.main()
