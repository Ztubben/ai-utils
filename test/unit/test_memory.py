"""Unit tests for two-tier memory: nested AGENTS.md learnings (US-010, ADR-0005).

Ralph keeps cross-iteration memory in exactly two stores split by durability:
durable **Learnings** in nested `AGENTS.md` files (read at story start, promoted to
the nearest module-local AGENTS.md before completion) and transient story-specific
notes on the issue (the Handoff). There is NO `progress.txt`.

The deterministic seams live in `lib/ralph_memory.py`:
  - `nested_agents_md(start_dir, root)` -- the AGENTS.md files Ralph reads at the
    start of a story, nearest-first (AC#1).
  - `promotion_target(changed_path, root)` -- the nearest AGENTS.md a reusable
    learning is promoted to, module-local rather than a single growing global file
    (AC#2, AC#3), never a progress.txt (AC#5).
  - `is_progress_txt` / `find_progress_txt` -- guard that Ralph creates/reads no
    progress.txt (AC#5).

The judgment-heavy memory discipline lives in the checked-in prompt
`prompts/memory.v1.md`, drift-guarded here.
"""
import os
import subprocess
import sys
import tempfile
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
LIB_DIR = os.path.join(REPO_ROOT, "lib")
RALPH = os.path.join(REPO_ROOT, "bin", "ralph")
PROMPT_V1 = os.path.join(REPO_ROOT, "prompts", "memory.v1.md")

sys.path.insert(0, LIB_DIR)
import ralph_memory  # noqa: E402


def _touch(path):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as fh:
        fh.write("# notes\n")


