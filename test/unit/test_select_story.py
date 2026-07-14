"""Unit tests for the pure story-selection engine (US-004, ADR-0002).

Selection is a pure function over a normalized backlog: it decides the next
action Ralph should take (resume #N / start #N / no-work / halt) and changes
nothing. Stories are fed in `gh issue view --json` shape and normalized before
selection, exactly as `ralph --dry-run` will feed the live backlog.
"""
import json
import os
import subprocess
import sys
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
LIB_DIR = os.path.join(REPO_ROOT, "lib")
BACKLOGS = os.path.join(REPO_ROOT, "test", "fixtures", "backlogs")
RALPH = os.path.join(REPO_ROOT, "bin", "ralph")

sys.path.insert(0, LIB_DIR)
import ralph_select  # noqa: E402


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


def action_for(*stories):
    return ralph_select.next_action(list(stories))


class ResumeFirst(unittest.TestCase):
    def test_in_progress_is_chosen_before_any_ready_scan(self):
        act = action_for(
            story(1, state="ready", type_="afk", prio=1),
            story(2, state="in-progress", type_="afk", prio=9),
        )
        self.assertEqual(act.kind, ralph_select.RESUME)
        self.assertEqual(act.number, 2)

    def test_lowest_prio_then_number_among_in_progress(self):
        act = action_for(
            story(5, state="in-progress", type_="afk", prio=2),
            story(2, state="in-progress", type_="afk", prio=1),
            story(3, state="in-progress", type_="afk", prio=1),
        )
        self.assertEqual((act.kind, act.number), (ralph_select.RESUME, 2))


class Ordering(unittest.TestCase):
    def test_lower_prio_number_wins(self):
        act = action_for(
            story(3, state="ready", type_="afk", prio=2),
            story(1, state="ready", type_="afk", prio=1),
        )
        self.assertEqual((act.kind, act.number), (ralph_select.START, 1))

    def test_tie_breaks_on_lowest_issue_number_fifo(self):
        act = action_for(
            story(7, state="ready", type_="afk", prio=1),
            story(4, state="ready", type_="afk", prio=1),
        )
        self.assertEqual(act.number, 4)

    def test_afk_beats_hil_on_priority_tie_despite_higher_number(self):
        # Amended ADR-0002: within the same prio:N the ordering key ranks
        # type:afk before type:hil, then falls back to issue-number FIFO.
        act = action_for(
            story(3, state="ready", type_="hil", prio=1, bench=True),
            story(8, state="ready", type_="afk", prio=1),
        )
        self.assertEqual((act.kind, act.number), (ralph_select.START, 8))

    def test_afk_beats_hil_among_prioless_stories(self):
        act = action_for(
            story(3, state="ready", type_="hil", prio=None, bench=True),
            story(8, state="ready", type_="afk", prio=None),
        )
        self.assertEqual((act.kind, act.number), (ralph_select.START, 8))

    def test_explicit_prio_still_dominates_type_rank(self):
        # A prio:1 HIL story beats a prio-less AFK story: the type rank only
        # breaks exact priority ties, it never overrides an encoded prio:N.
        act = action_for(
            story(1, state="ready", type_="afk", prio=None),
            story(2, state="ready", type_="hil", prio=1, bench=True),
        )
        self.assertEqual((act.kind, act.number), (ralph_select.START, 2))

    def test_fifo_preserved_within_same_prio_and_type(self):
        act = action_for(
            story(9, state="ready", type_="hil", prio=1, bench=True),
            story(5, state="ready", type_="hil", prio=1, bench=True),
        )
        self.assertEqual(act.number, 5)

    def test_story_without_prio_sorts_behind_prioritized_ones(self):
        # prio is optional: a no-prio story (prio=None -> +inf) loses to any
        # story that carries a prio:N, regardless of issue number.
        act = action_for(
            story(1, state="ready", type_="afk", prio=None),  # lower number...
            story(9, state="ready", type_="afk", prio=5),      # ...but this wins
        )
        self.assertEqual((act.kind, act.number), (ralph_select.START, 9))

    def test_prioless_stories_fall_back_to_fifo_among_themselves(self):
        act = action_for(
            story(7, state="ready", type_="afk", prio=None),
            story(4, state="ready", type_="afk", prio=None),
        )
        self.assertEqual(act.number, 4)

    def test_blocked_state_is_skipped(self):
        act = action_for(
            story(1, state="blocked", type_="afk", prio=1),
            story(2, state="ready", type_="afk", prio=2),
        )
        self.assertEqual((act.kind, act.number), (ralph_select.START, 2))

    def test_design_decision_blocker_is_skipped(self):
        act = action_for(
            story(1, blocker=True, prio=1),  # ready-for-human, no state:ready
            story(2, state="ready", type_="afk", prio=2),
        )
        self.assertEqual((act.kind, act.number), (ralph_select.START, 2))


