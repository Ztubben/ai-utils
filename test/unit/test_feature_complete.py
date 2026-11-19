"""Unit tests for Feature completion pass (US-028, ADR-0006).

When a Feature is complete (all stories closed, PRD open + state:ready), the
completion pass integrates the feature branch into base: autosquash-collapse
fixup! commits, rebase onto current base, run full gating, open a single PR,
rebase-merge it (never a merge commit, never squash), and close the PRD.

Failure modes:
  - Autosquash conflict: fall back to uncollapsed linear history, continue
  - Rebase conflict or red gate: push branch, comment on PRD, label
    ready-for-human, stop — NO needs-human circuit breaker
  - Base is main: integrate into it like any other base (ADR-0001)
  - An unverified HIL Story of the Feature: refuse outright, naming each one
    (PRD #69 — the physical gate now stands at the Feature boundary)
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
FIXTURES = os.path.join(REPO_ROOT, "test", "fixtures")
RALPH = os.path.join(REPO_ROOT, "bin", "ralph")

sys.path.insert(0, LIB_DIR)
import ralph_feature  # noqa: E402


def prd_issue(number=18, title="Per-Feature integration branches"):
    return {
        "number": number,
        "title": title,
        "labels": [{"name": "prd"}, {"name": "state:ready"}],
        "body": "The Feature PRD.\n\nDepends on: None\n",
        "state": "OPEN",
    }


def child(number, parent=18, type_="afk", state="OPEN",
          label="state:awaiting-bench"):
    return {
        "number": number,
        "title": "Story %d" % number,
        "labels": [{"name": "type:" + type_}, {"name": label}],
        "body": "## Acceptance Criteria\n- [ ] it works\n\nParent: #%d\n"
                "Depends on: None\n" % parent
                + ("\n## Bench Test Procedure\n- poke it\n"
                   if type_ == "hil" else ""),
        "state": state,
    }


def _flat(commands):
    return [tok for cmd in commands for tok in cmd]


class TheFeatureBoundaryHilGate(unittest.TestCase):
    """PRD #69: unverified hardware code reaches the Feature branch, not base."""

    def _plan(self, backlog):
        return ralph_feature.feature_complete_plan(
            prd_issue(), base="develop", backlog=backlog)

    def test_an_open_hil_story_refuses_the_integration(self):
        plan = self._plan([prd_issue(), child(20, type_="hil")])

        self.assertFalse(plan.ok)
        self.assertEqual(plan.steps, [])
        self.assertIn("#20", " ".join(plan.errors))

    def test_the_refusal_names_every_unverified_hil_story(self):
        plan = self._plan([prd_issue(), child(20, type_="hil"),
                           child(21, type_="afk"),
                           child(22, type_="hil")])

        message = " ".join(plan.errors)
        self.assertIn("#20", message)
        self.assertIn("#22", message)
        self.assertNotIn("#21", message)

    def test_it_proceeds_once_every_hil_story_is_closed(self):
        plan = self._plan([prd_issue(),
                           child(20, type_="hil", state="CLOSED"),
                           child(22, type_="hil", state="CLOSED")])

        self.assertTrue(plan.ok, plan.errors)
        self.assertTrue(plan.steps)

    def test_an_all_afk_feature_is_unaffected(self):
        plan = self._plan([prd_issue(), child(20), child(21, state="CLOSED")])

        self.assertTrue(plan.ok, plan.errors)

    def test_another_features_open_hil_story_is_not_this_features_problem(self):
        plan = self._plan([prd_issue(), child(30, parent=99, type_="hil")])

        self.assertTrue(plan.ok, plan.errors)

    def test_a_hil_story_still_in_review_counts_as_unverified(self):
        plan = self._plan([prd_issue(),
                           child(20, type_="hil", label="state:in-review")])

        self.assertFalse(plan.ok)

    def test_the_refusal_stops_this_feature_and_nothing_else(self):
        plan = self._plan([prd_issue(), child(20, type_="hil")])

        self.assertNotIn("needs-human", " ".join(plan.errors))
        self.assertEqual(plan.steps, [])

    def test_the_feature_still_integrates_as_one_merge_into_base(self):
        plan = self._plan([prd_issue(), child(20, state="CLOSED")])
        step = next(s for s in plan.steps if s["name"] == "pr-create")
        create, merge = step["commands"]

        self.assertEqual(create[create.index("--base") + 1], "develop")
        self.assertEqual(create[create.index("--head") + 1],
                         "feature/18-per-feature-integration-branches")
        self.assertIn("--rebase", merge)
        self.assertNotIn("--squash", merge)

    def test_the_feature_pull_request_is_not_offered_for_model_review(self):
        """Every Story it contains was reviewed on its own pull request."""
        import ralph_review

        plan = self._plan([prd_issue(), child(20, state="CLOSED")])
        step = next(s for s in plan.steps if s["name"] == "pr-create")

        self.assertNotIn(ralph_review.MANAGED_PR_MARKER,
                         " ".join(_flat(step["commands"])))

    def test_a_backlog_that_was_not_supplied_leaves_the_gate_unevaluated(self):
        # The live CLI reads the backlog and refuses when it cannot; the pure
        # plan simply reports what it was given.
        plan = ralph_feature.feature_complete_plan(prd_issue(), base="develop")

        self.assertTrue(plan.ok, plan.errors)


