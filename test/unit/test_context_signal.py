"""The context-fill signal that tells an iteration when to hand off (ADR-0004).

A `--print` agent is asked to hand off before its context runs out but is never
told how full it is. The Claude implementation role therefore launches with a
`PostToolUse` hook that reads the session transcript and, past
`limits.handoff_context_tokens`, tells the agent to hand off. These tests drive
the pure reading, the hook entry point end to end (a real subprocess over a
real transcript file), and the launch command that installs it.
"""
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(REPO_ROOT, "lib"))

import ralph_agent  # noqa: E402
import ralph_config  # noqa: E402
import ralph_context  # noqa: E402

FIXTURES = os.path.join(REPO_ROOT, "test", "fixtures", "config", "valid")


def assistant(fresh, written, read, output):
    return json.dumps({"type": "assistant", "message": {"usage": {
        "input_tokens": fresh, "cache_creation_input_tokens": written,
        "cache_read_input_tokens": read, "output_tokens": output}}})


class ContextIsReadOffTheLatestRequest(unittest.TestCase):

    def test_sums_every_input_term_and_the_output_of_the_latest_request(self):
        lines = [assistant(2, 9000, 30000, 500),
                 json.dumps({"type": "user", "message": {"content": "tool result"}}),
                 assistant(1, 4000, 90000, 800)]
        self.assertEqual(ralph_context.context_tokens(lines), 94801)

    def test_a_transcript_with_no_request_reports_nothing(self):
        self.assertIsNone(ralph_context.context_tokens(
            ["not json", json.dumps({"type": "user"}), json.dumps([1])]))


class TheNoticeFiresOnlyPastTheThreshold(unittest.TestCase):

    def test_under_threshold_there_is_no_notice(self):
        self.assertIsNone(ralph_context.notice(149999, 150000))
        self.assertIsNone(ralph_context.notice(None, 150000))

    def test_past_threshold_the_agent_is_told_to_hand_off(self):
        answer = ralph_context.notice(150000, 150000)["hookSpecificOutput"]
        self.assertEqual(answer["hookEventName"], "PostToolUse")
        self.assertIn("Ralph context notice", answer["additionalContext"])
        self.assertIn("Handoff", answer["additionalContext"])


class TheHookRunsAsTheCliWouldRunIt(unittest.TestCase):

    def run_hook(self, transcript_lines, threshold, payload=None):
        with tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False) as fh:
            fh.write("\n".join(transcript_lines) + "\n")
        self.addCleanup(os.unlink, fh.name)
        stdin = json.dumps(payload if payload is not None
                           else {"hook_event_name": "PostToolUse",
                                 "transcript_path": fh.name})
        return subprocess.run(ralph_context.hook_command(threshold), shell=True,
                              input=stdin, capture_output=True, text=True)

    def test_past_threshold_the_hook_prints_the_notice(self):
        proc = self.run_hook([assistant(1, 1000, 200000, 10)], 150000)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("Ralph context notice", json.loads(proc.stdout)
                      ["hookSpecificOutput"]["additionalContext"])

    def test_under_threshold_the_hook_is_silent(self):
        proc = self.run_hook([assistant(1, 1000, 20000, 10)], 150000)
        self.assertEqual((proc.returncode, proc.stdout), (0, ""))

    def test_a_hook_that_cannot_read_its_input_never_fails_the_run(self):
        proc = self.run_hook([], 150000, payload={"transcript_path": "/nonexistent"})
        self.assertEqual((proc.returncode, proc.stdout), (0, ""))
        out = io.StringIO()
        self.assertEqual(ralph_context.main(["hook", "1"], io.StringIO("{{"), out), 0)
        self.assertEqual(out.getvalue(), "")


class OnlyTheClaudeImplementationRoleCarriesTheHook(unittest.TestCase):

    def config(self, name="minimal.yml"):
        result = ralph_config.load_and_validate(os.path.join(FIXTURES, name))
        assert result.ok, result.errors
        return result.config

    def test_the_threshold_defaults_from_the_schema(self):
        self.assertEqual(self.config()["limits"]["handoff_context_tokens"], 150000)

    def test_implementation_launch_installs_the_hook_at_the_configured_threshold(self):
        cfg = self.config()
        cfg["limits"]["handoff_context_tokens"] = 120000
        adapter, errors = ralph_agent.adapter_for_role(cfg, "implementation")
        self.assertEqual(errors, [])
        argv = adapter.argv()
        settings = json.loads(argv[argv.index("--settings") + 1])
        hook = settings["hooks"]["PostToolUse"][0]["hooks"][0]
        self.assertTrue(hook["command"].endswith("ralph_context.py hook 120000"))

    def test_review_launch_carries_no_hook(self):
        # Review runs without session persistence, so there is no transcript
        # to read -- and a reviewer has no Handoff to write.
        adapter, _ = ralph_agent.adapter_for_role(self.config(), "review")
        self.assertNotIn("--settings", adapter.argv())


if __name__ == "__main__":
    unittest.main()