class NestedAgentsMd(unittest.TestCase):
    """AC#1: Ralph reads nearby nested AGENTS.md at the start of a story."""

    def _tree(self, tmp):
        # root/AGENTS.md, root/lib/AGENTS.md, root/lib/sub (no AGENTS.md).
        _touch(os.path.join(tmp, "AGENTS.md"))
        _touch(os.path.join(tmp, "lib", "AGENTS.md"))
        os.makedirs(os.path.join(tmp, "lib", "sub"), exist_ok=True)

    def test_collects_nearest_first_up_to_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._tree(tmp)
            got = ralph_memory.nested_agents_md(os.path.join(tmp, "lib", "sub"), tmp)
            self.assertEqual(
                got,
                [os.path.join(tmp, "lib", "AGENTS.md"),
                 os.path.join(tmp, "AGENTS.md")],
            )

    def test_includes_root_agents_md(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._tree(tmp)
            got = ralph_memory.nested_agents_md(tmp, tmp)
            self.assertEqual(got, [os.path.join(tmp, "AGENTS.md")])

    def test_empty_when_none_present(self):
        with tempfile.TemporaryDirectory() as tmp:
            os.makedirs(os.path.join(tmp, "lib"), exist_ok=True)
            self.assertEqual(ralph_memory.nested_agents_md(os.path.join(tmp, "lib"), tmp), [])

    def test_never_returns_progress_txt(self):
        with tempfile.TemporaryDirectory() as tmp:
            _touch(os.path.join(tmp, "AGENTS.md"))
            _touch(os.path.join(tmp, "progress.txt"))
            got = ralph_memory.nested_agents_md(tmp, tmp)
            self.assertTrue(all(os.path.basename(p) == "AGENTS.md" for p in got))


class PromotionTarget(unittest.TestCase):
    """AC#2/#3: a reusable learning is promoted to the NEAREST AGENTS.md, kept
    module-local rather than dumped into a single growing global file."""

    def test_prefers_nearest_module_local_over_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            _touch(os.path.join(tmp, "AGENTS.md"))
            _touch(os.path.join(tmp, "lib", "AGENTS.md"))
            target = ralph_memory.promotion_target(
                os.path.join(tmp, "lib", "sub", "foo.py"), tmp)
            self.assertEqual(target, os.path.join(tmp, "lib", "AGENTS.md"))

    def test_creates_module_local_when_none_exists(self):
        # No AGENTS.md anywhere (not even root): promote to a NEW module-local
        # file in the changed file's own directory, not the global root.
        with tempfile.TemporaryDirectory() as tmp:
            os.makedirs(os.path.join(tmp, "pkg"), exist_ok=True)
            target = ralph_memory.promotion_target(
                os.path.join(tmp, "pkg", "foo.py"), tmp)
            self.assertEqual(target, os.path.join(tmp, "pkg", "AGENTS.md"))
            self.assertNotEqual(target, os.path.join(tmp, "AGENTS.md"))

    def test_uses_own_dir_agents_md_when_present(self):
        with tempfile.TemporaryDirectory() as tmp:
            _touch(os.path.join(tmp, "AGENTS.md"))
            _touch(os.path.join(tmp, "lib", "AGENTS.md"))
            target = ralph_memory.promotion_target(
                os.path.join(tmp, "lib", "foo.py"), tmp)
            self.assertEqual(target, os.path.join(tmp, "lib", "AGENTS.md"))

    def test_accepts_a_directory_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            _touch(os.path.join(tmp, "lib", "AGENTS.md"))
            target = ralph_memory.promotion_target(os.path.join(tmp, "lib"), tmp)
            self.assertEqual(target, os.path.join(tmp, "lib", "AGENTS.md"))

    def test_target_is_always_agents_md_never_progress_txt(self):
        with tempfile.TemporaryDirectory() as tmp:
            os.makedirs(os.path.join(tmp, "pkg"), exist_ok=True)
            target = ralph_memory.promotion_target(
                os.path.join(tmp, "pkg", "foo.py"), tmp)
            self.assertEqual(os.path.basename(target), "AGENTS.md")
            self.assertFalse(ralph_memory.is_progress_txt(target))


class ProgressTxtGuard(unittest.TestCase):
    """AC#5: no progress.txt is created or read."""

    def test_is_progress_txt(self):
        self.assertTrue(ralph_memory.is_progress_txt("a/b/progress.txt"))
        self.assertTrue(ralph_memory.is_progress_txt("progress.txt"))
        self.assertFalse(ralph_memory.is_progress_txt("a/AGENTS.md"))
        self.assertFalse(ralph_memory.is_progress_txt("progress.md"))

    def test_find_progress_txt_reports_offenders(self):
        with tempfile.TemporaryDirectory() as tmp:
            _touch(os.path.join(tmp, "AGENTS.md"))
            _touch(os.path.join(tmp, "sub", "progress.txt"))
            hits = ralph_memory.find_progress_txt(tmp)
            self.assertEqual(hits, [os.path.join(tmp, "sub", "progress.txt")])

    def test_find_progress_txt_empty_for_clean_tree(self):
        with tempfile.TemporaryDirectory() as tmp:
            _touch(os.path.join(tmp, "AGENTS.md"))
            _touch(os.path.join(tmp, "lib", "AGENTS.md"))
            self.assertEqual(ralph_memory.find_progress_txt(tmp), [])


class Cli(unittest.TestCase):
    def _run(self, *args):
        return subprocess.run(
            [RALPH, *args], cwd=REPO_ROOT,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )

    def test_read_learnings_prints_nested_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            _touch(os.path.join(tmp, "AGENTS.md"))
            proc = self._run("--read-learnings", tmp)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertIn(os.path.join(tmp, "AGENTS.md"), proc.stdout)

    def test_read_learnings_missing_dir_exits_two(self):
        proc = self._run("--read-learnings", "/no/such/dir/here")
        self.assertEqual(proc.returncode, 2)

    def test_learn_target_prints_agents_md(self):
        with tempfile.TemporaryDirectory() as tmp:
            _touch(os.path.join(tmp, "AGENTS.md"))
            proc = self._run("--learn-target", os.path.join(tmp, "foo.py"))
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertIn("AGENTS.md", proc.stdout)
            self.assertNotIn("progress.txt", proc.stdout)

    def test_learn_target_requires_a_path(self):
        proc = self._run("--learn-target")
        self.assertEqual(proc.returncode, 2)


class MemoryPromptV1(unittest.TestCase):
    """The judgment-heavy two-tier memory discipline is a checked-in prompt."""

    def setUp(self):
        self.assertTrue(os.path.isfile(PROMPT_V1), "prompts/memory.v1.md must be checked in")
        with open(PROMPT_V1) as fh:
            self.text = fh.read()

    def test_covers_memory_directives(self):
        low = self.text.lower()
        for needle in ["learnings", "agents.md", "nearest", "module-local",
                       "lean", "progress.txt", "issue", "start"]:
            self.assertIn(needle, low, "memory.v1 prompt missing: %s" % needle)

    def test_uses_hil_terminology_not_hitl(self):
        self.assertNotIn("HITL", self.text)


if __name__ == "__main__":
    unittest.main()