# ---------------------------------------------------------------------------
# AC: The happy-path plan runs autosquash, rebase onto base, gating, PR
# create, rebase-merge, and PRD close, in that order
# ---------------------------------------------------------------------------
class FeatureCompletePlanHappyPath(unittest.TestCase):

    def _plan(self, **kwargs):
        kwargs.setdefault("base", "develop")
        kwargs.setdefault("feature_pattern", "feature/{issue}-{slug}")
        kwargs.setdefault("gating", [{"name": "test", "run": "make test"}])
        return ralph_feature.feature_complete_plan(prd_issue(), **kwargs)

    def test_plan_is_ok(self):
        plan = self._plan()
        self.assertTrue(plan.ok, plan.errors)

    def test_steps_run_in_order(self):
        # #120: the pass syncs the Feature branch first and pushes what it
        # gated before the pull request is opened, so what merges was gated.
        self.assertEqual([step["name"] for step in self._plan().steps],
                         ["sync", "autosquash", "rebase", "gating", "push",
                          "pr-create", "prd-close", "cleanup"])

    def _step(self, name, **kwargs):
        return next(s for s in self._plan(**kwargs).steps if s["name"] == name)

    def test_sync_checks_out_the_feature_branch_from_origin_whatever_head_was(self):
        branch = "feature/18-per-feature-integration-branches"
        self.assertEqual(self._step("sync")["commands"], [
            ["git", "fetch", "origin"],
            ["git", "checkout", "-q", "--no-track", "-B", branch, "origin/" + branch],
        ])

    def test_autosquash_and_rebase_target_the_remote_base(self):
        self.assertIn("--autosquash", _flat(self._step("autosquash")["commands"]))
        self.assertEqual(self._step("autosquash")["commands"][0][-1], "origin/develop")
        self.assertEqual(self._step("rebase")["commands"], [["git", "rebase", "origin/develop"]])

    def test_the_gated_branch_is_pushed_with_lease_by_name(self):
        branch = "feature/18-per-feature-integration-branches"
        ref = "refs/heads/%s" % branch
        self.assertEqual(self._step("push")["commands"], [
            ["git", "push", "--force-with-lease=%s" % ref, "origin", "%s:%s" % (ref, ref)],
        ])

    def test_pr_create_then_rebase_merge(self):
        tokens = _flat(self._step("pr-create")["commands"])
        self.assertIn("create", tokens)
        self.assertIn("--rebase", tokens)
        # The local branch is checked out here, so gh must not try to delete
        # it; the remote branch goes in the best-effort cleanup step instead.
        self.assertNotIn("--delete-branch", tokens)

    def test_prd_close_then_best_effort_remote_cleanup(self):
        tokens = _flat(self._step("prd-close")["commands"])
        self.assertIn("close", tokens)
        self.assertIn("18", tokens)
        cleanup = self._step("cleanup")
        self.assertTrue(cleanup.get("fallback_ok"))
        self.assertEqual(cleanup["commands"], [
            ["git", "push", "origin", "--delete",
             "refs/heads/feature/18-per-feature-integration-branches"]])

    def test_merge_rebases_never_a_merge_commit_never_squash(self):
        # develop requires linear history, so GitHub refuses a merge commit;
        # a squash would collapse the Feature's per-Story commits into one.
        merge = self._step("pr-create")["commands"][1]
        self.assertIn("--rebase", merge)
        self.assertNotIn("--merge", merge)
        self.assertNotIn("--squash", merge)

    def test_branch_resolves_from_prd(self):
        plan = self._plan()
        self.assertEqual(plan.branch, "feature/18-per-feature-integration-branches")

    def test_never_mentions_main(self):
        plan = self._plan()
        for step in plan.steps:
            self.assertNotIn("main", _flat(step["commands"]))

    def test_honors_custom_feature_pattern(self):
        plan = self._plan(feature_pattern="feat/{issue}/{slug}")
        self.assertEqual(plan.branch, "feat/18/per-feature-integration-branches")


