"""Unit tests for role alternation across newly started Stories (#47, PRD #42).

Ralph treats the two selected Model Profiles as a *pair* and swaps which one
implements and which one reviews every time a Story that carries no assignment
starts, so authorship and review influence stay balanced over the backlog.

The invariant that makes it safe is that alternation advances on **fresh pairs
only**: a resumed checkpointed Story, a retried failed Attempt, and a further
review round all read their roles off the Story's own labels (#46), so no Story
ever swaps models midway. The alternation phase itself is loop-local durable
state under the git dir -- next to the tick lock -- so a Story started in the
next tick continues the alternation rather than restarting it.

The seam mirrors the rest of the loop: `ralph_alternation` is pure ordering plus
a tiny state store, `assign_plan` takes the phase as an argument and stays a pure
command plan, and `ralph --assign-models` is the only place that reads the phase
and advances it -- after the plan has actually been applied.
"""
import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
LIB_DIR = os.path.join(REPO_ROOT, "lib")
FIXTURES = os.path.join(REPO_ROOT, "test", "fixtures", "config")
RALPH = os.path.join(REPO_ROOT, "bin", "ralph")

sys.path.insert(0, LIB_DIR)
import ralph_alternation  # noqa: E402
import ralph_config  # noqa: E402
import ralph_models  # noqa: E402

IMPL_A = "model:impl:claude-opus-5"
REVIEW_B = "model:review:gpt-5-codex"
# The same pair, swapped: the second newly started Story runs Codex as the
# Implementation Agent and Claude as the Review Agent.
IMPL_B = "model:impl:gpt-5-codex"
REVIEW_A = "model:review:claude-opus-5"


def valid(name):
    return os.path.join(FIXTURES, "valid", name)


def config_of(name):
    result = ralph_config.load_and_validate(valid(name))
    assert result.ok, result.errors
    return result.config


def story(number=47, labels=("state:in-progress", "type:afk", "prio:1")):
    return {
        "number": number,
        "title": "Alternate the two model roles",
        "labels": [{"name": n} for n in labels],
        "body": "## Acceptance Criteria\n- [ ] alternates the pair\n\n"
                "Parent: None\nDepends on: None\n",
        "state": "OPEN",
    }


def assigned_story(number=47, impl="claude-opus-5", review="gpt-5-codex"):
    return story(number=number,
                 labels=("state:in-progress", "type:afk", "prio:1",
                         "model:impl:" + impl, "model:review:" + review))


def _flat(commands):
    return " | ".join(" ".join(c) for c in commands)


def _added_labels(plan):
    """The assignment labels the plan applies, as {role prefix: identity}."""
    edit = [c for c in plan.commands if c[:3] == ["gh", "issue", "edit"]]
    if not edit:
        return {}
    out = {}
    for label in [a for a in edit[0] if a.startswith("model:")]:
        prefix, identity = label.rsplit(":", 1)
        out[prefix + ":"] = identity
    return out


class ThePairSwapsOnEveryNewlyStartedStory(unittest.TestCase):
    """AC: the first newly assigned Story uses the resolved role order; the next
    newly assigned Story swaps the pair."""

    def test_the_first_newly_assigned_story_uses_the_resolved_order(self):
        plan = ralph_models.assign_plan(story(), config_of("models.yml"), phase=0)
        self.assertTrue(plan.ok, plan.errors)
        self.assertEqual(plan.implementation.model, "claude-opus-5")
        self.assertEqual(plan.review.model, "gpt-5-codex")
        self.assertFalse(plan.swapped)

    def test_the_next_newly_assigned_story_swaps_the_pair(self):
        plan = ralph_models.assign_plan(story(), config_of("models.yml"), phase=1)
        self.assertTrue(plan.ok, plan.errors)
        self.assertEqual(plan.implementation.model, "gpt-5-codex")
        self.assertEqual(plan.review.model, "claude-opus-5")
        self.assertTrue(plan.swapped)

    def test_the_swap_is_what_gets_persisted_on_the_story(self):
        plan = ralph_models.assign_plan(story(number=47), config_of("models.yml"),
                                        phase=1)
        self.assertEqual(_added_labels(plan),
                         {"model:impl:": "gpt-5-codex",
                          "model:review:": "claude-opus-5"})

    def test_the_swapped_labels_are_created_on_demand_too(self):
        plan = ralph_models.assign_plan(story(), config_of("models.yml"), phase=1)
        created = [c[3] for c in plan.commands if c[:3] == ["gh", "label", "create"]]
        self.assertEqual(created, [IMPL_B, REVIEW_A])

    def test_the_pair_alternates_over_a_run_of_newly_started_stories(self):
        config = config_of("models.yml")
        seen = [ralph_models.assign_plan(story(number=n), config, phase=phase)
                .implementation.model
                for phase, n in enumerate(range(50, 54))]
        self.assertEqual(seen, ["claude-opus-5", "gpt-5-codex",
                                "claude-opus-5", "gpt-5-codex"])

    def test_a_role_override_defines_the_order_the_alternation_starts_from(self):
        # The operator's CLI role order is the *first* Story's order; the next
        # newly assigned Story swaps that pair, not the committed default pair.
        config = config_of("models-reassigned.yml")
        first = ralph_models.assign_plan(story(), config, phase=0,
                                         implementation="codex-impl",
                                         review="claude-review")
        second = ralph_models.assign_plan(story(), config, phase=1,
                                          implementation="codex-impl",
                                          review="claude-review")
        self.assertEqual(first.implementation.model, "gpt-5-codex-mini")
        self.assertEqual(second.implementation.model, "claude-sonnet-5")
        self.assertEqual(second.review.model, "gpt-5-codex-mini")

    def test_swapping_never_collapses_the_independence_check(self):
        # A same-identity pair is refused whichever way round it is ordered.
        plan = ralph_models.assign_plan(story(),
                                        config_of("models-same-identity.yml"),
                                        phase=1)
        self.assertFalse(plan.ok)
        self.assertIn("--allow-same-model", " ".join(plan.errors))


