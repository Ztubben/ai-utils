"""Every Tick runs an immutable copy of its Tooling Commit (#146, ADR-0009),
and copies nothing needs any more are pruned (#147).

The pure half -- where the running ai-utils sits, and which commit is the
Tooling Commit -- is tested directly. The rest runs the real `bin/ralph.sh`, as
the scheduler would, from a checkout of ai-utils that is its own target
repository, on real git with a bare `origin`: the Tick must run `origin/main`'s
code from a frozen copy even while the checkout's `bin/ralph.sh` and `lib/`
say otherwise, and so must the agents it launches. (The Superproject half is
the scenario tier's `pointer_*` worlds.)
"""
import fcntl
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import textwrap
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(REPO_ROOT, "lib"))

import ralph_agent  # noqa: E402
import ralph_tooling  # noqa: E402

FULL_CONFIG = os.path.join(REPO_ROOT, "test", "fixtures", "config", "valid", "full.yml")
TOOLING = ("bin", "lib", "schema", "prompts")
GIT_ENV = {"GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1",
           "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
A, B = "a" * 40, "b" * 40


def reader(answers):
    """A git reader answering from a dict keyed by the question."""
    return lambda kind, *args: answers.get((kind,) + args)


class Where(unittest.TestCase):
    def test_the_checkout_root_is_self_hosting(self):
        self.assertEqual(ralph_tooling.where("/r", "/r"), (ralph_tooling.SELF_HOSTED, None))

    def test_a_nested_ai_utils_is_a_superproject_mount(self):
        self.assertEqual(ralph_tooling.where("/r/tools/ai-utils", "/r"),
                         (ralph_tooling.SUPERPROJECT, "tools/ai-utils"))

    def test_an_ai_utils_outside_the_checkout_is_elsewhere(self):
        self.assertEqual(ralph_tooling.where("/opt/ai-utils", "/r")[0], ralph_tooling.ELSEWHERE)
        self.assertEqual(ralph_tooling.where("/r", None)[0], ralph_tooling.ELSEWHERE)


class Resolve(unittest.TestCase):
    def test_self_hosting_resolves_origin_of_the_base(self):
        r = ralph_tooling.resolve("/r", "/r", "main", False,
                                  reader({("rev", "origin/main"): A}))
        self.assertTrue(r.frozen)
        self.assertEqual((r.kind, r.commit, r.repo), (ralph_tooling.SELF_HOSTED, A, "/r"))

    def test_a_superproject_resolves_the_gitlink_in_origin_of_the_base(self):
        r = ralph_tooling.resolve("/r/ai-utils", "/r", "develop", False, reader({
            ("gitlink", "origin/develop", "ai-utils"): A,
            ("gitlink", "HEAD", "ai-utils"): B}))
        self.assertTrue(r.frozen)
        self.assertEqual((r.kind, r.commit, r.repo, r.mount),
                         (ralph_tooling.SUPERPROJECT, A, "/r/ai-utils", "ai-utils"))

    def test_allowed_drift_runs_the_mounted_checkout_unfrozen(self):
        r = ralph_tooling.resolve("/r/ai-utils", "/r", "main", True,
                                  reader({("gitlink", "origin/main", "ai-utils"): A}))
        self.assertTrue(r.ok)
        self.assertFalse(r.frozen)
        self.assertIn("allow_pointer_drift", r.message)

    def test_an_ai_utils_installed_elsewhere_is_not_frozen(self):
        r = ralph_tooling.resolve("/opt/ai-utils", "/r", "main", False, reader({}))
        self.assertTrue(r.ok)
        self.assertFalse(r.frozen)

    def test_an_unresolvable_commit_is_refused(self):
        self.assertFalse(ralph_tooling.resolve("/r", "/r", "main", False, reader({})).ok)
        r = ralph_tooling.resolve("/r/ai-utils", "/r", "main", False, reader({}))
        self.assertFalse(r.ok)
        self.assertIn("ai-utils", r.message)

    def test_a_copy_lives_in_the_git_common_directory_named_by_its_commit(self):
        self.assertEqual(ralph_tooling.copy_path("/r/.git", A),
                         os.path.join("/r/.git", "ralph-tooling", A))


C = "c" * 40


class PrunePlan(unittest.TestCase):
    def test_only_copies_nothing_needs_are_pruned(self):
        self.assertEqual(ralph_tooling.prune_plan([A, B, C], [A], None, ()), [B, C])

    def test_the_current_and_last_known_good_copies_are_never_pruned(self):
        self.assertEqual(ralph_tooling.prune_plan([A, B, C], [A], B, ()), [C])

    def test_a_copy_a_running_tick_uses_is_never_pruned(self):
        self.assertEqual(ralph_tooling.prune_plan([A, B, C], [A], None, [C]), [B])

    def test_several_commits_can_be_kept(self):
        self.assertEqual(ralph_tooling.prune_plan([A, B, C], [A, B], None, ()), [C])


class TheAgentRunsTheSameTooling(unittest.TestCase):
    """Every adapter, every role: the launcher's own bin/ comes first."""

    def test_every_adapter_and_role_puts_the_tooling_bin_first(self):
        for name, adapter in sorted(ralph_agent.PROVIDERS.items()):
            for role in ("implementation", "review"):
                with self.subTest(provider=name, role=role):
                    path = adapter(model="m", role=role).environment()["PATH"]
                    self.assertEqual(path.split(os.pathsep)[0], ralph_agent.TOOLING_BIN)
                    self.assertEqual(path.split(os.pathsep).count(ralph_agent.TOOLING_BIN), 1)


def _write_exec(path, contents):
    with open(path, "w") as fh:
        fh.write(contents)
    os.chmod(path, os.stat(path).st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)


class SelfHostedWorld:
    """ai-utils as its own target repository: a checkout of this working tree's
    tooling, pushed to a bare `origin` as `main`, with mock gh and provider CLIs
    on PATH. `upstream` is a second clone that stands for merges into `main`."""

    def __init__(self, tmp):
        self.tmp = tmp
        self.origin = os.path.join(tmp, "origin.git")
        self.checkout = os.path.join(tmp, "checkout")
        self.upstream = os.path.join(tmp, "upstream")
        self.mockbin = os.path.join(tmp, "mockbin")
        self.log = os.path.join(tmp, "ralph.log")
        self.gh_dir = os.path.join(tmp, "gh")
        for d in (self.checkout, self.mockbin, self.gh_dir):
            os.makedirs(d)
        self.git("init", "-q", "--bare", "-b", "main", self.origin, cwd=tmp)
        self.git("init", "-q", "-b", "main")
        self.git("remote", "add", "origin", self.origin)
        for part in TOOLING:
            shutil.copytree(os.path.join(REPO_ROOT, part), os.path.join(self.checkout, part),
                            ignore=shutil.ignore_patterns("__pycache__"))
        shutil.copy(FULL_CONFIG, os.path.join(self.checkout, ".ralph.yml"))
        with open(os.path.join(self.checkout, ".gitignore"), "w") as fh:
            fh.write("__pycache__/\n")
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "ai-utils")
        self.git("push", "-q", "origin", "main")
        self.git("clone", "-q", self.origin, self.upstream, cwd=tmp)
        self.set_backlog([])
        self._write_mocks()

    def git(self, *args, cwd=None):
        return subprocess.run(["git"] + list(args), cwd=cwd or self.checkout,
                              env=dict(os.environ, **GIT_ENV), check=True,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              text=True).stdout.strip()

    def rev(self, ref, cwd=None):
        return self.git("rev-parse", ref, cwd=cwd)

    def copy(self, commit):
        return os.path.join(self.checkout, ".git", "ralph-tooling", commit)

    def merge_to_main(self, path, append, message="a merged Story"):
        """A change lands on origin/main, the way a merged pull request does."""
        self.git("pull", "-q", "--ff-only", cwd=self.upstream)
        with open(os.path.join(self.upstream, path), "a") as fh:
            fh.write(append)
        self.git("add", "-A", cwd=self.upstream)
        self.git("commit", "-q", "-m", message, cwd=self.upstream)
        self.git("push", "-q", "origin", "main", cwd=self.upstream)
        return self.rev("HEAD", cwd=self.upstream)

    def edit_checkout(self, path, old, new):
        """Change the checkout's own copy, as an agent working a Story would."""
        full = os.path.join(self.checkout, path)
        with open(full) as fh:
            text = fh.read()
        assert old in text, (path, old)
        with open(full, "w") as fh:
            fh.write(text.replace(old, new))

    def set_backlog(self, issues):
        with open(os.path.join(self.gh_dir, "backlog.json"), "w") as fh:
            json.dump(issues, fh)
        for issue in issues:
            with open(os.path.join(self.gh_dir, "issue-%d.json" % issue["number"]), "w") as fh:
                json.dump(issue, fh)

    def _write_mocks(self):
        _write_exec(os.path.join(self.mockbin, "gh"), textwrap.dedent("""\
            #!/usr/bin/env bash
            echo "gh $*" >> "$RALPH_LOG"
            if [[ "$1 $2" == "issue list" ]]; then
              cat "$RALPH_GH_DIR/backlog.json"
            elif [[ "$1 $2" == "issue view" ]]; then
              cat "$RALPH_GH_DIR/issue-$3.json" 2>/dev/null || echo '{}'
            fi
            """))
        for provider in sorted(ralph_agent.PROVIDERS):
            # The agent reports which `ralph` its PATH resolves, and whether the
            # tick that launched it still holds the tick lock.
            _write_exec(os.path.join(self.mockbin, provider), textwrap.dedent("""\
                #!/usr/bin/env bash
                cat > /dev/null
                echo "%s ralph=$(command -v ralph)" >> "$RALPH_LOG"
                if flock -n "$RALPH_LOCK_PROBE" -c true 2>/dev/null; then
                  echo "tick-lock free" >> "$RALPH_LOG"
                else
                  echo "tick-lock held" >> "$RALPH_LOG"
                fi
                """ % provider))

    def env(self):
        e = dict(os.environ)
        for name in list(e):
            if name.startswith("RALPH_"):
                del e[name]
        # Never the real provider CLIs: see the tick harness gotcha in AGENTS.md.
        for adapter in ralph_agent.PROVIDERS.values():
            e.pop(adapter.binary_env, None)
        e.update(GIT_ENV)
        e["PATH"] = self.mockbin + os.pathsep + e["PATH"]
        e["RALPH_LOG"] = self.log
        e["RALPH_GH_DIR"] = self.gh_dir
        e["RALPH_LOCK_PROBE"] = os.path.join(self.checkout, ".git", "ralph-tick.lock")
        e["RALPH_MAX_ITERATIONS"] = "1"
        return e

    def tick(self):
        return subprocess.run([os.path.join(self.checkout, "bin", "ralph.sh")],
                              cwd=self.checkout, env=self.env(), stdout=subprocess.PIPE,
                              stderr=subprocess.STDOUT, text=True, timeout=120)

    def log_lines(self):
        if not os.path.exists(self.log):
            return []
        with open(self.log) as fh:
            return [ln.rstrip("\n") for ln in fh if ln.strip()]