# ---------------------------------------------------------------------------
# AC: An autosquash conflict falls back to the uncollapsed linear history and
# the pass continues
# ---------------------------------------------------------------------------
class AutosquashFallback(unittest.TestCase):

    def test_autosquash_step_has_fallback_flag(self):
        plan = ralph_feature.feature_complete_plan(
            prd_issue(), base="develop",
            feature_pattern="feature/{issue}-{slug}",
            gating=[{"name": "test", "run": "make test"}])
        autosquash = next(s for s in plan.steps if s["name"] == "autosquash")
        self.assertTrue(autosquash.get("fallback_ok"),
                        "autosquash step must tolerate failure (cosmetic fallback)")


# ---------------------------------------------------------------------------
# AC: A rebase conflict or red gate ends with branch pushed, PRD commented and
# labeled ready-for-human, no merge, no needs-human
# ---------------------------------------------------------------------------
class RebaseConflictOrRedGate(unittest.TestCase):

    def _plan(self):
        return ralph_feature.feature_complete_plan(
            prd_issue(), base="develop",
            feature_pattern="feature/{issue}-{slug}",
            gating=[{"name": "test", "run": "make test"}])

    def _blocking(self, plan):
        return [s for s in plan.steps if s["name"] in ("rebase", "gating")]

    def test_rebase_step_has_blocker_commands(self):
        plan = self._plan()
        rebase = self._blocking(plan)[0]
        self.assertIn("on_fail", rebase)
        fail_cmds = rebase["on_fail"]
        tokens = _flat(fail_cmds)
        self.assertIn("push", tokens)
        self.assertIn("ready-for-human", tokens)
        self.assertNotIn("needs-human", tokens)

    def test_gating_step_has_blocker_commands(self):
        plan = self._plan()
        gating = self._blocking(plan)[1]
        self.assertIn("on_fail", gating)
        fail_cmds = gating["on_fail"]
        tokens = _flat(fail_cmds)
        self.assertIn("push", tokens)
        self.assertIn("ready-for-human", tokens)
        self.assertNotIn("needs-human", tokens)

    def test_blocker_commands_comment_on_prd(self):
        plan = self._plan()
        for step in self._blocking(plan):
            fail_cmds = step["on_fail"]
            self.assertTrue(
                any(c[:3] == ["gh", "issue", "comment"] for c in fail_cmds),
                "%s on_fail must comment on the PRD" % step["name"])

    def test_blocker_push_names_the_feature_branch_never_head(self):
        # AGENTS.md: publication names both refs; HEAD is not evidence of
        # which branch is the Feature's, and -u must not move the upstream.
        ref = "refs/heads/feature/18-per-feature-integration-branches"
        for step in self._blocking(self._plan()):
            push = next(c for c in step["on_fail"] if c[:2] == ["git", "push"])
            self.assertEqual(push, ["git", "push", "--force-with-lease=%s" % ref,
                                    "origin", "%s:%s" % (ref, ref)])

    def test_blocker_commands_label_prd_ready_for_human(self):
        plan = self._plan()
        for step in self._blocking(plan):
            fail_cmds = step["on_fail"]
            self.assertTrue(
                any(c[:3] == ["gh", "issue", "edit"] and "ready-for-human" in c
                    for c in fail_cmds),
                "%s on_fail must label PRD ready-for-human" % step["name"])


# ---------------------------------------------------------------------------
# AC: The pass integrates into a `main` base like any other
# ---------------------------------------------------------------------------
class AcceptMainBase(unittest.TestCase):

    def _plan(self, base):
        return ralph_feature.feature_complete_plan(
            prd_issue(), base=base,
            feature_pattern="feature/{issue}-{slug}",
            gating=[{"name": "test", "run": "make test"}])

    def test_integrates_into_main(self):
        plan = self._plan("main")
        self.assertTrue(plan.ok, plan.errors)
        rebase = next(s for s in plan.steps if s["name"] == "rebase")
        self.assertEqual(rebase["commands"], [["git", "rebase", "origin/main"]])
        pr = next(s for s in plan.steps if s["name"] == "pr-create")
        create = pr["commands"][0]
        self.assertEqual(create[create.index("--base") + 1], "main")

    def test_accepts_main_case_insensitive(self):
        self.assertTrue(self._plan("Main").ok)


