"""Contract tests for the per-Story token ledger and usage footers (#63).

The canonical record of what a Story cost is one machine-managed comment on the
Story: a table a person can read and a versioned payload a script can aggregate,
updated in place as invocations accumulate rather than posted again and again.
The pull request carries only footers -- each agent response showing what that
one invocation cost -- so the immediate signal is visible without opening the
ledger.
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

sys.path.insert(0, LIB_DIR)
import ralph_ledger  # noqa: E402
import ralph_review_render  # noqa: E402
import ralph_review_respond  # noqa: E402
import ralph_usage  # noqa: E402

HEAD = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"

CODEX_USAGE = {"input_tokens": 12808, "cached_input_tokens": 9984,
               "output_tokens": 5, "reasoning_output_tokens": 0}
CLAUDE_USAGE = {"input_tokens": 2, "cache_creation_input_tokens": 8601,
                "cache_read_input_tokens": 8121, "output_tokens": 40,
                "output_tokens_details": {"thinking_tokens": 12}}


def event(provider="codex", model="gpt-5-codex", role="review",
          phase=ralph_usage.REVIEW, round_no=1, head=HEAD, raw=None):
    return ralph_usage.invocation_event(
        ralph_usage.normalize(provider, CODEX_USAGE if raw is None else raw),
        role=role, phase=phase, model=model, provider=provider, story=63,
        pull_request=70, round_no=round_no, head=head, run="tick-1",
        when="2026-08-28T09:00:00Z")


class TheLedgerCommentCarriesBothViews(unittest.TestCase):
    def setUp(self):
        self.body = ralph_ledger.ledger_body(63, [event()])

    def test_a_person_can_read_the_table(self):
        self.assertIn("| 1 | review | codex | gpt-5-codex |", self.body)
        self.assertIn("12,808", self.body)

    def test_a_script_can_read_the_payload(self):
        payload = ralph_ledger.parse_payload(self.body)

        self.assertEqual(payload["version"], ralph_ledger.CONTRACT_VERSION)
        self.assertEqual(payload["story"], 63)
        self.assertEqual(len(payload["events"]), 1)

    def test_the_payload_records_role_round_provider_model_and_head(self):
        recorded = ralph_ledger.parse_payload(self.body)["events"][0]

        self.assertEqual(recorded["role"], "review")
        self.assertEqual(recorded["round"], 1)
        self.assertEqual(recorded["provider"], "codex")
        self.assertEqual(recorded["model"], "gpt-5-codex")
        self.assertEqual(recorded["head"], HEAD)

    def test_it_is_findable_by_a_marker_so_it_can_be_updated(self):
        self.assertIn(ralph_ledger.LEDGER_MARKER, self.body)


class UnavailableStaysUnavailableInBothViews(unittest.TestCase):
    """AC: a category the provider never reported is never shown as a number."""

    def setUp(self):
        self.body = ralph_ledger.ledger_body(63, [event()])

    def test_the_table_shows_a_gap_not_a_zero(self):
        row = [line for line in self.body.splitlines()
               if line.startswith("| 1 |")][0]
        cells = [cell.strip() for cell in row.strip("|").split("|")]

        self.assertIn(ralph_ledger.UNAVAILABLE_CELL, cells)
        self.assertIn("0", cells, "a reported zero is still shown as zero")

    def test_the_payload_keeps_the_availability_of_every_category(self):
        recorded = ralph_ledger.parse_payload(self.body)["events"][0]

        self.assertIsNone(recorded["usage"]["total"])
        self.assertEqual(recorded["availability"]["total"],
                         ralph_usage.UNAVAILABLE)
        self.assertEqual(recorded["availability"]["reasoning"],
                         ralph_usage.REPORTED)


def ledger_comment(events, ident="123456"):
    return {"id": "IC_kwDOabc", "url":
            "https://github.com/o/r/issues/63#issuecomment-%s" % ident,
            "body": ralph_ledger.ledger_body(63, events)}


class OneLedgerPerStoryUpdatedInPlace(unittest.TestCase):
    """AC: created once, then edited -- never a second comment."""

    def test_the_first_invocation_creates_the_comment(self):
        plan = ralph_ledger.ledger_plan(63, [], event())

        self.assertTrue(plan.ok, plan.errors)
        self.assertTrue(plan.created)
        self.assertEqual(plan.commands[0][:4],
                         ["gh", "issue", "comment", "63"])

    def test_a_later_invocation_edits_that_same_comment(self):
        first = event()
        plan = ralph_ledger.ledger_plan(
            63, [ledger_comment([first])],
            event(provider="claude", model="claude-opus-5",
                  phase=ralph_usage.RESPONSE, role="implementation",
                  raw=CLAUDE_USAGE))

        self.assertTrue(plan.ok, plan.errors)
        self.assertFalse(plan.created)
        self.assertEqual(len(plan.commands), 1)
        self.assertEqual(plan.commands[0][:4],
                         ["gh", "api", "--method", "PATCH"])
        self.assertIn("issues/comments/123456", plan.commands[0][4])

    def test_the_edit_keeps_every_earlier_reading(self):
        first = event()
        plan = ralph_ledger.ledger_plan(
            63, [ledger_comment([first])],
            event(round_no=2, provider="claude", model="claude-opus-5",
                  raw=CLAUDE_USAGE))

        body = plan.commands[0][-1]
        payload = ralph_ledger.parse_payload(body)
        self.assertEqual([e["round"] for e in payload["events"]], [1, 2])
        self.assertEqual([e["provider"] for e in payload["events"]],
                         ["codex", "claude"])

    def test_other_story_comments_are_never_mistaken_for_the_ledger(self):
        # Handoffs, Attempts, review results and responses all live here too.
        unrelated = [{"body": "Handoff: picking this up next tick"},
                     {"body": "<!-- ralph-review-result:v1 head=x round=1 -->"}]

        plan = ralph_ledger.ledger_plan(63, unrelated, event())

        self.assertTrue(plan.created)

    def test_a_ledger_with_no_addressable_id_is_reported_not_duplicated(self):
        # Two comments both claiming to be canonical is the worse outcome.
        headless = dict(ledger_comment([event()]))
        headless.pop("url")

        plan = ralph_ledger.ledger_plan(63, [headless], event(round_no=2))

        self.assertFalse(plan.ok)
        self.assertEqual(plan.commands, [])


class ALedgerThatOutgrowsOneCommentSaysSo(unittest.TestCase):
    """A Story can run more invocations than one GitHub comment will hold."""

    def setUp(self):
        self.body = ralph_ledger.ledger_body(63, [event()] * 400)

    def test_it_stays_within_what_github_will_accept(self):
        self.assertLessEqual(len(self.body), ralph_ledger.MAX_BODY)

    def test_it_keeps_the_most_recent_readings(self):
        payload = ralph_ledger.parse_payload(self.body)

        self.assertGreater(len(payload["events"]), 0)
        self.assertLess(len(payload["events"]), 400)

    def test_the_readings_it_dropped_are_stated_not_hidden(self):
        # Silently posting fewer rows would make the ledger quietly wrong,
        # which is the one thing a record of what is known must not be.
        payload = ralph_ledger.parse_payload(self.body)

        self.assertEqual(payload["dropped"],
                         400 - len(payload["events"]))
        self.assertIn("dropped", self.body)


REVIEW_FIXTURES = os.path.join(REPO_ROOT, "test", "fixtures", "reviews")


def review_fixture(name):
    with open(os.path.join(REVIEW_FIXTURES, name)) as fh:
        return json.load(fh)


class EachAgentResponseCarriesItsOwnFooter(unittest.TestCase):
    """AC: the immediate cost signal, without opening the ledger."""

    def test_the_footer_reports_this_invocation_only(self):
        footer = ralph_ledger.usage_footer(event(round_no=2))

        self.assertIn("gpt-5-codex", footer)
        self.assertIn("12,808", footer)
        self.assertIn("round 2", footer)
        self.assertEqual(len(footer.splitlines()), 1)

    def test_an_unavailable_category_stays_unavailable_in_the_footer(self):
        footer = ralph_ledger.usage_footer(event())

        self.assertIn(ralph_usage.UNAVAILABLE, footer)
        self.assertNotIn("total 0", footer)

    def test_a_published_review_carries_the_reviewer_s_footer(self):
        body = ralph_review_render.review_body(
            review_fixture("valid-inline.json"), usage_event=event())

        self.assertIn("gpt-5-codex", body)
        self.assertIn("12,808", body)

    def test_an_implementation_answer_carries_its_own_footer(self):
        answer = {"contract": ralph_review_respond.CONTRACT_VERSION,
                  "head": HEAD, "round": 1, "model": "claude-opus-5",
                  "summary": "Fixed the one that mattered.",
                  "dispositions": [{"id": "F-1", "disposition": "accepted",
                                    "note": "Added the missing guard.",
                                    "evidence": "lib/x.py:10"}]}

        body = ralph_review_respond.response_comment(
            answer, "b" * 40,
            usage_event=event(provider="claude", model="claude-opus-5",
                              phase=ralph_usage.RESPONSE,
                              role="implementation", raw=CLAUDE_USAGE))

        self.assertIn("claude-opus-5", body)
        self.assertIn("16,724", body)

    def test_a_response_with_no_reading_reads_exactly_as_it_used_to(self):
        # A provider that reported nothing must not leave an empty footer
        # hanging off the response.
        answer = {"contract": ralph_review_respond.CONTRACT_VERSION,
                  "head": HEAD, "round": 1, "model": "claude-opus-5",
                  "summary": "Fixed it.", "dispositions": []}

        self.assertEqual(
            ralph_review_respond.response_comment(answer, "b" * 40),
            ralph_review_respond.response_comment(answer, "b" * 40,
                                                  usage_event=None))


class CliLedgerAgainstMockedGh(unittest.TestCase):
    """The implementation iteration is launched straight from the tick, so its
    row reaches the Story the same way a round's does."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = self.tmp.name
        self.log = os.path.join(self.root, "calls.log")

    def _write(self, name, text, executable=False):
        path = os.path.join(self.root, name)
        with open(path, "w") as fh:
            fh.write(text)
        if executable:
            os.chmod(path, os.stat(path).st_mode | stat.S_IEXEC)
        return path

    def _gh(self, comments):
        self._write("comments.json", json.dumps({"comments": comments}))
        self._write("gh", '#!/usr/bin/env bash\n'
                          'echo "gh $*" >> "$RALPH_LOG"\n'
                          'if [[ "$1 $2" == "issue view" ]]; then '
                          'cat "%s/comments.json"; fi\nexit 0\n' % self.root,
                    executable=True)

    def _provider(self, output):
        self._write("claude", '#!/usr/bin/env bash\ncat >/dev/null\n'
                              'cat <<\'OUT\'\n%s\nOUT\n' % output,
                    executable=True)

    def run_launch(self):
        env = dict(os.environ, PATH=self.root + os.pathsep + os.environ["PATH"],
                   RALPH_LOG=self.log, RALPH_RUN_ID="tick-under-test")
        for name in ("RALPH_CLAUDE", "RALPH_CODEX"):
            env.pop(name, None)
        story = self._write("story.json", json.dumps(
            {"number": 63, "title": "Ledger", "body": "",
             "labels": [{"name": "state:in-progress"}, {"name": "type:afk"},
                        {"name": "model:impl:claude-opus-5"},
                        {"name": "model:review:gpt-5-codex"}]}))
        proc = subprocess.run(
            [os.path.join(REPO_ROOT, "bin", "ralph"), "--launch-agent",
             "implementation",
             os.path.join(REPO_ROOT, "test", "fixtures", "config", "valid",
                          "models.yml"),
             "--story", story],
            cwd=self.root, env=env, input="a prompt",
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        calls = ""
        if os.path.exists(self.log):
            with open(self.log) as fh:
                calls = fh.read()
        return proc, calls

    def test_the_first_implementation_invocation_creates_the_ledger(self):
        self._gh([])
        self._provider(json.dumps({"result": "worked on it",
                                   "usage": CLAUDE_USAGE}))

        proc, calls = self.run_launch()

        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("gh issue comment 63", calls)
        self.assertIn(ralph_ledger.LEDGER_MARKER, calls)
        self.assertIn("implementation", calls)

    def test_a_later_invocation_edits_the_ledger_it_already_has(self):
        self._gh([{"url": "https://github.com/o/r/issues/63#issuecomment-99",
                   "body": ralph_ledger.ledger_body(63, [event()])}])
        self._provider(json.dumps({"result": "worked on it",
                                   "usage": CLAUDE_USAGE}))

        proc, calls = self.run_launch()

        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertNotIn("gh issue comment 63", calls)
        self.assertIn("issues/comments/99", calls)

    def test_a_ledger_that_cannot_be_written_never_fails_the_run(self):
        # Telemetry: losing a row must not cost the invocation it describes.
        self._write("gh", '#!/usr/bin/env bash\necho "gh $*" >> "$RALPH_LOG"\n'
                          'exit 1\n', executable=True)
        self._provider(json.dumps({"result": "worked on it",
                                   "usage": CLAUDE_USAGE}))

        proc, _ = self.run_launch()

        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("worked on it", proc.stdout)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