class Dependencies(unittest.TestCase):
    def test_afk_dependency_still_open_blocks_selection(self):
        # #2 has the better prio but depends on an unmerged (open) AFK story.
        act = action_for(
            story(1, state="ready", type_="afk", prio=9),
            story(2, state="ready", type_="afk", prio=1, depends=[1]),
        )
        self.assertEqual((act.kind, act.number), (ralph_select.START, 1))

    def test_afk_dependency_merged_closed_is_satisfied(self):
        act = action_for(
            story(1, state="ready", type_="afk", prio=9, closed=True),
            story(2, state="ready", type_="afk", prio=1, depends=[1]),
        )
        self.assertEqual((act.kind, act.number), (ralph_select.START, 2))

    def test_hil_dependency_awaiting_bench_is_not_satisfied(self):
        # A HIL dep is satisfied only once bench-verified (closed); awaiting-bench
        # is still open, so the dependent is never selected.
        act = action_for(
            story(1, state="awaiting-bench", type_="hil", prio=1, bench=True),
            story(2, state="ready", type_="afk", prio=2, depends=[1]),
        )
        self.assertEqual(act.kind, ralph_select.NO_WORK)

    def test_hil_dependency_bench_verified_closed_is_satisfied(self):
        act = action_for(
            story(1, state="awaiting-bench", type_="hil", prio=1, bench=True, closed=True),
            story(2, state="ready", type_="afk", prio=2, depends=[1]),
        )
        self.assertEqual((act.kind, act.number), (ralph_select.START, 2))

    def test_dependency_absent_from_backlog_is_treated_as_done(self):
        act = action_for(story(2, state="ready", type_="afk", prio=1, depends=[99]))
        self.assertEqual((act.kind, act.number), (ralph_select.START, 2))


class PrdNeverSelected(unittest.TestCase):
    def test_ready_prd_is_never_started(self):
        # A prd-labeled issue is not a story (ADR-0002): even carrying
        # state:ready it must never come back as a start action.
        act = action_for(story(10, state="ready", prio=1, prd=True))
        self.assertEqual(act.kind, ralph_select.NO_WORK)

    def test_in_progress_prd_is_never_resumed(self):
        act = action_for(
            story(10, state="in-progress", prio=1, prd=True),
            story(2, state="ready", type_="afk", prio=2),
        )
        self.assertEqual((act.kind, act.number), (ralph_select.START, 2))

    def test_ready_prd_does_not_shadow_a_ready_story(self):
        act = action_for(
            story(10, state="ready", prio=1, prd=True),
            story(2, state="ready", type_="afk", prio=2),
        )
        self.assertEqual((act.kind, act.number), (ralph_select.START, 2))


class InheritedPrdDependencies(unittest.TestCase):
    def test_feature_story_ineligible_while_its_prd_dep_is_open(self):
        # PRD #10 depends on PRD #5 (cross-Feature ordering); story #11 of
        # Feature #10 inherits that unsatisfied edge and stays ineligible.
        act = action_for(
            story(5, prio=None, prd=True),
            story(10, prio=None, prd=True, depends=[5]),
            story(11, state="ready", type_="afk", prio=1, parent=10),
        )
        self.assertEqual(act.kind, ralph_select.NO_WORK)

    def test_feature_story_eligible_once_prd_dep_closes(self):
        act = action_for(
            story(5, prio=None, prd=True, closed=True),
            story(10, prio=None, prd=True, depends=[5]),
            story(11, state="ready", type_="afk", prio=1, parent=10),
        )
        self.assertEqual((act.kind, act.number), (ralph_select.START, 11))