class AlternationAdvancesOnlyOnAFreshPair(unittest.TestCase):
    """AC: resuming a checkpointed Story, retrying a failed Attempt, and running
    a further review round all keep the Story's persisted roles."""

    def test_a_newly_assigned_story_advances_the_alternation(self):
        plan = ralph_models.assign_plan(story(), config_of("models.yml"), phase=0)
        self.assertTrue(plan.advances_alternation)

    def test_an_assigned_story_keeps_its_roles_whatever_the_phase(self):
        # Resume / retry / further review round: the Story is already assigned,
        # so the phase never reaches it.
        for phase in (0, 1, 2, 3):
            plan = ralph_models.assign_plan(assigned_story(), config_of("models.yml"),
                                            phase=phase)
            self.assertTrue(plan.ok, plan.errors)
            self.assertEqual(plan.commands, [])
            self.assertEqual(plan.implementation.model, "claude-opus-5")
            self.assertEqual(plan.review.model, "gpt-5-codex")
            self.assertFalse(plan.swapped)

    def test_an_assigned_story_does_not_advance_the_alternation(self):
        # The swap that the *next* newly started Story gets must not be consumed
        # by a resume, or two Stories in a row would run the same order.
        plan = ralph_models.assign_plan(assigned_story(), config_of("models.yml"),
                                        phase=0)
        self.assertFalse(plan.advances_alternation)

    def test_a_story_assigned_in_swapped_order_is_read_back_swapped(self):
        swapped = assigned_story(impl="gpt-5-codex", review="claude-opus-5")
        plan = ralph_models.assign_plan(swapped, config_of("models.yml"), phase=0)
        self.assertEqual(plan.commands, [])
        self.assertEqual(plan.implementation.model, "gpt-5-codex")
        self.assertEqual(plan.review.model, "claude-opus-5")

    def test_a_half_assigned_story_heals_forward_without_swapping(self):
        # A crash between the two label writes leaves one role recorded. That
        # Story already started, so it is not a fresh pair: the recorded role
        # stands and only the missing one is resolved -- no swap, no advance.
        half = story(labels=("state:in-progress", "type:afk", IMPL_A))
        plan = ralph_models.assign_plan(half, config_of("models.yml"), phase=1)
        self.assertTrue(plan.ok, plan.errors)
        self.assertEqual(plan.newly_assigned, ["review"])
        self.assertEqual(plan.implementation.model, "claude-opus-5")
        self.assertEqual(_added_labels(plan), {"model:review:": "gpt-5-codex"})
        self.assertFalse(plan.swapped)
        self.assertFalse(plan.advances_alternation)

    def test_a_repository_without_a_catalog_never_advances(self):
        plan = ralph_models.assign_plan(story(), config_of("minimal.yml"), phase=1)
        self.assertTrue(plan.ok, plan.errors)
        self.assertEqual(plan.commands, [])
        self.assertFalse(plan.advances_alternation)


