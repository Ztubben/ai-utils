"""Every Tick runs in its own Slot (#134, PRD #133, ADR-0008).

A **Slot** is a git worktree dedicated to one Loop, at a conventional sibling
path (`<repo>.ralph-slot-<N>`). The Tick provisions it on first use, repairs it
when it was deleted or is no longer a worktree of this clone, and does all of
its git work there -- so the operator keeps their own checkout while the Loop
runs, and their half-finished merge can no longer stop a Tick. The Tick lock is
per Slot and lives in the git common directory, where every worktree of one
clone agrees on it, and the Alternation phase moves there too.

The pure decisions (the Slot number, its path, its lock, what provisioning has
to do) are tested directly. The rest runs the real `bin/ralph.sh` on real git,
from the self-hosted world the frozen-tooling suite already builds: only real
git can show that the Slot is a worktree, that the operator's checkout came
through a Tick untouched, and that a mid-merge Slot and a mid-merge checkout
are told apart.
"""
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(REPO_ROOT, "lib"))

import ralph_alternation  # noqa: E402
import ralph_slot  # noqa: E402

from test_frozen_tooling import GIT_ENV, SelfHostedWorld, ready_story  # noqa: E402


class SlotNumber(unittest.TestCase):
    def test_a_tick_with_no_slot_in_its_environment_is_slot_one(self):
        self.assertEqual(ralph_slot.slot_number({}), "1")

    def test_the_scheduler_names_the_slot_in_the_environment(self):
        self.assertEqual(ralph_slot.slot_number({"RALPH_SLOT": "2"}), "2")

    def test_an_empty_slot_in_the_environment_is_an_unset_one(self):
        # A scheduler template whose instance did not expand must still tick.
        self.assertEqual(ralph_slot.slot_number({"RALPH_SLOT": ""}), "1")

    def test_a_slot_number_that_could_escape_the_path_is_refused(self):
        for value in ("..", "../evil", "a/b", " ", "-x", "/1", "a b"):
            self.assertIsNone(ralph_slot.slot_number({"RALPH_SLOT": value}), value)


class SlotPaths(unittest.TestCase):
    def test_the_slot_is_a_sibling_of_the_repository_named_by_its_number(self):
        self.assertEqual(ralph_slot.slot_dir("/w/acme", "1"), "/w/acme.ralph-slot-1")
        self.assertEqual(ralph_slot.slot_dir("/w/acme", "2"), "/w/acme.ralph-slot-2")

    def test_the_lock_lives_in_the_git_common_directory_per_slot(self):
        self.assertEqual(ralph_slot.lock_file("/w/acme/.git", "1"),
                         "/w/acme/.git/ralph-tick-slot-1.lock")
        self.assertNotEqual(ralph_slot.lock_file("/w/acme/.git", "1"),
                            ralph_slot.lock_file("/w/acme/.git", "2"))


class ProvisioningAction(unittest.TestCase):
    def test_a_missing_slot_is_created(self):
        self.assertEqual(ralph_slot.action(False, False), ralph_slot.CREATED)

    def test_a_slot_that_is_a_worktree_of_this_clone_is_reused(self):
        self.assertEqual(ralph_slot.action(True, True), ralph_slot.REUSED)

    def test_a_directory_that_is_no_longer_our_worktree_is_repaired(self):
        self.assertEqual(ralph_slot.action(True, False), ralph_slot.REPAIRED)


class SlotWorld(SelfHostedWorld):
    """The self-hosted world, plus what a Slot test needs to look at."""

    def slot(self, number="1"):
        return self.checkout + ".ralph-slot-" + number

    def common(self):
        return os.path.join(self.checkout, ".git")

    def lock(self, number="1"):
        return os.path.join(self.common(), "ralph-tick-slot-%s.lock" % number)

    def worktrees(self):
        out = self.git("worktree", "list", "--porcelain")
        return [ln.split(" ", 1)[1] for ln in out.splitlines()
                if ln.startswith("worktree ")]

    def git_dir_of(self, path):
        return self.git("rev-parse", "--absolute-git-dir", cwd=path)

    def head_of(self, path):
        return self.git("rev-parse", "HEAD", cwd=path)

    def branch_of(self, path):
        proc = subprocess.run(["git", "symbolic-ref", "-q", "--short", "HEAD"],
                              cwd=path, env=dict(os.environ, **GIT_ENV),
                              stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                              text=True)
        return proc.stdout.strip() if proc.returncode == 0 else None

    def tree_of(self, path):
        """Everything the working tree at *path* holds, content and all."""
        seen = {}
        for root, dirs, files in os.walk(path):
            dirs[:] = [d for d in dirs if d not in (".git", "__pycache__")]
            for name in files:
                full = os.path.join(root, name)
                with open(full, "rb") as fh:
                    seen[os.path.relpath(full, path)] = fh.read()
        return seen