class DependencyReachability(unittest.TestCase):
    def test_closed_same_feature_dep_is_satisfied(self):
        act = action_for(
            story(12, state="ready", type_="afk", prio=9, parent=10, closed=True),
            story(13, state="ready", type_="afk", prio=1, parent=10, depends=[12]),
        )
        self.assertEqual((act.kind, act.number), (ralph_select.START, 13))

    def test_closed_dep_on_another_features_story_is_never_satisfied(self):
        # #12 is closed on Feature #10's unmerged feature branch: its code is
        # unreachable from Feature #20, so the edge is never satisfied.
        act = action_for(
            story(12, state="ready", type_="afk", prio=9, parent=10, closed=True),
            story(21, state="ready", type_="afk", prio=1, parent=20, depends=[12]),
        )
        self.assertEqual(act.kind, ralph_select.NO_WORK)

    def test_orphan_depending_on_a_feature_story_is_never_satisfied(self):
        act = action_for(
            story(12, state="ready", type_="afk", prio=9, parent=10, closed=True),
            story(3, state="ready", type_="afk", prio=1, depends=[12]),
        )
        self.assertEqual(act.kind, ralph_select.NO_WORK)

    def test_orphan_depending_on_a_closed_orphan_is_satisfied(self):
        act = action_for(
            story(4, state="ready", type_="afk", prio=9, closed=True),
            story(3, state="ready", type_="afk", prio=1, depends=[4]),
        )
        self.assertEqual((act.kind, act.number), (ralph_select.START, 3))

    def test_dep_on_a_closed_prd_is_satisfied(self):
        # A closed PRD means the whole Feature merged into the base branch
        # (CONTEXT.md), so any story may depend on it closed-means-satisfied.
        act = action_for(
            story(10, prio=None, prd=True, closed=True),
            story(3, state="ready", type_="afk", prio=1, depends=[10]),
        )
        self.assertEqual((act.kind, act.number), (ralph_select.START, 3))

    def test_dep_on_an_open_prd_is_not_satisfied(self):
        act = action_for(
            story(10, prio=None, prd=True),
            story(3, state="ready", type_="afk", prio=1, depends=[10]),
        )
        self.assertEqual(act.kind, ralph_select.NO_WORK)


class FeatureCompletionScan(unittest.TestCase):
    """PRDs eligible for the Feature completion pass (ADR-0006): the issue
    carries `prd` and `state:ready`, at least one story names it as Parent:,
    and every such story is closed."""

    def scan(self, *stories):
        return ralph_select.ready_features(list(stories))

    def test_ready_prd_with_all_stories_closed_is_reported(self):
        eligible = self.scan(
            story(10, state="ready", prio=None, prd=True),
            story(11, state="ready", type_="afk", prio=1, parent=10, closed=True),
            story(12, state="ready", type_="hil", prio=1, parent=10, closed=True, bench=True),
        )
        self.assertEqual(eligible, [10])

    def test_prd_lacking_state_ready_is_never_reported(self):
        # Story breakdown not finished: even with every story closed, a PRD
        # without state:ready must not enter the completion pass.
        eligible = self.scan(
            story(10, state="in-progress", prio=None, prd=True),
            story(11, state="ready", type_="afk", prio=1, parent=10, closed=True),
        )
        self.assertEqual(eligible, [])

    def test_prd_without_any_state_label_is_never_reported(self):
        eligible = self.scan(
            story(10, prio=None, prd=True),
            story(11, state="ready", type_="afk", prio=1, parent=10, closed=True),
        )
        self.assertEqual(eligible, [])

    def test_prd_with_zero_stories_is_never_reported(self):
        eligible = self.scan(story(10, state="ready", prio=None, prd=True))
        self.assertEqual(eligible, [])

    def test_prd_with_an_open_story_is_never_reported(self):
        eligible = self.scan(
            story(10, state="ready", prio=None, prd=True),
            story(11, state="ready", type_="afk", prio=1, parent=10, closed=True),
            story(12, state="ready", type_="afk", prio=1, parent=10),
        )
        self.assertEqual(eligible, [])

    def test_closed_prd_is_never_reported(self):
        # A closed PRD means the Feature already merged into base
        # (CONTEXT.md): the completion pass must not pick it up again.
        eligible = self.scan(
            story(10, state="ready", prio=None, prd=True, closed=True),
            story(11, state="ready", type_="afk", prio=1, parent=10, closed=True),
        )
        self.assertEqual(eligible, [])

    def test_multiple_eligible_prds_are_reported_ascending(self):
        eligible = self.scan(
            story(20, state="ready", prio=None, prd=True),
            story(21, state="ready", type_="afk", prio=1, parent=20, closed=True),
            story(10, state="ready", prio=None, prd=True),
            story(11, state="ready", type_="afk", prio=1, parent=10, closed=True),
        )
        self.assertEqual(eligible, [10, 20])