class TheFixedRoleOption(unittest.TestCase):
    """AC: the fixed-role option disables swapping for every newly assigned
    Story."""

    def test_the_committed_option_keeps_the_resolved_order_at_every_phase(self):
        config = config_of("models-fixed-roles.yml")
        for phase in (0, 1, 2, 3):
            plan = ralph_models.assign_plan(story(), config, phase=phase)
            self.assertTrue(plan.ok, plan.errors)
            self.assertEqual(plan.implementation.model, "claude-opus-5")
            self.assertEqual(plan.review.model, "gpt-5-codex")
            self.assertFalse(plan.swapped)

    def test_fixed_roles_never_advance_the_alternation(self):
        plan = ralph_models.assign_plan(story(), config_of("models-fixed-roles.yml"),
                                        phase=0)
        self.assertFalse(plan.advances_alternation)

    def test_the_operator_flag_fixes_the_roles_for_this_invocation(self):
        plan = ralph_models.assign_plan(story(), config_of("models.yml"), phase=1,
                                        fixed_roles=True)
        self.assertEqual(plan.implementation.model, "claude-opus-5")
        self.assertFalse(plan.swapped)

    def test_alternation_is_on_unless_the_option_turns_it_off(self):
        self.assertTrue(ralph_alternation.enabled(config_of("models.yml")))
        self.assertFalse(ralph_alternation.enabled(config_of("models-fixed-roles.yml")))
        self.assertTrue(ralph_alternation.enabled(config_of("minimal.yml")))

    def test_a_non_boolean_alternate_is_rejected_by_the_validator(self):
        result = ralph_config.load_and_validate(
            os.path.join(FIXTURES, "invalid", "bad-alternate.yml"))
        self.assertFalse(result.ok)
        self.assertTrue(any("alternate" in e for e in result.errors), result.errors)


class TheAlternationPhaseIsPureOrdering(unittest.TestCase):
    """The ordering itself has no state: a phase in, an order out."""

    def test_an_even_phase_keeps_the_order(self):
        self.assertEqual(ralph_alternation.order_for(0, "impl", "rev"),
                         ("impl", "rev"))

    def test_an_odd_phase_swaps_the_pair(self):
        self.assertEqual(ralph_alternation.order_for(1, "impl", "rev"),
                         ("rev", "impl"))

    def test_the_phase_advances_by_one(self):
        self.assertEqual(ralph_alternation.advanced(0), 1)
        self.assertEqual(ralph_alternation.advanced(1), 2)

    def test_swaps_reports_the_phase_parity(self):
        self.assertEqual([ralph_alternation.swaps(p) for p in range(4)],
                         [False, True, False, True])


class TheAlternationStateStore(unittest.TestCase):
    """AC: alternation state survives across ticks.

    It lives under the git dir -- next to the tick lock, never in the working
    tree and never committed -- and a missing or damaged file starts the
    alternation over rather than failing a tick.
    """

    def repo(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(tmp, ignore_errors=True))
        os.makedirs(os.path.join(tmp, ".git"))
        return tmp

    def test_the_state_lives_under_the_git_dir(self):
        tmp = self.repo()
        path = ralph_alternation.state_path(tmp)
        self.assertTrue(path.startswith(os.path.join(tmp, ".git") + os.sep), path)

    def test_a_written_phase_is_read_back(self):
        path = ralph_alternation.state_path(self.repo())
        self.assertTrue(ralph_alternation.write_phase(path, 3))
        self.assertEqual(ralph_alternation.read_phase(path), 3)

    def test_a_missing_state_file_starts_at_phase_zero(self):
        self.assertEqual(
            ralph_alternation.read_phase(ralph_alternation.state_path(self.repo())), 0)

    def test_a_damaged_state_file_starts_over_instead_of_failing(self):
        path = ralph_alternation.state_path(self.repo())
        os.makedirs(os.path.dirname(path))
        with open(path, "w") as fh:
            fh.write("{not json")
        self.assertEqual(ralph_alternation.read_phase(path), 0)

    def test_a_git_dir_file_gitlink_is_followed(self):
        # A submodule / worktree checkout points at its real git dir with a
        # `gitdir:` file; the state belongs there, not in a directory that does
        # not exist.
        tmp = self.repo()
        real = os.path.join(tmp, "real-git-dir")
        os.makedirs(real)
        __import__("shutil").rmtree(os.path.join(tmp, ".git"))
        with open(os.path.join(tmp, ".git"), "w") as fh:
            fh.write("gitdir: %s\n" % real)
        self.assertTrue(ralph_alternation.state_path(tmp).startswith(real + os.sep))

    def test_a_checkout_without_a_git_dir_has_nowhere_durable_to_record(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(tmp, ignore_errors=True))
        self.assertIsNone(ralph_alternation.state_path(tmp))
        self.assertEqual(ralph_alternation.read_phase(None), 0)
        self.assertFalse(ralph_alternation.write_phase(None, 1))


