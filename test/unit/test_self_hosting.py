"""ai-utils' own target-repository deployment (#64, PRD #42).

When ai-utils is the checkout root it is its own target repository (ADR-0001
amendment), so it commits the same things any other target repository does: a
model-profile catalog, a quality gate, CI, protected control-plane paths, and a
base-branch policy. These are an *instance* of the public contracts, never
defaults a repository that mounts ai-utils as a submodule inherits.

These tests pin the parts a later Story could silently drift: that the committed
config still validates against the shipped schema, that the CI the review gate
reads is itself protected, and that the CI check is named what the gating step
is named -- a required check whose context nothing publishes would block every
merge into the base branch.
"""
import os
import sys
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(REPO_ROOT, "lib"))

import yaml  # noqa: E402

import ralph_config  # noqa: E402

CONFIG = os.path.join(REPO_ROOT, ".ralph.yml")
WORKFLOWS = os.path.join(REPO_ROOT, ".github", "workflows")


def committed_config():
    result = ralph_config.load_and_validate(CONFIG)
    assert result.ok, result.errors
    return result.config


def workflows():
    """Every committed workflow, as {filename: parsed document}."""
    found = {}
    for name in sorted(os.listdir(WORKFLOWS)):
        if name.endswith((".yml", ".yaml")):
            with open(os.path.join(WORKFLOWS, name)) as fh:
                found[name] = yaml.safe_load(fh)
    return found


class TheCommittedConfigIsAnInstanceOfTheContract(unittest.TestCase):
    """AC: ai-utils' own config is a valid instance of the shipped contract."""

    def test_the_repositorys_own_config_validates(self):
        result = ralph_config.load_and_validate(CONFIG)
        self.assertTrue(result.ok, result.errors)

    def test_both_roles_resolve_to_distinct_committed_models(self):
        import ralph_models
        resolved = ralph_models.resolve_roles(committed_config())
        self.assertTrue(resolved.ok, resolved.errors)
        self.assertNotEqual(resolved.implementation.model, resolved.review.model,
                            "self-hosting must not lose reviewer independence")


class TheCiTheGateReadsIsProtected(unittest.TestCase):
    """AC: .github/workflows/** is a protected control-plane path."""

    def test_the_workflow_directory_is_in_the_protected_control_plane(self):
        protected = (committed_config().get("control_plane") or {}).get("protected") or []
        self.assertIn(".github/workflows/**", protected,
                      "an unattended Story could otherwise weaken the CI the "
                      "review gate reads")

    def test_the_review_machinery_itself_stays_protected(self):
        protected = (committed_config().get("control_plane") or {}).get("protected") or []
        for pattern in ("prompts/**", "schema/**", "lib/ralph_review*.py",
                        ".ralph.yml"):
            self.assertIn(pattern, protected)


class CiPublishesTheCheckTheGateRequires(unittest.TestCase):
    """AC: a workflow runs the gating suite on pull requests to the base
    branch, under the gating step's own name."""

    def setUp(self):
        self.config = committed_config()
        self.workflows = workflows()
        self.assertTrue(self.workflows, "ai-utils self-hosts; it carries its own CI")

    def documents(self):
        return list(self.workflows.values())

    def test_a_workflow_runs_the_same_suite_as_the_local_gate(self):
        commands = [step.get("run", "")
                    for doc in self.documents()
                    for job in (doc.get("jobs") or {}).values()
                    for step in (job.get("steps") or [])]
        self.assertTrue(any("test/run.sh" in run for run in commands),
                        "CI must re-verify each review fix commit with the same "
                        "gate the tick runs locally")

    def test_every_gating_step_name_is_published_as_a_check(self):
        # The check name a GitHub Actions job publishes is its job name (its id
        # when it declares none). Branch protection requires those names, so a
        # gating step with no matching job is a check that never arrives.
        job_names = set()
        for doc in self.documents():
            for job_id, job in (doc.get("jobs") or {}).items():
                job_names.add(job.get("name") or job_id)
        for step in self.config["gating"]:
            self.assertIn(step["name"], job_names)

    def test_ci_runs_on_pull_requests_targeting_the_base_branch(self):
        base = self.config["branching"]["base"]
        # `on` is YAML 1.1's boolean true, which is why this reads oddly.
        triggers = [doc.get("on", doc.get(True)) or {} for doc in self.documents()]
        targeted = [t["pull_request"].get("branches") or []
                    for t in triggers if "pull_request" in t]
        self.assertTrue(any(base in branches for branches in targeted),
                        "model review and CI run concurrently on the pull "
                        "request Ralph opens against %r" % base)


if __name__ == "__main__":
    unittest.main()