class CliReadyFeatures(unittest.TestCase):
    def _run(self, path):
        return subprocess.run(
            [RALPH, "--ready-features", path],
            cwd=REPO_ROOT,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )

    def test_prints_eligible_prd_numbers_one_per_line(self):
        proc = self._run(os.path.join(BACKLOGS, "features.json"))
        self.assertEqual(proc.returncode, 0, proc.stdout.decode())
        self.assertEqual(proc.stdout.decode().split(), ["10"])

    def test_prints_nothing_for_a_backlog_with_no_eligible_feature(self):
        proc = self._run(os.path.join(BACKLOGS, "empty.json"))
        self.assertEqual(proc.returncode, 0, proc.stdout.decode())
        self.assertEqual(proc.stdout.decode().strip(), "")


class NoWorkAndHalt(unittest.TestCase):
    def test_empty_backlog_is_no_work(self):
        self.assertEqual(action_for().kind, ralph_select.NO_WORK)

    def test_all_blocked_backlog_is_no_work(self):
        act = action_for(
            story(1, state="blocked", type_="afk", prio=1),
            story(2, state="blocked", type_="hil", prio=2, bench=True),
        )
        self.assertEqual(act.kind, ralph_select.NO_WORK)

    def test_closed_stories_are_ignored(self):
        act = action_for(story(1, state="ready", type_="afk", prio=1, closed=True))
        self.assertEqual(action_for(story(1, state="ready", type_="afk", prio=1, closed=True)).kind,
                         ralph_select.NO_WORK)
        self.assertEqual(act.kind, ralph_select.NO_WORK)

    def test_needs_human_label_halts_the_loop(self):
        act = action_for(
            story(1, state="ready", type_="afk", prio=1),
            story(2, state="blocked", type_="afk", prio=2, needs_human=True),
        )
        self.assertEqual(act.kind, ralph_select.HALT)


class PureOverNormalizedList(unittest.TestCase):
    def test_select_next_operates_on_a_normalized_list(self):
        raw = [
            story(1, state="ready", type_="afk", prio=2),
            story(2, state="in-progress", type_="afk", prio=5),
        ]
        normalized = ralph_select.normalize(raw)
        # normalize is pure: fields the engine needs, no gh objects left.
        self.assertEqual(normalized[1]["state"], "in-progress")
        self.assertFalse(normalized[1]["closed"])
        act = ralph_select.select_next(normalized)
        self.assertEqual((act.kind, act.number), (ralph_select.RESUME, 2))

    def test_normalize_carries_parent_and_is_prd(self):
        raw = [
            story(10, state="ready", prio=None, prd=True),
            story(11, state="ready", type_="afk", prio=1, parent=10),
            story(3, state="ready", type_="afk", prio=1),
        ]
        normalized = ralph_select.normalize(raw)
        self.assertTrue(normalized[0]["is_prd"])
        self.assertEqual(normalized[1]["parent"], 10)
        self.assertFalse(normalized[1]["is_prd"])
        self.assertIsNone(normalized[2]["parent"])


class CliDryRun(unittest.TestCase):
    def _run(self, path):
        return subprocess.run(
            [RALPH, "--dry-run", path],
            cwd=REPO_ROOT,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )

    def test_dry_run_prints_start_action_for_a_ready_backlog(self):
        proc = self._run(os.path.join(BACKLOGS, "ready.json"))
        self.assertEqual(proc.returncode, 0, proc.stdout.decode())
        self.assertEqual(proc.stdout.decode().strip(), "start #10")

    def test_dry_run_prints_no_work_for_empty_backlog(self):
        proc = self._run(os.path.join(BACKLOGS, "empty.json"))
        self.assertEqual(proc.returncode, 0, proc.stdout.decode())
        self.assertEqual(proc.stdout.decode().strip(), "no-work")


if __name__ == "__main__":
    unittest.main()
