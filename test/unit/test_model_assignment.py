"""Unit tests for durable model assignment on the Story (#46, PRD #42).

A newly unassigned Story records the exact implementation and review model
identities as Story labels, so retries, resumes, and audits read the assignment
from the backlog instead of from whatever configuration happens to be current.
An already-assigned Story keeps its assignment: later default changes and CLI
overrides never rewrite it.

The seam mirrors the other completion stages: `assign_plan` is a pure command
plan (create the labels on demand, then apply them) that runs nothing, and
`ralph --assign-models` executes it against gh. Behavior is covered here and
against a mocked `gh` on PATH.
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
import ralph_agent  # noqa: E402
import ralph_config  # noqa: E402
import ralph_models  # noqa: E402
import ralph_story  # noqa: E402

IMPL_LABEL = "model:impl:claude-opus-5"
REVIEW_LABEL = "model:review:gpt-5-codex"


def valid(name):
    return os.path.join(FIXTURES, "valid", name)


def config_of(name):
    result = ralph_config.load_and_validate(valid(name))
    assert result.ok, result.errors
    return result.config


def story(number=46, labels=("state:in-progress", "type:afk", "prio:1")):
    return {
        "number": number,
        "title": "Persist the model assignments",
        "labels": [{"name": n} for n in labels],
        "body": "## Acceptance Criteria\n- [ ] records the identities\n\n"
                "Parent: None\nDepends on: None\n",
        "state": "OPEN",
    }


def assigned_story(number=46, impl=IMPL_LABEL, review=REVIEW_LABEL):
    return story(number=number,
                 labels=("state:in-progress", "type:afk", "prio:1", impl, review))


def _flat(commands):
    return " | ".join(" ".join(c) for c in commands)


def _mockbin(tmp, gh_exit=0):
    log = os.path.join(tmp, "calls.log")
    path = os.path.join(tmp, "gh")
    with open(path, "w") as fh:
        fh.write('#!/usr/bin/env bash\n'
                 'echo "gh $*" >> "$RALPH_LOG"\n'
                 'exit %d\n' % gh_exit)
    os.chmod(path, os.stat(path).st_mode | stat.S_IEXEC | stat.S_IXGRP)
    return log


class RecordsTheAssignment(unittest.TestCase):
    """AC: starting a newly unassigned Story records the exact implementation
    and review model identities as durable Story labels."""

    def test_plan_resolves_both_roles_to_exact_model_identities(self):
        plan = ralph_models.assign_plan(story(), config_of("models.yml"))
        self.assertTrue(plan.ok, plan.errors)
        self.assertEqual(plan.implementation.model, "claude-opus-5")
        self.assertEqual(plan.review.model, "gpt-5-codex")

    def test_plan_applies_one_label_per_role_carrying_the_identity(self):
        plan = ralph_models.assign_plan(story(number=46), config_of("models.yml"))
        edit = [c for c in plan.commands if c[:3] == ["gh", "issue", "edit"]]
        self.assertEqual(len(edit), 1, _flat(plan.commands))
        self.assertEqual(edit[0][3], "46")
        self.assertIn(IMPL_LABEL, edit[0])
        self.assertIn(REVIEW_LABEL, edit[0])

    def test_the_labels_carry_the_model_identity_not_the_profile_key(self):
        plan = ralph_models.assign_plan(story(), config_of("models.yml"))
        flat = _flat(plan.commands)
        self.assertNotIn("claude-impl", flat)
        self.assertNotIn("codex-review", flat)

    def test_newly_assigned_reports_both_roles(self):
        plan = ralph_models.assign_plan(story(), config_of("models.yml"))
        self.assertEqual(sorted(plan.newly_assigned), ["implementation", "review"])

    def test_a_role_override_is_what_gets_persisted(self):
        plan = ralph_models.assign_plan(story(), config_of("models-reassigned.yml"),
                                        implementation="claude-impl",
                                        review="codex-review")
        self.assertTrue(plan.ok, plan.errors)
        self.assertEqual(plan.implementation.model, "claude-opus-5")
        self.assertIn(IMPL_LABEL, _flat(plan.commands))

    def test_the_same_model_refusal_still_guards_a_new_assignment(self):
        plan = ralph_models.assign_plan(story(),
                                        config_of("models-same-identity.yml"))
        self.assertFalse(plan.ok)
        self.assertEqual(plan.commands, [])
        self.assertIn("--allow-same-model", " ".join(plan.errors))

    def test_never_references_main(self):
        plan = ralph_models.assign_plan(story(), config_of("models.yml"))
        self.assertNotIn("main", _flat(plan.commands))


class LabelsAreCreatedOnDemandAndIdempotent(unittest.TestCase):
    """AC: the labels are created on demand the first time an identity is
    assigned, and re-applying the same assignment is idempotent."""

    def test_each_assignment_label_is_created_before_it_is_applied(self):
        plan = ralph_models.assign_plan(story(), config_of("models.yml"))
        created = [c for c in plan.commands if c[:3] == ["gh", "label", "create"]]
        self.assertEqual([c[3] for c in created], [IMPL_LABEL, REVIEW_LABEL])
        edit_idx = next(i for i, c in enumerate(plan.commands)
                        if c[:3] == ["gh", "issue", "edit"])
        for i, c in enumerate(plan.commands):
            if c[:3] == ["gh", "label", "create"]:
                self.assertLess(i, edit_idx)

    def test_label_creation_is_idempotent(self):
        plan = ralph_models.assign_plan(story(), config_of("models.yml"))
        for c in plan.commands:
            if c[:3] == ["gh", "label", "create"]:
                self.assertIn("--force", c)

    def test_re_running_against_the_assigned_story_changes_nothing(self):
        config = config_of("models.yml")
        plan = ralph_models.assign_plan(assigned_story(), config)
        self.assertTrue(plan.ok, plan.errors)
        self.assertEqual(plan.commands, [])
        self.assertEqual(plan.newly_assigned, [])
        self.assertEqual(plan.implementation.model, "claude-opus-5")
        self.assertEqual(plan.review.model, "gpt-5-codex")

    def test_a_half_assigned_story_only_records_the_missing_role(self):
        # A crash between the two label writes must heal forward, not reassign.
        half = story(labels=("state:in-progress", "type:afk", IMPL_LABEL))
        plan = ralph_models.assign_plan(half, config_of("models-reassigned.yml"))
        self.assertTrue(plan.ok, plan.errors)
        self.assertEqual(plan.newly_assigned, ["review"])
        self.assertEqual(plan.implementation.model, "claude-opus-5")
        self.assertEqual(plan.review.model, "claude-sonnet-5")
        flat = _flat(plan.commands)
        self.assertNotIn(IMPL_LABEL, flat)
        self.assertIn("model:review:claude-sonnet-5", flat)


class NoCatalogIsNothingToRecord(unittest.TestCase):
    """The catalog stays optional (#44): a target repository without one keeps
    ticking on the shipped provider, and there is no exact identity to persist."""

    def test_plan_is_an_empty_no_op(self):
        plan = ralph_models.assign_plan(story(), config_of("minimal.yml"))
        self.assertTrue(plan.ok, plan.errors)
        self.assertEqual(plan.commands, [])
        self.assertIsNone(plan.implementation)

    def test_cli_exits_zero_without_touching_gh(self):
        with tempfile.TemporaryDirectory() as tmp:
            log = _mockbin(tmp)
            proc = subprocess.run(
                [RALPH, "--assign-models", "-", valid("minimal.yml")],
                cwd=REPO_ROOT, input=json.dumps(story()),
                env=dict(os.environ, PATH=tmp + os.pathsep + os.environ["PATH"],
                         RALPH_LOG=log),
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertFalse(os.path.exists(log))


class AssignedRolesAreReadFromTheStory(unittest.TestCase):
    """AC: resuming or retrying an assigned Story reads its roles from the
    Story labels, not from config."""

    def test_roles_come_from_the_labels_not_the_committed_defaults(self):
        res = ralph_models.roles_for_story(config_of("models-reassigned.yml"),
                                           assigned_story())
        self.assertTrue(res.ok, res.errors)
        self.assertEqual(res.implementation.model, "claude-opus-5")
        self.assertEqual(res.review.model, "gpt-5-codex")

    def test_the_provider_adapter_comes_from_the_catalog_entry(self):
        res = ralph_models.roles_for_story(config_of("models-reassigned.yml"),
                                           assigned_story())
        self.assertEqual(res.implementation.provider, "claude")
        self.assertEqual(res.review.provider, "codex")

    def test_an_unassigned_story_falls_back_to_the_committed_defaults(self):
        res = ralph_models.roles_for_story(config_of("models.yml"), story())
        self.assertTrue(res.ok, res.errors)
        self.assertEqual(res.implementation.model, "claude-opus-5")
        self.assertEqual(res.review.model, "gpt-5-codex")

    def test_an_assigned_identity_outside_the_catalog_refuses(self):
        # The allowlist still governs what may be launched.
        rogue = assigned_story(impl="model:impl:some-unlisted-model")
        res = ralph_models.roles_for_story(config_of("models.yml"), rogue)
        self.assertFalse(res.ok)
        self.assertIn("some-unlisted-model", " ".join(res.errors))

    def test_the_agent_adapter_launches_the_assigned_model(self):
        adapter, errors = ralph_agent.adapter_for_role(
            config_of("models-reassigned.yml"), "implementation",
            story=assigned_story())
        self.assertEqual(errors, [])
        self.assertEqual(adapter.provider, "claude")
        self.assertEqual(adapter.model, "claude-opus-5")

    def test_the_review_adapter_launches_the_assigned_review_model(self):
        adapter, errors = ralph_agent.adapter_for_role(
            config_of("models-reassigned.yml"), "review",
            story=assigned_story())
        self.assertEqual(errors, [])
        self.assertEqual(adapter.provider, "codex")
        self.assertEqual(adapter.model, "gpt-5-codex")


class AnAssignedStoryIsNeverRewritten(unittest.TestCase):
    """AC: changed config defaults and CLI overrides leave an already-assigned
    Story's roles untouched."""

    def test_changed_committed_defaults_do_not_rewrite_the_assignment(self):
        plan = ralph_models.assign_plan(assigned_story(),
                                        config_of("models-reassigned.yml"))
        self.assertTrue(plan.ok, plan.errors)
        self.assertEqual(plan.commands, [])
        self.assertEqual(plan.implementation.model, "claude-opus-5")
        self.assertEqual(plan.review.model, "gpt-5-codex")

    def test_cli_overrides_do_not_rewrite_the_assignment(self):
        plan = ralph_models.assign_plan(assigned_story(),
                                        config_of("models-reassigned.yml"),
                                        implementation="codex-impl",
                                        review="claude-review")
        self.assertTrue(plan.ok, plan.errors)
        self.assertEqual(plan.commands, [])
        self.assertEqual(plan.implementation.model, "claude-opus-5")
        self.assertEqual(plan.review.model, "gpt-5-codex")

    def test_overrides_do_not_reach_the_agent_of_an_assigned_story(self):
        adapter, errors = ralph_agent.adapter_for_role(
            config_of("models-reassigned.yml"), "implementation",
            implementation="codex-impl", story=assigned_story())
        self.assertEqual(errors, [])
        self.assertEqual(adapter.model, "claude-opus-5")

    def test_a_same_model_assignment_is_honored_without_the_acknowledgement(self):
        # Independence was decided when the Story was assigned; re-litigating it
        # later would strand an in-flight Story on a config change.
        same = assigned_story(impl="model:impl:claude-opus-5",
                              review="model:review:claude-opus-5")
        res = ralph_models.roles_for_story(config_of("models-same-identity.yml"),
                                           same)
        self.assertTrue(res.ok, res.errors)
        self.assertTrue(res.same_model)


class ValidatorAcceptsTheAssignmentLabels(unittest.TestCase):
    """AC: the story validator accepts the assignment labels and still enforces
    exactly one `state:` and one `type:` label."""

    def test_an_assigned_story_is_canonical(self):
        result = ralph_story.validate_story(assigned_story())
        self.assertTrue(result.ok, result.errors)

    def test_the_assignment_is_surfaced_as_normalized_fields(self):
        fields = ralph_story.validate_story(assigned_story()).fields
        self.assertEqual(fields["models"], {"implementation": "claude-opus-5",
                                            "review": "gpt-5-codex"})

    def test_an_unassigned_story_reports_no_assignment(self):
        fields = ralph_story.validate_story(story()).fields
        self.assertEqual(fields["models"], {"implementation": None, "review": None})

    def test_two_labels_for_one_role_is_an_ambiguous_assignment(self):
        ambiguous = story(labels=("state:in-progress", "type:afk", IMPL_LABEL,
                                  "model:impl:gpt-5-codex"))
        result = ralph_story.validate_story(ambiguous)
        self.assertFalse(result.ok)
        self.assertTrue(any("model:impl:" in e for e in result.errors), result.errors)

    def test_assignment_labels_do_not_excuse_a_missing_type_label(self):
        result = ralph_story.validate_story(
            story(labels=("state:in-progress", IMPL_LABEL, REVIEW_LABEL)))
        self.assertFalse(result.ok)
        self.assertTrue(any("type:*" in e for e in result.errors), result.errors)

    def test_assignment_labels_do_not_excuse_two_state_labels(self):
        result = ralph_story.validate_story(
            story(labels=("state:ready", "state:in-progress", "type:afk",
                          IMPL_LABEL, REVIEW_LABEL)))
        self.assertFalse(result.ok)
        self.assertTrue(any("state:*" in e for e in result.errors), result.errors)

    def test_lint_story_accepts_an_assigned_story(self):
        proc = subprocess.run([RALPH, "--lint-story", "-"], cwd=REPO_ROOT,
                              input=json.dumps(assigned_story()),
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)


class CliAssignModels(unittest.TestCase):
    def _run(self, story_obj, config, tmp, log, extra=()):
        env = dict(os.environ, PATH=tmp + os.pathsep + os.environ["PATH"],
                   RALPH_LOG=log)
        return subprocess.run(
            [RALPH, "--assign-models", "-", config] + list(extra),
            cwd=REPO_ROOT, input=json.dumps(story_obj), env=env,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )

    def test_assigns_via_mocked_gh_and_reports_both_identities(self):
        with tempfile.TemporaryDirectory() as tmp:
            log = _mockbin(tmp)
            proc = self._run(story(number=46), valid("models.yml"), tmp, log)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            with open(log) as fh:
                calls = fh.read()
            self.assertIn("label create " + IMPL_LABEL, calls)
            self.assertIn("label create " + REVIEW_LABEL, calls)
            self.assertIn("issue edit 46", calls)
            self.assertIn("claude-opus-5", proc.stdout)
            self.assertIn("gpt-5-codex", proc.stdout)

    def test_an_assigned_story_issues_no_gh_calls(self):
        with tempfile.TemporaryDirectory() as tmp:
            log = _mockbin(tmp)
            proc = self._run(assigned_story(), valid("models-reassigned.yml"),
                             tmp, log)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertFalse(os.path.exists(log))
            self.assertIn("claude-opus-5", proc.stdout)

    def test_same_model_refusal_exits_two(self):
        with tempfile.TemporaryDirectory() as tmp:
            log = _mockbin(tmp)
            proc = self._run(story(), valid("models-same-identity.yml"), tmp, log)
            self.assertEqual(proc.returncode, 2)
            self.assertIn("--allow-same-model", proc.stderr)

    def test_same_model_acknowledgement_proceeds(self):
        with tempfile.TemporaryDirectory() as tmp:
            log = _mockbin(tmp)
            proc = self._run(story(), valid("models-same-identity.yml"), tmp, log,
                             extra=["--allow-same-model"])
            self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_a_gh_failure_exits_one(self):
        with tempfile.TemporaryDirectory() as tmp:
            log = _mockbin(tmp, gh_exit=1)
            proc = self._run(story(), valid("models.yml"), tmp, log)
            self.assertEqual(proc.returncode, 1)

    def test_an_invalid_config_exits_two(self):
        with tempfile.TemporaryDirectory() as tmp:
            log = _mockbin(tmp)
            proc = self._run(story(),
                             os.path.join(FIXTURES, "invalid", "missing-gating.yml"),
                             tmp, log)
            self.assertEqual(proc.returncode, 2)


if __name__ == "__main__":
    unittest.main()