# ---------------------------------------------------------------------------
# Plan runner tests (exercises the step-by-step executor with fallbacks)
# ---------------------------------------------------------------------------
class RunFeaturePlan(unittest.TestCase):

    def _simple_plan(self):
        return ralph_feature.feature_complete_plan(
            prd_issue(), base="develop",
            feature_pattern="feature/{issue}-{slug}",
            gating=[{"name": "test", "run": "true"}])

    def test_happy_path_all_green(self):
        plan = self._simple_plan()
        result = ralph_feature.run_feature_plan(plan, cwd="/tmp",
                                                 dry_run=True)
        self.assertTrue(result.ok)

    def test_autosquash_conflict_continues(self):
        """Autosquash failure is cosmetic; the pass continues."""
        plan = self._simple_plan()
        result = ralph_feature.run_feature_plan(
            plan, cwd="/tmp", dry_run=True,
            inject_failures={"autosquash"})
        self.assertTrue(result.ok)

    def test_rebase_conflict_stops_and_runs_on_fail(self):
        plan = self._simple_plan()
        result = ralph_feature.run_feature_plan(
            plan, cwd="/tmp", dry_run=True,
            inject_failures={"rebase"})
        self.assertFalse(result.ok)
        self.assertEqual(result.failed_step, "rebase")
        self.assertTrue(result.on_fail_ran)

    def test_gating_failure_stops_and_runs_on_fail(self):
        plan = self._simple_plan()
        result = ralph_feature.run_feature_plan(
            plan, cwd="/tmp", dry_run=True,
            inject_failures={"gating"})
        self.assertFalse(result.ok)
        self.assertEqual(result.failed_step, "gating")
        self.assertTrue(result.on_fail_ran)


class TheGatedBranchIsWhatMerges(unittest.TestCase):
    """#120 on real git: whatever HEAD and the local base were, the pass
    rebases the Feature branch from origin onto origin/<base>, autosquashes
    its fixups and pushes that before any pull request is opened."""

    def git(self, *args, cwd=None):
        return subprocess.check_output(
            ["git", "-c", "user.email=t@t", "-c", "user.name=t", *args],
            cwd=cwd or self.repo, stderr=subprocess.STDOUT, text=True).strip()

    def test_feature_is_rebased_on_origin_base_and_pushed(self):
        with tempfile.TemporaryDirectory() as root:
            remote = os.path.join(root, "remote.git")
            self.repo = os.path.join(root, "repo")
            other = os.path.join(root, "other")
            self.git("init", "-q", "--bare", "-b", "develop", remote, cwd=root)
            self.git("clone", "-q", remote, self.repo, cwd=root)
            self.git("commit", "-q", "--allow-empty", "-m", "base")
            self.git("push", "-q", "origin", "HEAD:develop")
            plan = ralph_feature.feature_complete_plan(
                prd_issue(), base="develop", gating=[{"name": "t", "run": "true"}])
            branch = plan.branch
            self.git("switch", "-q", "-c", branch)
            with open(os.path.join(self.repo, "f.txt"), "w") as fh:
                fh.write("story\n")
            self.git("add", "f.txt")
            self.git("commit", "-q", "-m", "feat: story")
            with open(os.path.join(self.repo, "f.txt"), "a") as fh:
                fh.write("fix\n")
            self.git("commit", "-q", "-am", "fixup! feat: story")
            self.git("push", "-q", "origin", branch)
            # Someone else moves origin/develop on; the local develop is stale.
            self.git("clone", "-q", remote, other, cwd=root)
            self.git("commit", "-q", "--allow-empty", "-m", "base moves on", cwd=other)
            self.git("push", "-q", "origin", "HEAD:develop", cwd=other)
            moved = self.git("rev-parse", "HEAD", cwd=other)
            # The previous Story left the checkout somewhere else entirely.
            self.git("switch", "-q", "-c", "ralph/99-unrelated", "develop")
            self.git("commit", "-q", "--allow-empty", "-m", "unrelated")

            git_only = ralph_feature.Plan(True, [], [
                dict(step, commands=[c for c in step["commands"] if c[0] == "git"])
                for step in plan.steps if step["name"] not in ("pr-create", "prd-close", "cleanup")
            ], branch=branch, base="develop")
            # The plan's own git calls need an identity to rebase with; CI
            # has no global one.
            identity = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
                        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
            saved = {k: os.environ.get(k) for k in identity}
            os.environ.update(identity)
            try:
                result = ralph_feature.run_feature_plan(git_only, cwd=self.repo)
            finally:
                for k, v in saved.items():
                    if v is None:
                        os.environ.pop(k, None)
                    else:
                        os.environ[k] = v
            self.assertTrue(result.ok, result.failed_step)

            pushed = self.git("rev-parse", "refs/heads/" + branch, cwd=remote)
            self.assertEqual(self.git("rev-parse", pushed + "^", cwd=remote), moved)
            self.assertEqual(self.git("log", "--format=%s", "%s..%s" % (moved, pushed),
                                      cwd=remote), "feat: story")


