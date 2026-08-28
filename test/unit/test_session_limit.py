"""Session-limit detection (#65).

The tick used to decide "did this agent hit its session limit?" with one exit
code (91) and one literal substring ("usage limit reached"). The claude CLI
emits neither today -- it says "You've hit your session limit · resets 9pm
(Europe/Stockholm)" -- so a limit hit was misread as partial progress and the
same story was relaunched until the tick's iteration budget ran out.

`lib/ralph_session.py` is the one place that decision is made, so both the tick
and the provider adapters read the same answer. These tests pin the wordings it
must recognise, the layered fallbacks that keep it working when the wording
changes again, and the false-positive guard that stops an agent *writing about*
session limits from checkpointing itself.
"""
import os
import subprocess
import sys
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
LIB_DIR = os.path.join(REPO_ROOT, "lib")
RALPH = os.path.join(REPO_ROOT, "bin", "ralph")

sys.path.insert(0, LIB_DIR)
import ralph_session  # noqa: E402

# The exact line observed from the claude CLI on 2026-08-27 that started the
# retry-storm this module exists to prevent.
OBSERVED = "You've hit your session limit · resets 9pm (Europe/Stockholm)"


class ClassifyTest(unittest.TestCase):
    def assertExhausted(self, exit_code, output, msg=None):
        self.assertEqual(ralph_session.SESSION_EXHAUSTED,
                         ralph_session.classify(exit_code, output), msg or output)

    def assertNormal(self, exit_code, output, msg=None):
        self.assertEqual(ralph_session.NORMAL,
                         ralph_session.classify(exit_code, output), msg or output)

    def test_current_claude_wording(self):
        # AC: the actual current CLI output classifies as session exhaustion,
        # even though its exit code is an ordinary failure code, not 91.
        self.assertExhausted(1, OBSERVED)

    def test_current_claude_wording_after_partial_transcript(self):
        # The limit can land mid-run: the notice terminates a transcript that
        # already has real output in it.
        self.assertExhausted(1, "working on #47\nediting lib/foo.py\n" + OBSERVED + "\n")

    def test_legacy_marker_still_detected(self):
        # AC: no regression on the old wording.
        self.assertExhausted(0, "Claude usage limit reached. Try again later.")

    def test_legacy_exit_code_still_detected(self):
        # AC: no regression on exit code 91, whatever the output says.
        self.assertExhausted(91, "")
        self.assertExhausted(91, "some unrelated output")

    def test_reworded_limit_notices(self):
        # AC: not hard-coded to one exact string. These are wordings the CLI
        # does not use today; detection must survive the phrasing changing.
        for text in ["You have hit your usage limit",
                     "Session limit reached · resets 9pm",
                     "You've reached your weekly limit · resets Monday",
                     "5-hour limit exceeded",
                     "Your quota is used up · resets at 21:00"]:
            self.assertExhausted(1, text)

    def test_normal_completion_is_not_exhaustion(self):
        self.assertNormal(0, "did the work\nRALPH-STORY-COMPLETE")
        self.assertNormal(0, "")

    def test_agent_prose_about_session_limits_is_not_exhaustion(self):
        # The tick greps the agent's own transcript. An iteration working *this*
        # story writes "session limit" repeatedly; that must not checkpoint it.
        transcript = ("Reading lib/ralph_session.py.\n"
                      "The bug: a session limit hit was misread, so the usage "
                      "limit reached marker never fired and the story was "
                      "relaunched.\n"
                      "Added a test for \"You've hit your session limit\".\n"
                      "Gate green.\n"
                      "RALPH-STORY-COMPLETE")
        self.assertNormal(0, transcript)

    def test_exit_code_override_accepts_a_list(self):
        # Robustness layer 1: the exit-code signal is a set, so a provider that
        # changes its code can be taught without a release.
        env = {"RALPH_SESSION_LIMIT_EXIT": "91,143"}
        self.assertEqual(ralph_session.SESSION_EXHAUSTED,
                         ralph_session.classify(143, "", env=env))
        self.assertEqual(ralph_session.SESSION_EXHAUSTED,
                         ralph_session.classify(91, "", env=env))
        self.assertEqual(ralph_session.NORMAL,
                         ralph_session.classify(7, "", env=env))

    def test_marker_override_adds_to_the_builtins(self):
        # Robustness layer 2: a superproject can widen the marker set, but an
        # override can never silently disable the shipped patterns -- that is
        # exactly how one stale literal became the only detector.
        env = {"RALPH_SESSION_LIMIT_MARKER": "out of tokens for now"}
        self.assertEqual(ralph_session.SESSION_EXHAUSTED,
                         ralph_session.classify(1, "out of tokens for now", env=env))
        self.assertEqual(ralph_session.SESSION_EXHAUSTED,
                         ralph_session.classify(1, OBSERVED, env=env))

    def test_bad_exit_override_falls_back_to_the_default(self):
        env = {"RALPH_SESSION_LIMIT_EXIT": "not-a-number"}
        self.assertEqual(ralph_session.SESSION_EXHAUSTED,
                         ralph_session.classify(91, "", env=env))


class ClassifyCliTest(unittest.TestCase):
    """`ralph --classify-session RC` -- the seam bin/ralph.sh reads, since bash
    cannot import Python. Output on stdin, verdict as the exit code."""

    def classify(self, rc, output, env=None):
        e = dict(os.environ)
        e.update(env or {})
        return subprocess.run([RALPH, "--classify-session", str(rc)], input=output,
                              env=e, stdout=subprocess.PIPE,
                              stderr=subprocess.STDOUT, text=True)

    def test_exhausted_exits_with_the_session_limit_code(self):
        proc = self.classify(1, OBSERVED)
        self.assertEqual(ralph_session.EXIT_SESSION_EXHAUSTED, proc.returncode, proc.stdout)

    def test_normal_exits_zero(self):
        proc = self.classify(0, "all good\nRALPH-STORY-COMPLETE")
        self.assertEqual(ralph_session.EXIT_NORMAL, proc.returncode, proc.stdout)

    def test_legacy_exit_code(self):
        proc = self.classify(91, "")
        self.assertEqual(ralph_session.EXIT_SESSION_EXHAUSTED, proc.returncode, proc.stdout)

    def test_missing_argument_is_a_usage_error(self):
        e = dict(os.environ)
        proc = subprocess.run([RALPH, "--classify-session"], input="", env=e,
                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        self.assertEqual(2, proc.returncode, proc.stdout)


if __name__ == "__main__":
    unittest.main()