def ready_story(number=7):
    return {"number": number, "title": "S%d" % number, "state": "OPEN",
            "labels": [{"name": n} for n in ("state:ready", "type:afk", "prio:1")],
            "body": "## Acceptance Criteria\n- [ ] it works\n\nParent: None\n"
                    "Depends on: None\n"}


# What the checkout's own (unmerged) tooling would say if it ran.
CHECKOUT_MARK = "the checkout's own code ran"


class SelfHostedTicksRunFrozenTooling(unittest.TestCase):
    def world(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(lambda: shutil.rmtree(tmp, ignore_errors=True))
        return SelfHostedWorld(tmp)

    def break_checkout(self, w):
        """The checkout's ralph.sh and lib/ as a half-written Story leaves them."""
        w.edit_checkout("bin/ralph.sh", 'log "no eligible work; tick complete"',
                        'log "%s"' % CHECKOUT_MARK)
        with open(os.path.join(w.checkout, "lib", "ralph_select.py"), "a") as fh:
            fh.write("\nraise SystemExit(%r)\n" % CHECKOUT_MARK)

    def assert_ran_frozen(self, w, proc, commit):
        self.assertEqual(proc.returncode, 0, proc.stdout)
        self.assertNotIn(CHECKOUT_MARK, proc.stdout)
        self.assertIn("running the self-hosted Tooling Commit %s from %s"
                      % (commit, w.copy(commit)), proc.stdout)
        self.assertIn("no eligible work; tick complete", proc.stdout)

    def test_uncommitted_tooling_changes_do_not_run(self):
        w = self.world()
        self.break_checkout(w)
        self.assert_ran_frozen(w, w.tick(), w.rev("origin/main"))

    def test_tooling_changes_on_another_branch_do_not_run(self):
        w = self.world()
        w.git("checkout", "-q", "-b", "ralph/7-half-written")
        self.break_checkout(w)
        w.git("commit", "-q", "-am", "WIP")
        self.assert_ran_frozen(w, w.tick(), w.rev("origin/main"))

    def test_the_tick_runs_origin_main_as_of_its_start(self):
        w = self.world()
        merged = w.merge_to_main("README.md", "merged\n")
        self.assertNotEqual(merged, w.rev("HEAD"))
        self.assert_ran_frozen(w, w.tick(), merged)

    def test_a_copy_is_created_once_and_reused_unchanged(self):
        w = self.world()
        commit = w.rev("origin/main")
        first = w.tick()
        self.assertIn("created the frozen copy of %s" % commit, first.stdout)
        marker = os.path.join(w.copy(commit), "lib", "ralph_tooling.py")
        before = os.stat(marker).st_mtime_ns
        second = w.tick()
        self.assert_ran_frozen(w, second, commit)
        self.assertIn("reusing the frozen copy of %s" % commit, second.stdout)
        self.assertNotIn("created the frozen copy", second.stdout)
        self.assertEqual(os.stat(marker).st_mtime_ns, before)
        self.assertEqual(w.git("status", "--porcelain", cwd=w.copy(commit)), "")
        self.assertEqual(w.rev("HEAD", cwd=w.copy(commit)), commit)

    def test_an_upgrade_runs_a_new_copy_and_never_edits_the_old_one(self):
        w = self.world()
        old = w.rev("origin/main")
        w.tick()
        new = w.merge_to_main("README.md", "merged\n")
        with open(os.path.join(w.checkout, ".git", "ralph-tooling", old + ".lock"),
                  "w") as held:
            # Another tick still runs from the old copy while this one upgrades.
            fcntl.flock(held, fcntl.LOCK_SH)
            self.assert_ran_frozen(w, w.tick(), new)
        self.assertEqual(w.rev("HEAD", cwd=w.copy(old)), old)
        self.assertEqual(w.git("status", "--porcelain", cwd=w.copy(old)), "")
        self.assertFalse(os.path.exists(os.path.join(w.copy(old), "README.md")))

    def test_the_re_exec_happens_once_and_keeps_the_lock(self):
        w = self.world()
        w.set_backlog([ready_story()])
        proc = w.tick()
        self.assertEqual(proc.returncode, 0, proc.stdout)
        self.assertEqual(proc.stdout.count("running the self-hosted Tooling Commit"), 1,
                         proc.stdout)
        self.assertIn("tick-lock held", w.log_lines())
        self.assertNotIn("tick-lock free", w.log_lines())

    def test_the_agents_ralph_is_the_frozen_copys(self):
        w = self.world()
        self.break_checkout(w)
        w.set_backlog([ready_story()])
        proc = w.tick()
        self.assertEqual(proc.returncode, 0, proc.stdout)
        frozen = os.path.join(w.copy(w.rev("origin/main")), "bin", "ralph")
        agent = [ln for ln in w.log_lines() if " ralph=" in ln]
        self.assertTrue(agent, proc.stdout)
        self.assertTrue(all(ln.endswith("ralph=" + frozen) for ln in agent), agent)

    def test_a_main_failing_preflight_is_refused_naming_commit_and_step(self):
        w = self.world()
        w.set_backlog([ready_story()])
        broken = w.merge_to_main("schema/ralph.schema.json", "not json\n",
                                 "a schema nobody can read")
        proc = w.tick()
        self.assertEqual(proc.returncode, 2, proc.stdout)
        self.assertIn("Tooling Commit %s fails preflight at the config check" % broken,
                      proc.stdout)
        self.assertFalse([ln for ln in w.log_lines() if ln.startswith("gh issue edit")])
        self.assertFalse([ln for ln in w.log_lines() if " ralph=" in ln])


class FrozenCopiesAreBounded(unittest.TestCase):
    """#147: after an upgrade, the old copy goes once no tick runs from it."""

    def world(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(lambda: shutil.rmtree(tmp, ignore_errors=True))
        return SelfHostedWorld(tmp)

    def upgraded(self):
        w = self.world()
        old = w.rev("origin/main")
        self.assertEqual(w.tick().returncode, 0)
        new = w.merge_to_main("README.md", "merged\n")
        return w, old, new

    def worktrees(self, w):
        return w.git("worktree", "list", "--porcelain")

    def test_the_previous_copy_is_pruned_on_a_later_tick(self):
        w, old, new = self.upgraded()
        proc = w.tick()
        self.assertEqual(proc.returncode, 0, proc.stdout)
        self.assertIn("pruned the frozen copy of %s" % old, proc.stdout)
        self.assertFalse(os.path.exists(w.copy(old)))
        self.assertNotIn(w.copy(old), self.worktrees(w))
        self.assertTrue(os.path.isdir(w.copy(new)))

    def test_the_last_known_good_copy_is_never_pruned(self):
        w, old, new = self.upgraded()
        with open(os.path.join(w.checkout, ".git", "ralph-tooling", "last-known-good"),
                  "w") as fh:
            fh.write(old + "\n")
        proc = w.tick()
        self.assertEqual(proc.returncode, 0, proc.stdout)
        self.assertTrue(os.path.isdir(w.copy(old)))
        self.assertNotIn("pruned", proc.stdout)

    def test_a_copy_a_running_tick_holds_is_never_pruned(self):
        w, old, new = self.upgraded()
        lock = os.path.join(w.checkout, ".git", "ralph-tooling", old + ".lock")
        with open(lock, "w") as held:
            # Another Slot's tick, still running from the old copy.
            fcntl.flock(held, fcntl.LOCK_SH)
            proc = w.tick()
        self.assertEqual(proc.returncode, 0, proc.stdout)
        self.assertIn("frozen copy of %s is in use" % old, proc.stdout)
        self.assertTrue(os.path.isdir(w.copy(old)))
        proc = w.tick()
        self.assertIn("pruned the frozen copy of %s" % old, proc.stdout)
        self.assertFalse(os.path.exists(w.copy(old)))

    @unittest.skipIf(os.geteuid() == 0, "root removes read-only directories")
    def test_a_copy_that_cannot_be_removed_is_logged_and_the_tick_goes_on(self):
        w, old, new = self.upgraded()
        stuck = os.path.join(w.copy(old), "lib")
        os.chmod(stuck, 0o555)
        self.addCleanup(lambda: os.path.isdir(stuck) and os.chmod(stuck, 0o755))
        proc = w.tick()
        self.assertEqual(proc.returncode, 0, proc.stdout)
        self.assertIn("could not remove the frozen copy of %s" % old, proc.stdout)
        self.assertIn("no eligible work; tick complete", proc.stdout)
        os.chmod(stuck, 0o755)
        proc = w.tick()
        self.assertIn("pruned the frozen copy of %s" % old, proc.stdout)


if __name__ == "__main__":
    unittest.main()