# ---------------------------------------------------------------------------
# CLI smoke tests against mocked git/gh
# ---------------------------------------------------------------------------
class CliCompleteFeature(unittest.TestCase):

    def _mockbin(self, tmp, git_exit=0, gh_exit=0):
        log = os.path.join(tmp, "calls.log")
        for name, code in (("git", git_exit), ("gh", gh_exit), ("make", 0)):
            path = os.path.join(tmp, name)
            with open(path, "w") as fh:
                fh.write('#!/usr/bin/env bash\n'
                         'echo "%s $*" >> "$RALPH_LOG"\n'
                         '''if [[ "%s" == git && "$1" == rev-parse ]]; then echo aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa; fi
if [[ "%s" == gh && "$1 $2" == "pr list" ]]; then echo '[]'; fi
if [[ "%s" == gh && "$1 $2" == "pr view" ]]; then echo '{"headRefOid":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","statusCheckRollup":[{"context":"build","state":"SUCCESS"},{"context":"test","state":"SUCCESS"},{"context":"ralph/model-review","state":"SUCCESS"}]}'; fi
exit %d
''' % (name, name, name, name, code))
            os.chmod(path, os.stat(path).st_mode | stat.S_IEXEC | stat.S_IXGRP)
        return log

    def _run(self, prd, config, tmp, log):
        env = dict(os.environ, PATH=tmp + os.pathsep + os.environ["PATH"],
                   RALPH_LOG=log)
        prd_path = os.path.join(tmp, "prd.json")
        with open(prd_path, "w") as fh:
            json.dump(prd, fh)
        return subprocess.run(
            [RALPH, "--complete-feature", prd_path, config],
            cwd=REPO_ROOT, env=env,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )

    def test_happy_path_merges_and_closes_prd(self):
        with tempfile.TemporaryDirectory() as tmp:
            log = self._mockbin(tmp)
            config = os.path.join(FIXTURES, "config", "valid", "full.yml")
            proc = self._run(prd_issue(), config, tmp, log)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            with open(log) as fh:
                calls = fh.read()
            self.assertIn("pr merge", calls)
            self.assertIn("issue close 18", calls)
            self.assertIn("--base main", calls)  # the configured base

    def test_an_unverified_hil_story_refuses_the_integration(self):
        with tempfile.TemporaryDirectory() as tmp:
            log = self._mockbin(tmp)
            backlog = json.dumps([prd_issue(), child(20, type_="hil")])
            gh = os.path.join(tmp, "gh")
            with open(gh, "w") as fh:
                fh.write('#!/usr/bin/env bash\n'
                         'echo "gh $*" >> "$RALPH_LOG"\n'
                         'if [[ "$1 $2" == "issue list" ]]; then\n'
                         '  cat <<\'JSON\'\n%s\nJSON\n'
                         'fi\n' % backlog)
            os.chmod(gh, os.stat(gh).st_mode | stat.S_IEXEC)
            config = os.path.join(FIXTURES, "config", "valid", "full.yml")
            proc = self._run(prd_issue(), config, tmp, log)

            self.assertEqual(proc.returncode, 2, proc.stdout)
            self.assertIn("#20", proc.stderr)
            with open(log) as fh:
                calls = fh.read()
            self.assertNotIn("pr merge", calls)
            self.assertNotIn("issue close", calls)

    def test_main_base_is_not_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            log = self._mockbin(tmp)
            config = os.path.join(FIXTURES, "config", "valid", "base-main.yml")
            proc = self._run(prd_issue(), config, tmp, log)
            self.assertNotIn("refusing", proc.stderr)
            self.assertTrue(os.path.exists(log))  # the plan ran