class SlotTest(unittest.TestCase):
    def world(self):
        tmp = tempfile.mkdtemp()
        w = SlotWorld(tmp)
        # The Slot is a *sibling* of the checkout, so it is not under tmp/.
        self.addCleanup(lambda: shutil.rmtree(tmp, ignore_errors=True))
        self.addCleanup(lambda: shutil.rmtree(w.slot(), ignore_errors=True))
        return w

    def ticked(self, w, **overrides):
        proc = w.tick(**overrides)
        self.assertEqual(proc.returncode, 0, proc.stdout)
        return proc


class TheTickProvisionsItsSlot(SlotTest):
    def test_a_first_tick_creates_the_slot_as_a_worktree(self):
        w = self.world()
        self.assertFalse(os.path.exists(w.slot()))
        proc = self.ticked(w)
        self.assertIn("slot 1: created %s" % w.slot(), proc.stdout)
        self.assertIn(w.slot(), w.worktrees())
        self.assertTrue(os.path.isfile(os.path.join(w.slot(), ".ralph.yml")))

    def test_a_later_tick_reuses_the_slot(self):
        w = self.world()
        self.ticked(w)
        created = w.git_dir_of(w.slot())
        proc = self.ticked(w)
        self.assertIn("slot 1: reused %s" % w.slot(), proc.stdout)
        self.assertEqual(w.git_dir_of(w.slot()), created)
        self.assertEqual(w.worktrees().count(w.slot()), 1)

    def test_a_tick_after_the_slot_directory_was_deleted_recreates_it(self):
        w = self.world()
        self.ticked(w)
        shutil.rmtree(w.slot())
        proc = self.ticked(w)
        self.assertRegex(proc.stdout, r"slot 1: (created|repaired) %s" % w.slot())
        self.assertIn(w.slot(), w.worktrees())
        self.assertTrue(os.path.isfile(os.path.join(w.slot(), ".ralph.yml")))

    def test_a_tick_after_the_worktree_metadata_was_pruned_recreates_it(self):
        w = self.world()
        self.ticked(w)
        # The directory survives but the clone no longer knows it: what a
        # `git worktree prune` against a temporarily unmounted Slot leaves.
        shutil.rmtree(os.path.join(w.common(), "worktrees"))
        self.assertNotIn(w.slot(), w.worktrees())
        proc = self.ticked(w)
        self.assertIn("slot 1: repaired %s" % w.slot(), proc.stdout)
        self.assertIn(w.slot(), w.worktrees())

    def test_a_second_slot_gets_its_own_worktree_and_lock(self):
        w = self.world()
        self.addCleanup(lambda: shutil.rmtree(w.slot("2"), ignore_errors=True))
        self.ticked(w)
        proc = self.ticked(w, RALPH_SLOT="2")
        self.assertIn("slot 2: created %s" % w.slot("2"), proc.stdout)
        self.assertIn(w.slot(), w.worktrees())
        self.assertIn(w.slot("2"), w.worktrees())
        self.assertTrue(os.path.exists(w.lock("2")))


