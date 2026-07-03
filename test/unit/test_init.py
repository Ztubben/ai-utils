"""Unit tests for superproject bootstrap (`ralph --init`).

`ralph --init` idempotently creates the canonical GitHub label vocabulary
(state:/type:/prio:/needs-human/ready-for-human) that ralph_story/ralph_select
consume, and ensures the configured base branch exists. The deterministic seam is
a pure command *plan* (the ordered git/gh commands); a thin runner executes it,
and the CLI (`ralph --init`) detects live repo state, prints, and sets exit codes.
Behavior is covered against mocked git/gh on PATH.
"""
import os
import stat
import subprocess
import sys
import tempfile
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
LIB_DIR = os.path.join(REPO_ROOT, "lib")
FIXTURES = os.path.join(REPO_ROOT, "test", "fixtures")
RALPH = os.path.join(REPO_ROOT, "bin", "ralph")

sys.path.insert(0, LIB_DIR)
import ralph_init  # noqa: E402
import ralph_story  # noqa: E402
import ralph_select  # noqa: E402


def _flat(commands):
    return [tok for cmd in commands for tok in cmd]


class InitPlanLabels(unittest.TestCase):
    def test_creates_every_label_the_engine_consumes(self):
        # The canonical vocabulary must cover exactly what the story validator and
        # selection engine key off, or issues cannot be authored/transitioned.
        plan = ralph_init.init_plan(base="develop", base_exists=True)
        created = {cmd[3] for cmd in plan if cmd[:3] == ["gh", "label", "create"]}
        for state in ralph_story.STATES:
            self.assertIn("state:" + state, created)
        for type_ in ralph_story.TYPES:
            self.assertIn("type:" + type_, created)
        self.assertIn(ralph_story.BLOCKER_LABEL, created)          # ready-for-human
        self.assertIn(ralph_select.NEEDS_HUMAN_LABEL, created)     # needs-human

    def test_seeds_a_prio_starter_range(self):
        plan = ralph_init.init_plan(base="develop", base_exists=True, prio_max=5)
        created = {cmd[3] for cmd in plan if cmd[:3] == ["gh", "label", "create"]}
        for n in range(6):
            self.assertIn("prio:%d" % n, created)
        self.assertNotIn("prio:6", created)

    def test_labels_are_idempotent_via_force(self):
        plan = ralph_init.init_plan(base="develop", base_exists=True)
        for cmd in plan:
            if cmd[:3] == ["gh", "label", "create"]:
                self.assertIn("--force", cmd)


class InitPlanBaseBranch(unittest.TestCase):
    def test_creates_base_off_default_when_missing(self):
        plan = ralph_init.init_plan(base="develop", base_exists=False,
                                    default_branch="main")
        push = next((c for c in plan if c[:2] == ["git", "push"]), None)
        self.assertIsNotNone(push, "a missing base must be created")
        self.assertIn("origin/main:refs/heads/develop", push)

    def test_does_not_create_base_when_present(self):
        plan = ralph_init.init_plan(base="develop", base_exists=True)
        self.assertFalse(any(c[:1] == ["git"] for c in plan))

    def test_refuses_to_fabricate_main(self):
        # Ralph never touches main (ADR-0001); init must not conjure it either.
        plan = ralph_init.init_plan(base="main", base_exists=False)
        self.assertFalse(any(c[:1] == ["git"] for c in plan))
        self.assertNotIn("main:refs/heads/main", _flat(plan))


class InitCli(unittest.TestCase):
    def _mockbin(self, tmp, base_exists=False, default_branch="main",
                 gh_exit=0, git_exit=0):
        log = os.path.join(tmp, "calls.log")
        gh = ('#!/usr/bin/env bash\n'
              'echo "gh $*" >> "$RALPH_LOG"\n'
              'if [[ "$1 $2" == "repo view" ]]; then echo "%s"; fi\n'
              'exit %d\n' % (default_branch, gh_exit))
        git = ('#!/usr/bin/env bash\n'
               'echo "git $*" >> "$RALPH_LOG"\n'
               'if [[ "$1" == "ls-remote" ]]; then %s fi\n'
               'exit %d\n'
               % ('echo "sha\trefs/heads/develop";' if base_exists else ':;',
                  git_exit))
        for name, body in (("gh", gh), ("git", git)):
            path = os.path.join(tmp, name)
            with open(path, "w") as fh:
                fh.write(body)
            os.chmod(path, os.stat(path).st_mode | stat.S_IEXEC | stat.S_IXGRP)
        return log

    def _run(self, tmp, log, config):
        env = dict(os.environ, PATH=tmp + os.pathsep + os.environ["PATH"],
                   RALPH_LOG=log)
        return subprocess.run([RALPH, "--init", config], cwd=tmp, env=env,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

    def test_creates_labels_and_missing_base(self):
        with tempfile.TemporaryDirectory() as tmp:
            log = self._mockbin(tmp, base_exists=False, default_branch="main")
            config = os.path.join(FIXTURES, "config", "valid", "full.yml")  # base: develop
            proc = self._run(tmp, log, config)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            with open(log) as fh:
                calls = fh.read()
            self.assertIn("gh label create state:in-progress", calls)
            self.assertIn("gh label create type:hil", calls)
            self.assertIn("gh label create needs-human", calls)
            self.assertIn("origin/main:refs/heads/develop", calls)  # base created

    def test_idempotent_when_base_present(self):
        with tempfile.TemporaryDirectory() as tmp:
            log = self._mockbin(tmp, base_exists=True)
            config = os.path.join(FIXTURES, "config", "valid", "full.yml")
            proc = self._run(tmp, log, config)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            with open(log) as fh:
                calls = fh.read()
            self.assertIn("gh label create state:ready", calls)
            self.assertNotIn("git push", calls)  # base already there

    def test_bad_config_exits_two(self):
        with tempfile.TemporaryDirectory() as tmp:
            log = self._mockbin(tmp)
            config = os.path.join(FIXTURES, "config", "invalid", "missing-gating.yml")
            proc = self._run(tmp, log, config)
            self.assertEqual(proc.returncode, 2, proc.stdout)

    def test_gh_label_failure_exits_one(self):
        with tempfile.TemporaryDirectory() as tmp:
            log = self._mockbin(tmp, gh_exit=1)
            config = os.path.join(FIXTURES, "config", "valid", "full.yml")
            proc = self._run(tmp, log, config)
            self.assertEqual(proc.returncode, 1)


if __name__ == "__main__":
    unittest.main()