class RequiredFeatureChecks(unittest.TestCase):
    def test_required_contexts_must_explicitly_succeed(self):
        required = ['test', 'ralph/model-review']
        for bad in ['FAILURE', 'ERROR', 'PENDING', 'EXPECTED', 'SKIPPED', 'NEUTRAL']:
            with self.subTest(bad=bad):
                pr = dict(statusCheckRollup=[dict(context='test', state=bad),
                    dict(context='ralph/model-review', state='SUCCESS')])
                failing, pending = ralph_feature.required_check_verdict(pr, required)
                self.assertTrue(failing or pending)
        self.assertEqual(ralph_feature.required_check_verdict({}, required), ([], required))

    def test_check_runs_statuses_and_multiple_gating_steps(self):
        pr = dict(statusCheckRollup=[dict(name='build', status='COMPLETED', conclusion='SUCCESS'),
            dict(context='test', state='SUCCESS'), dict(context='ralph/model-review', state='SUCCESS')])
        self.assertEqual(ralph_feature.required_check_verdict(pr,
                         ['build', 'test', 'ralph/model-review']), ([], []))

    def test_head_change_does_not_merge_and_merge_race_is_pinned(self):
        from unittest.mock import patch
        plan = ralph_feature.feature_complete_plan(prd_issue(), gating=[dict(name='test', run='true')],
                                                   backlog=[prd_issue()])
        step = next(s for s in plan.steps if s['name'] == 'pr-create')
        for changed in (False, True):
            calls = []
            def run(args, **kwargs):
                calls.append(args)
                output = ''
                if args[:2] == ['git', 'rev-parse']:
                    output = 'a' * 40
                elif args[1:3] == ['pr', 'list']:
                    output = '[{"number": 99}]'
                elif args[1:3] == ['pr', 'view']:
                    output = json.dumps(dict(headRefOid=('b' if changed else 'a') * 40,
                        statusCheckRollup=[dict(context=n, state='SUCCESS') for n in
                                          ['test', 'ralph/model-review']]))
                elif args[1:3] == ['pr', 'merge']:
                    # GitHub refuses if a push races the read: never retry without the pin.
                    raise subprocess.CalledProcessError(1, args)
                return subprocess.CompletedProcess(args, 0, stdout=output)
            with patch.object(ralph_feature.subprocess, 'run', side_effect=run):
                result = ralph_feature._checked_feature_pr(plan, step, '/tmp')
            merges = [a for a in calls if a[1:3] == ['pr', 'merge']]
            if changed:
                self.assertTrue(result.pending)
                self.assertEqual(merges, [])
            else:
                self.assertFalse(result.ok)
                self.assertEqual(merges[0][-2:], ['--match-head-commit', 'a' * 40])
                self.assertNotIn('--admin', merges[0])

    def test_unread_approval_evidence_takes_the_blocker_path(self):
        """#172 F-1: a gh failure reading a Story's comments refuses, never crashes."""
        from unittest.mock import patch
        story = child(20, state="CLOSED")
        plan = ralph_feature.feature_complete_plan(prd_issue(), gating=[dict(name='test', run='true')],
                                                   backlog=[prd_issue(), story])
        step = next(s for s in plan.steps if s['name'] == 'pr-create')
        calls = []

        def run(args, **kwargs):
            calls.append(args)
            output = ''
            if args[:2] == ['git', 'rev-parse']:
                output = 'a' * 40
            elif args[1:3] == ['pr', 'list'] and '--state' in args and 'merged' in args:
                # Story #20 merged through a managed PR with no passing review,
                # so its human-approval evidence must be read.
                output = json.dumps([dict(number=30, body="<!-- ralph-managed-pr:v1 -->\n\nRefs #20",
                                          headRefOid='h' * 40, statusCheckRollup=[])])
            elif args[1:3] == ['pr', 'list']:
                output = '[{"number": 99}]'
            elif args[1:3] == ['pr', 'view']:
                output = json.dumps(dict(headRefOid='a' * 40, url='https://x/pull/99',
                    statusCheckRollup=[dict(context='test', state='SUCCESS')]))
            elif args[1:3] == ['pr', 'diff']:
                output = 'diff --git a/f b/f\n'
            elif args[1:3] == ['issue', 'view']:
                return subprocess.CompletedProcess(args, 1, stdout='temporary API error')
            elif args[:2] == ['git', 'patch-id']:
                output = 'p1 ' + '0' * 40
            return subprocess.CompletedProcess(args, 0, stdout=output)
        with patch.object(ralph_feature.subprocess, 'run', side_effect=run):
            result = ralph_feature._checked_feature_pr(plan, step, '/tmp')
        self.assertFalse(result.ok)
        self.assertEqual(result.failed_step, 'github-checks')
        comments = [a for a in calls if a[1:3] == ['issue', 'comment']]
        self.assertEqual(len(comments), 1)
        self.assertIn('story/#20', comments[0][-1])
        self.assertIn('temporary API error', comments[0][-1])
        self.assertIn(['gh', 'issue', 'edit', '18', '--add-label', 'ready-for-human'],
                      [a for a in calls if a[1:3] == ['issue', 'edit']])
        self.assertEqual([a for a in calls if a[1:2] == ['api'] or a[1:3] == ['pr', 'merge']], [])