class TheOperatorsCheckoutIsLeftAlone(SlotTest):
    def test_a_tick_leaves_the_checkouts_branch_head_and_working_tree(self):
        w = self.world()
        w.set_backlog([ready_story(7)])
        w.git("checkout", "-q", "-b", "operator/grilling")
        with open(os.path.join(w.checkout, "CONTEXT.md"), "w") as fh:
            fh.write("a grilling session in progress\n")
        # Even the config the operator is editing stays broken in their tree.
        with open(os.path.join(w.checkout, ".ralph.yml"), "a") as fh:
            fh.write("\n<<<<<<< HEAD\nnot: yaml: at: all\n")
        before = (w.branch_of(w.checkout), w.head_of(w.checkout),
                  w.tree_of(w.checkout))
        proc = self.ticked(w)
        self.assertEqual((w.branch_of(w.checkout), w.head_of(w.checkout),
                          w.tree_of(w.checkout)), before, proc.stdout)
        # The story work happened, and it happened in the Slot.
        self.assertIn("ralph/7-s7", w.git("branch", "--list", "ralph/7-s7"))
        self.assertEqual(w.branch_of(w.slot()), "ralph/7-s7")

    def test_a_mid_merge_checkout_no_longer_stops_a_tick(self):
        w = self.world()
        w.set_backlog([ready_story(7)])
        with open(os.path.join(w.common(), "MERGE_HEAD"), "w") as fh:
            fh.write(w.head_of(w.checkout) + "\n")
        proc = self.ticked(w)
        self.assertNotIn("refusing to tick", proc.stdout)
        self.assertIn("start #7", proc.stdout)

    def test_a_mid_merge_slot_is_still_refused_naming_the_cause(self):
        w = self.world()
        self.ticked(w)
        with open(os.path.join(w.git_dir_of(w.slot()), "MERGE_HEAD"), "w") as fh:
            fh.write(w.head_of(w.slot()) + "\n")
        proc = w.tick()
        self.assertEqual(proc.returncode, 2, proc.stdout)
        self.assertIn("mid-merge", proc.stdout)
        self.assertIn("refusing to tick", proc.stdout)
        self.assertIn(w.slot(), proc.stdout)


class TheTickLockIsPerSlot(SlotTest):
    def test_the_lock_file_is_in_the_git_common_directory(self):
        w = self.world()
        self.ticked(w)
        self.assertTrue(os.path.exists(w.lock()), os.listdir(w.common()))

    def test_an_overlapping_tick_in_the_same_slot_exits_immediately(self):
        w = self.world()
        self.ticked(w)
        w.set_backlog([ready_story(7)])
        held = open(w.lock(), "w")
        self.addCleanup(held.close)
        import fcntl
        fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
        proc = w.tick()
        self.assertEqual(proc.returncode, 0, proc.stdout)
        self.assertIn("already running", proc.stdout)
        self.assertIn("slot 1", proc.stdout)
        self.assertNotIn("start #7", proc.stdout)


class TheSlotStartsDetached(SlotTest):
    def test_the_slot_is_detached_at_the_start_of_each_tick(self):
        w = self.world()
        w.set_backlog([ready_story(7)])
        self.ticked(w)
        # The previous tick left the Slot on the Story's branch.
        self.assertEqual(w.branch_of(w.slot()), "ralph/7-s7")
        w.set_backlog([])
        proc = self.ticked(w)
        self.assertIn("slot 1: detached", proc.stdout)
        self.assertIsNone(w.branch_of(w.slot()))

    def test_a_branch_a_crashed_tick_held_can_be_checked_out_elsewhere(self):
        w = self.world()
        w.set_backlog([ready_story(7)])
        self.ticked(w)
        w.set_backlog([])
        self.ticked(w)
        # Detached, the Slot holds no branch: the operator can take it.
        w.git("checkout", "-q", "ralph/7-s7")
        self.assertEqual(w.branch_of(w.checkout), "ralph/7-s7")


class AlternationIsSharedByTheSlotsOfOneClone(unittest.TestCase):
    def repo(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(lambda: shutil.rmtree(tmp, ignore_errors=True))
        root = os.path.join(tmp, "repo")
        os.makedirs(root)
        env = dict(os.environ, **GIT_ENV)
        for args in (["init", "-q", "-b", "main"],
                     ["commit", "-q", "--allow-empty", "-m", "init"]):
            subprocess.run(["git"] + args, cwd=root, env=env, check=True,
                           stdout=subprocess.DEVNULL)
        worktree = os.path.join(tmp, "repo.ralph-slot-1")
        subprocess.run(["git", "worktree", "add", "-q", "--detach", worktree],
                       cwd=root, env=env, check=True, stdout=subprocess.DEVNULL)
        return root, worktree

    def test_the_phase_is_read_and_written_in_the_git_common_directory(self):
        root, worktree = self.repo()
        path = ralph_alternation.state_path(worktree)
        self.assertEqual(os.path.realpath(os.path.dirname(os.path.dirname(path))),
                         os.path.realpath(os.path.join(root, ".git")))

    def test_two_worktrees_of_one_clone_see_one_phase(self):
        root, worktree = self.repo()
        from_main = ralph_alternation.state_path(root)
        from_slot = ralph_alternation.state_path(worktree)
        self.assertTrue(ralph_alternation.write_phase(from_slot, 3))
        self.assertEqual(ralph_alternation.read_phase(from_main), 3)
        self.assertEqual(os.path.realpath(from_main), os.path.realpath(from_slot))


if __name__ == "__main__":
    unittest.main()