class CliAlternatesAcrossTicks(unittest.TestCase):
    """AC: alternation state survives across ticks.

    Every `ralph --assign-models` run below is a separate process against the
    same throwaway target repository, which is exactly what a later tick is.
    """

    def repo(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(tmp, ignore_errors=True))
        os.makedirs(os.path.join(tmp, ".git"))
        mockbin = os.path.join(tmp, "mockbin")
        os.makedirs(mockbin)
        gh = os.path.join(mockbin, "gh")
        with open(gh, "w") as fh:
            fh.write('#!/usr/bin/env bash\necho "gh $*" >> "$RALPH_LOG"\n')
        os.chmod(gh, os.stat(gh).st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
        return tmp

    def assign(self, tmp, story_obj, config="models.yml", extra=()):
        env = dict(os.environ, RALPH_LOG=os.path.join(tmp, "calls.log"),
                   PATH=os.path.join(tmp, "mockbin") + os.pathsep + os.environ["PATH"])
        proc = subprocess.run(
            [RALPH, "--assign-models", "-", valid(config)] + list(extra),
            cwd=tmp, input=json.dumps(story_obj), env=env,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return proc

    def applied(self, tmp):
        """The `--add-label model:impl:<id>` identity of each assignment made."""
        out = []
        with open(os.path.join(tmp, "calls.log")) as fh:
            for line in fh:
                if "issue edit" not in line:
                    continue
                for token in line.split():
                    if token.startswith("model:impl:"):
                        out.append(token[len("model:impl:"):])
        return out

    def test_consecutive_ticks_alternate_the_implementation_role(self):
        tmp = self.repo()
        for number in (61, 62, 63, 64):
            self.assign(tmp, story(number=number))
        self.assertEqual(self.applied(tmp),
                         ["claude-opus-5", "gpt-5-codex",
                          "claude-opus-5", "gpt-5-codex"])

    def test_the_phase_is_recorded_under_the_git_dir(self):
        tmp = self.repo()
        self.assign(tmp, story(number=61))
        self.assertEqual(
            ralph_alternation.read_phase(ralph_alternation.state_path(tmp)), 1)

    def test_a_resumed_story_between_two_new_ones_does_not_consume_a_swap(self):
        tmp = self.repo()
        self.assign(tmp, story(number=61))             # base order
        self.assign(tmp, assigned_story(number=61))    # resume: no-op
        self.assign(tmp, story(number=62))             # swapped
        self.assertEqual(self.applied(tmp), ["claude-opus-5", "gpt-5-codex"])

    def test_a_failed_assignment_does_not_advance_the_phase(self):
        # The phase advances only once the plan has actually been applied, so a
        # gh outage cannot silently burn a swap.
        tmp = self.repo()
        gh = os.path.join(tmp, "mockbin", "gh")
        with open(gh, "w") as fh:
            fh.write('#!/usr/bin/env bash\necho "gh $*" >> "$RALPH_LOG"\nexit 1\n')
        os.chmod(gh, os.stat(gh).st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
        env = dict(os.environ, RALPH_LOG=os.path.join(tmp, "calls.log"),
                   PATH=os.path.join(tmp, "mockbin") + os.pathsep + os.environ["PATH"])
        proc = subprocess.run([RALPH, "--assign-models", "-", valid("models.yml")],
                              cwd=tmp, input=json.dumps(story(number=61)), env=env,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.assertEqual(proc.returncode, 1)
        self.assertEqual(
            ralph_alternation.read_phase(ralph_alternation.state_path(tmp)), 0)

    def test_the_committed_fixed_role_option_never_alternates(self):
        tmp = self.repo()
        for number in (61, 62, 63):
            self.assign(tmp, story(number=number), config="models-fixed-roles.yml")
        self.assertEqual(self.applied(tmp),
                         ["claude-opus-5", "claude-opus-5", "claude-opus-5"])

    def test_the_operator_flag_fixes_the_roles_without_losing_the_phase(self):
        tmp = self.repo()
        self.assign(tmp, story(number=61))                            # base
        self.assign(tmp, story(number=62), extra=["--fixed-roles"])   # base, held
        self.assign(tmp, story(number=63))                            # swapped
        self.assertEqual(self.applied(tmp),
                         ["claude-opus-5", "claude-opus-5", "gpt-5-codex"])

    def test_the_run_reports_which_order_it_assigned(self):
        tmp = self.repo()
        first = self.assign(tmp, story(number=61))
        second = self.assign(tmp, story(number=62))
        self.assertIn("role order: resolved", first.stdout)
        self.assertIn("role order: swapped", second.stdout)

    def test_resolve_models_rejects_the_assignment_only_flag(self):
        proc = subprocess.run([RALPH, "--resolve-models", valid("models.yml"),
                               "--fixed-roles"],
                              cwd=REPO_ROOT, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, text=True)
        self.assertEqual(proc.returncode, 2)
        self.assertIn("--fixed-roles", proc.stderr)


if __name__ == "__main__":
    unittest.main()