# ---------------------------------------------------------------------------
# #172: the Feature pull request inherits its Story pull requests' reviews
# ---------------------------------------------------------------------------
MARKER = "<!-- ralph-managed-pr:v1 -->"


def story_pr(number, story, head="h" * 40, patch="p1", review="SUCCESS"):
    rollup = [dict(context="test", state="SUCCESS")]
    if review:
        rollup.append(dict(context="ralph/model-review", state=review))
    return dict(number=number, body="%s\n\nRefs #%d" % (MARKER, story),
                headRefOid=head, statusCheckRollup=rollup, patch_id=patch)


def commit(sha="c" * 40, subject="feat: story", patch="p1"):
    return dict(sha=sha, subject=subject, patch_id=patch)


def approval(head):
    import ralph_review
    return dict(body=ralph_review.arbitration_record(dict(
        review="PRR_1", decision="APPROVED", reviewer="maintainer",
        head=head, overrode=[], note="ok")))


class InheritedModelReview(unittest.TestCase):

    def test_reviewed_unchanged_stories_are_inherited_naming_their_prs(self):
        verdict = ralph_feature.inheritance_verdict(
            [20, 21], [story_pr(30, 20), story_pr(31, 21, patch="p2")], {},
            [commit(patch="p1"), commit("d" * 40, patch="p2")])
        self.assertTrue(verdict.ok, verdict.errors)
        self.assertEqual(verdict.sources, [30, 31])
        description = ralph_feature.inherited_description(verdict.sources)
        self.assertIn("#30", description)
        self.assertIn("#31", description)

    def test_a_commit_no_story_pr_carried_is_named(self):
        verdict = ralph_feature.inheritance_verdict(
            [20], [story_pr(30, 20)], {},
            [commit(), commit("e" * 40, "feat: smuggled", patch="px")])
        self.assertFalse(verdict.ok)
        self.assertEqual(len(verdict.errors), 1)
        self.assertIn("eeeeeee", verdict.errors[0])
        self.assertIn("feat: smuggled", verdict.errors[0])
        self.assertEqual(verdict.sources, [])

    def test_a_commit_without_a_patch_is_never_matched(self):
        verdict = ralph_feature.inheritance_verdict(
            [20], [story_pr(30, 20, patch=None)], {}, [commit(patch=None)])
        self.assertFalse(verdict.ok)

    def test_a_story_with_no_merged_pr_is_named(self):
        verdict = ralph_feature.inheritance_verdict([20, 21], [story_pr(30, 20)], {},
                                                    [commit()])
        self.assertFalse(verdict.ok)
        self.assertTrue(any("story/#21" in e for e in verdict.errors), verdict.errors)

    def test_an_unreviewed_story_pr_is_named_and_lends_no_patch(self):
        for review in (None, "FAILURE", "PENDING"):
            with self.subTest(review=review):
                verdict = ralph_feature.inheritance_verdict(
                    [20], [story_pr(30, 20, review=review)], {}, [commit()])
                self.assertFalse(verdict.ok)
                joined = " ".join(verdict.errors)
                self.assertIn("story/#20", joined)
                self.assertIn("#30", joined)
                self.assertIn("commit/ccccccc", joined)

    def test_a_human_approval_of_the_merged_head_is_inherited(self):
        head = "a" * 40
        verdict = ralph_feature.inheritance_verdict(
            [20], [story_pr(30, 20, head=head, review=None)],
            {20: [approval(head)]}, [commit()])
        self.assertTrue(verdict.ok, verdict.errors)
        stale = ralph_feature.inheritance_verdict(
            [20], [story_pr(30, 20, head=head, review=None)],
            {20: [approval("b" * 40)]}, [commit()])
        self.assertFalse(stale.ok)

    def test_an_unread_backlog_is_never_an_inherited_review(self):
        verdict = ralph_feature.inheritance_verdict(None, [], {}, [])
        self.assertFalse(verdict.ok)
        plan = ralph_feature.feature_complete_plan(prd_issue())
        step = next(s for s in plan.steps if s["name"] == "pr-create")
        self.assertIsNone(step["stories"])

    def test_the_plan_carries_every_story_of_this_feature(self):
        plan = ralph_feature.feature_complete_plan(prd_issue(), backlog=[
            prd_issue(), child(21, state="CLOSED"), child(20, state="CLOSED"),
            child(40, parent=99, state="CLOSED")])
        step = next(s for s in plan.steps if s["name"] == "pr-create")
        self.assertEqual(step["stories"], [20, 21])

    def test_the_status_is_written_on_the_given_head_only(self):
        argv = ralph_feature.inheritance_status("f" * 40, [30])
        self.assertIn("repos/{owner}/{repo}/statuses/" + "f" * 40, argv)
        self.assertIn("state=success", argv)
        self.assertIn("context=ralph/model-review", argv)
        self.assertEqual([a for a in argv if "statuses/" in a],
                         ["repos/{owner}/{repo}/statuses/" + "f" * 40])


class FeatureCommitPatches(unittest.TestCase):
    """Patch-ids against real git: a rebase that alters a Story's patch is caught."""

    def git(self, *args, cwd=None):
        return subprocess.check_output(
            ["git", "-c", "user.email=t@t", "-c", "user.name=t", *args],
            cwd=cwd or self.repo, stderr=subprocess.STDOUT, text=True).strip()

    def write(self, path, text, cwd=None):
        with open(os.path.join(cwd or self.repo, path), "w") as fh:
            fh.write(text)

    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.addCleanup(subprocess.run, ["rm", "-rf", self.root])
        remote = os.path.join(self.root, "remote.git")
        self.repo = os.path.join(self.root, "repo")
        self.other = os.path.join(self.root, "other")
        self.git("init", "-q", "--bare", "-b", "main", remote, cwd=self.root)
        self.git("clone", "-q", remote, self.repo, cwd=self.root)
        self.write("f.txt", "a\nb\nc\nd\ne\n")
        self.git("add", "f.txt")
        self.git("commit", "-q", "-m", "base")
        self.git("push", "-q", "origin", "HEAD:main")
        base = self.git("rev-parse", "HEAD")
        self.git("switch", "-q", "-c", "feature/1-x")
        self.write("f.txt", "a\nb\nc\nd\ne\nstory\n")
        self.git("commit", "-q", "-am", "feat: story")
        # The Story pull request's diff: its head against its base.
        self.reviewed = ralph_feature._patch_id(
            self.git("diff", base, "HEAD") + "\n", self.repo)
        self.git("clone", "-q", remote, self.other, cwd=self.root)

    def move_base(self, path, text):
        self.write(path, text, cwd=self.other)
        self.git("add", path, cwd=self.other)
        self.git("commit", "-q", "-m", "base moves on", cwd=self.other)
        self.git("push", "-q", "origin", "HEAD:main", cwd=self.other)
        self.git("fetch", "-q", "origin")
        self.git("rebase", "-q", "origin/main")

    def verdict(self):
        commits = ralph_feature.feature_commits("main", "feature/1-x", self.repo)
        self.assertEqual([c["subject"] for c in commits], ["feat: story"])
        pr = story_pr(30, 1, patch=self.reviewed)
        return ralph_feature.inheritance_verdict([1], [pr], {}, commits)

    def test_an_unchanged_story_commit_matches_its_reviewed_diff(self):
        self.assertTrue(self.verdict().ok, self.verdict().errors)

    def test_a_base_change_elsewhere_leaves_the_patch_unchanged(self):
        self.move_base("g.txt", "unrelated\n")
        self.assertTrue(self.verdict().ok)

    def test_a_rebase_that_alters_the_patch_is_named(self):
        # Within the hunk's context but not in conflict: the rebase applies
        # cleanly and still changes what the reviewer saw.
        self.move_base("f.txt", "a\nb\nc\nD\ne\n")
        verdict = self.verdict()
        self.assertFalse(verdict.ok)
        self.assertIn("feat: story", verdict.errors[0])


if __name__ == "__main__":
    unittest.main()
