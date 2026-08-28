"""Contract tests for normalized provider-reported token usage (#62, PRD #42).

Every implementation and review invocation reports the token categories its
provider actually supplies, in one shape, so usage can be compared across
providers later.  A category a provider does not expose is recorded as
unavailable -- never estimated, never zero-filled -- so later statistics can
tell reported data from a guess.  Accounting is telemetry: nothing it records
may block or alter an invocation.
"""
import io
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

sys.path.insert(0, LIB_DIR)
import ralph_agent  # noqa: E402
import ralph_review  # noqa: E402
import ralph_review_round  # noqa: E402
import ralph_usage  # noqa: E402

# The `usage` object `claude --print --output-format json` reports, trimmed to
# the keys that carry token counts.
CLAUDE_USAGE = {
    "input_tokens": 2,
    "cache_creation_input_tokens": 8601,
    "cache_read_input_tokens": 8121,
    "output_tokens": 40,
    "output_tokens_details": {"thinking_tokens": 12},
}

# The `usage` object on the `turn.completed` event of `codex exec --json`.
CODEX_USAGE = {
    "input_tokens": 12808,
    "cached_input_tokens": 9984,
    "cache_write_input_tokens": 0,
    "output_tokens": 5,
    "reasoning_output_tokens": 0,
}


class NormalizesWhatClaudeReports(unittest.TestCase):
    def setUp(self):
        self.usage = ralph_usage.normalize("claude", CLAUDE_USAGE)

    def test_input_is_everything_that_was_sent_cached_or_not(self):
        # Claude counts cache reads and cache writes apart from `input_tokens`,
        # so the normalized total input is the three of them together. That is
        # arithmetic over reported numbers, not an estimate of a missing one.
        self.assertEqual(self.usage["input"], 2 + 8601 + 8121)

    def test_cached_input_is_the_part_served_from_cache(self):
        self.assertEqual(self.usage["cached_input"], 8121)

    def test_output_and_reasoning_are_reported_as_given(self):
        self.assertEqual(self.usage["output"], 40)
        self.assertEqual(self.usage["reasoning"], 12)

    def test_a_total_it_does_not_report_stays_unavailable(self):
        self.assertIsNone(self.usage["total"])
        self.assertFalse(self.usage.available("total"))
        self.assertEqual(self.usage.payload()["availability"]["total"],
                         ralph_usage.UNAVAILABLE)


class NormalizesWhatCodexReports(unittest.TestCase):
    def setUp(self):
        self.usage = ralph_usage.normalize("codex", CODEX_USAGE)

    def test_input_already_counts_the_cached_part(self):
        # Codex reports input inclusive of the cache hit, so normalizing it is
        # taking it as it stands -- the opposite of the Claude case, which is
        # exactly the difference this module exists to absorb.
        self.assertEqual(self.usage["input"], 12808)
        self.assertEqual(self.usage["cached_input"], 9984)

    def test_output_and_reasoning_are_reported_as_given(self):
        self.assertEqual(self.usage["output"], 5)
        self.assertEqual(self.usage["reasoning"], 0)

    def test_a_reported_zero_is_a_reading_not_a_gap(self):
        # The whole reason availability rides alongside the value: a model that
        # did no reasoning and a provider that does not count reasoning are
        # different facts, and a bare 0 cannot tell them apart.
        self.assertTrue(self.usage.available("reasoning"))
        self.assertEqual(self.usage.payload()["availability"]["reasoning"],
                         ralph_usage.REPORTED)

    def test_a_total_it_does_not_report_stays_unavailable(self):
        self.assertIsNone(self.usage["total"])


class NeverInventsANumber(unittest.TestCase):
    """A category is reported only when the provider actually reported it."""

    def test_a_provider_that_reported_nothing_reports_nothing(self):
        usage = ralph_usage.normalize("claude", None)

        for category in ralph_usage.CATEGORIES:
            self.assertIsNone(usage[category])
            self.assertEqual(usage.status(category), ralph_usage.UNAVAILABLE)

    def test_a_category_missing_one_of_its_terms_is_unavailable_whole(self):
        # A partial sum is precisely the invented number this refuses to make.
        partial = dict(CLAUDE_USAGE)
        del partial["cache_read_input_tokens"]

        usage = ralph_usage.normalize("claude", partial)

        self.assertIsNone(usage["input"])
        self.assertIsNone(usage["cached_input"])
        self.assertEqual(usage["output"], 40)

    def test_an_unknown_provider_is_all_unavailable_not_an_error(self):
        # Accounting is telemetry: it may know nothing, but it may not fail.
        usage = ralph_usage.normalize("some-future-provider", {"tokens": 5})

        self.assertFalse(any(usage.available(c)
                             for c in ralph_usage.CATEGORIES))

    def test_a_non_numeric_reading_is_not_a_reading(self):
        usage = ralph_usage.normalize("codex", dict(CODEX_USAGE,
                                                    output_tokens="lots"))

        self.assertIsNone(usage["output"])
        self.assertEqual(usage["input"], 12808)


class OneEventPerInvocation(unittest.TestCase):
    """Everything a later script needs to aggregate by Story, role and round."""

    def event(self, **kwargs):
        fields = dict(
            usage=ralph_usage.normalize("codex", CODEX_USAGE), role="review",
            phase=ralph_usage.REVIEW, model="gpt-5-codex", provider="codex",
            story=62, pull_request=70, round_no=2, head="a" * 40,
            run="tick-1", when="2026-08-28T09:00:00Z")
        fields.update(kwargs)
        return ralph_usage.invocation_event(**fields)

    def test_the_event_is_versioned_and_carries_the_whole_context(self):
        event = self.event()

        self.assertEqual(event["version"], ralph_usage.CONTRACT_VERSION)
        self.assertEqual(event["story"], 62)
        self.assertEqual(event["pull_request"], 70)
        self.assertEqual(event["role"], "review")
        self.assertEqual(event["phase"], ralph_usage.REVIEW)
        self.assertEqual(event["provider"], "codex")
        self.assertEqual(event["model"], "gpt-5-codex")
        self.assertEqual(event["round"], 2)
        self.assertEqual(event["head"], "a" * 40)
        self.assertEqual(event["time"], "2026-08-28T09:00:00Z")
        self.assertEqual(event["run"], "tick-1")

    def test_availability_travels_with_the_counts(self):
        event = self.event()

        self.assertEqual(event["usage"]["output"], 5)
        self.assertEqual(event["availability"]["total"],
                         ralph_usage.UNAVAILABLE)
        self.assertIsNone(event["usage"]["total"])

    def test_the_run_identity_groups_one_tick_s_invocations(self):
        os.environ["RALPH_RUN_ID"] = "tick-2026-08-28T09"
        self.addCleanup(os.environ.pop, "RALPH_RUN_ID", None)

        self.assertEqual(ralph_usage.run_identity(), "tick-2026-08-28T09")
        self.assertEqual(self.event(run=None)["run"], "tick-2026-08-28T09")


CLAUDE_ENVELOPE = json.dumps({
    "type": "result", "subtype": "success", "is_error": False,
    "result": "The review, in the reviewer's own words.\n",
    "session_id": "abc", "usage": CLAUDE_USAGE})

CODEX_EVENTS = "\n".join([
    json.dumps({"type": "thread.started", "thread_id": "t1"}),
    json.dumps({"type": "turn.started"}),
    json.dumps({"type": "item.completed",
                "item": {"id": "item_0", "type": "agent_message",
                         "text": "The review, in the reviewer's own words."}}),
    json.dumps({"type": "turn.completed", "usage": CODEX_USAGE}),
])


class AdaptersReportProviderUsage(unittest.TestCase):
    """Each adapter knows its provider's machine-readable output; nothing else
    has to. Callers keep seeing prose, and now also get the usage."""

    def launch(self, adapter, output, exit_code=0):
        seen = {}

        def run(argv, prompt, env):
            seen["argv"] = argv
            return exit_code, output

        return adapter.launch("a prompt", run=run), seen["argv"]

    def test_claude_asks_for_the_envelope_that_carries_usage(self):
        _, argv = self.launch(ralph_agent.ClaudeAdapter(model="claude-opus-5"),
                              CLAUDE_ENVELOPE)

        self.assertIn("--output-format", argv)
        self.assertEqual(argv[argv.index("--output-format") + 1], "json")

    def test_the_caller_still_receives_only_the_agent_s_own_words(self):
        outcome, _ = self.launch(ralph_agent.ClaudeAdapter(), CLAUDE_ENVELOPE)

        self.assertEqual(outcome.output,
                         "The review, in the reviewer's own words.\n")

    def test_claude_usage_arrives_normalized(self):
        outcome, _ = self.launch(ralph_agent.ClaudeAdapter(), CLAUDE_ENVELOPE)

        self.assertEqual(outcome.usage["output"], 40)
        self.assertEqual(outcome.usage["cached_input"], 8121)
        self.assertFalse(outcome.usage.available("total"))

    def test_codex_asks_for_its_event_stream(self):
        _, argv = self.launch(ralph_agent.CodexAdapter(model="gpt-5-codex"),
                              CODEX_EVENTS)

        self.assertIn("--json", argv)

    def test_codex_events_become_the_agent_s_words_and_its_usage(self):
        outcome, _ = self.launch(ralph_agent.CodexAdapter(), CODEX_EVENTS)

        self.assertEqual(outcome.output,
                         "The review, in the reviewer's own words.")
        self.assertEqual(outcome.usage["input"], 12808)

    def test_output_that_is_not_the_expected_shape_passes_through_whole(self):
        # An older provider CLI, an error written before the stream starts, a
        # mock in a test harness: the run still counts, it just reports no
        # usage. Accounting may know nothing; it may never swallow output.
        for adapter in (ralph_agent.ClaudeAdapter(), ralph_agent.CodexAdapter()):
            outcome, _ = self.launch(adapter, "RALPH-STORY-COMPLETE\n")

            self.assertEqual(outcome.output, "RALPH-STORY-COMPLETE\n")
            self.assertEqual(outcome.kind, ralph_agent.NORMAL)
            self.assertFalse(any(outcome.usage.available(c)
                                 for c in ralph_usage.CATEGORIES))

    def test_a_broken_envelope_never_fails_the_invocation(self):
        outcome, _ = self.launch(ralph_agent.ClaudeAdapter(),
                                 '{"result": "half an env')

        self.assertEqual(outcome.kind, ralph_agent.NORMAL)
        self.assertIn("half an env", outcome.output)

    def test_a_dead_provider_is_still_classified_on_what_it_said(self):
        # Unwrapping happens first, so the session-limit and failure verdicts
        # read the provider's own words rather than the JSON around them.
        outcome, _ = self.launch(ralph_agent.CodexAdapter(), CODEX_EVENTS,
                                 exit_code=1)

        self.assertEqual(outcome.kind, ralph_agent.INFRASTRUCTURE_FAILURE)

    def test_every_launchable_provider_has_a_usage_mapping(self):
        for provider in ralph_agent.PROVIDERS:
            self.assertIn(provider, ralph_usage.MAPPED_PROVIDERS)


REVIEW_FIXTURES = os.path.join(REPO_ROOT, "test", "fixtures", "reviews")
HEAD = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"


def review_fixture(name):
    with open(os.path.join(REVIEW_FIXTURES, name)) as fh:
        return json.load(fh)


class EveryInvocationEmitsOneEvent(unittest.TestCase):
    """The round is the seam that spends an invocation, so it is the seam that
    reports what the invocation cost."""

    def story(self):
        return {"number": 62, "title": "Account for tokens",
                "body": "## Acceptance Criteria\n\n- [ ] counts\n",
                "labels": [{"name": "state:in-review"}, {"name": "type:afk"}]}

    def pull_request(self, reviews=None):
        return {"number": 70, "headRefOid": HEAD, "baseRefOid": "b" * 40,
                "body": ralph_review.MANAGED_PR_MARKER,
                "reviews": reviews or [], "comments": []}

    def launcher(self, kind=ralph_agent.NORMAL, output=None, usage=None):
        def launch(prompt):
            return ralph_agent.Outcome(
                kind, "codex", "gpt-5-codex", 0,
                output if output is not None
                else json.dumps(dict(review_fixture("valid-inline.json"),
                                     head=HEAD)),
                usage=ralph_usage.normalize("codex",
                                            usage or CODEX_USAGE)), []
        return launch

    def conduct(self, launch, pull_request=None):
        return ralph_review_round.conduct(
            self.story(), pull_request or self.pull_request(),
            "# Ralph Review Context v1\n", launch=launch,
            publish=lambda review: (True, []))

    def test_a_published_round_reports_what_the_reviewer_cost(self):
        event = self.conduct(self.launcher()).usage_event

        self.assertEqual(event["version"], ralph_usage.CONTRACT_VERSION)
        self.assertEqual(event["story"], 62)
        self.assertEqual(event["pull_request"], 70)
        self.assertEqual(event["role"], "review")
        self.assertEqual(event["phase"], ralph_usage.REVIEW)
        self.assertEqual(event["round"], 1)
        self.assertEqual(event["head"], HEAD)
        self.assertEqual(event["provider"], "codex")
        self.assertEqual(event["model"], "gpt-5-codex")
        self.assertEqual(event["usage"]["output"], 5)

    def test_a_reviewer_that_died_still_spent_its_invocation(self):
        # The tokens were spent whether or not anything publishable came back;
        # a ledger that only counts successes understates every bad week.
        result = self.conduct(
            self.launcher(kind=ralph_agent.INFRASTRUCTURE_FAILURE))

        self.assertFalse(result.ok)
        self.assertIsNotNone(result.usage_event)
        self.assertEqual(result.usage_event["round"], 1)

    def test_unpublishable_output_still_spent_its_invocation(self):
        result = self.conduct(self.launcher(output="no contract object here"))

        self.assertEqual(result.kind, ralph_review_round.INVALID_OUTPUT)
        self.assertIsNotNone(result.usage_event)

    def test_a_round_that_never_launched_reports_nothing(self):
        # No invocation, no event: an empty row would read as a free review.
        reviewed = self.pull_request(
            reviews=[{"body": ralph_review.review_marker(HEAD)}])

        result = self.conduct(self.launcher(), pull_request=reviewed)

        self.assertEqual(result.kind, ralph_review_round.ALREADY_REVIEWED)
        self.assertIsNone(result.usage_event)

    def test_the_event_is_emitted_as_one_machine_readable_line(self):
        stream = io.StringIO()
        event = self.conduct(self.launcher()).usage_event

        ralph_usage.emit(event, stream=stream)

        line = stream.getvalue().strip()
        self.assertTrue(line.startswith(ralph_usage.EVENT_MARKER), line)
        self.assertEqual(
            json.loads(line[len(ralph_usage.EVENT_MARKER):])["story"], 62)
        self.assertEqual(len(stream.getvalue().splitlines()), 1)


class CliNormalizeUsage(unittest.TestCase):
    """An operator can see exactly what a provider's payload normalizes to."""

    def _run(self, provider, payload):
        return subprocess.run(
            [os.path.join(REPO_ROOT, "bin", "ralph"), "--normalize-usage",
             provider],
            cwd=REPO_ROOT, input=payload, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True)

    def test_it_prints_the_counts_and_what_is_actually_known(self):
        proc = self._run("claude", json.dumps(CLAUDE_USAGE))

        self.assertEqual(proc.returncode, 0, proc.stderr)
        printed = json.loads(proc.stdout)
        self.assertEqual(printed["tokens"]["output"], 40)
        self.assertIsNone(printed["tokens"]["total"])
        self.assertEqual(printed["availability"]["total"],
                         ralph_usage.UNAVAILABLE)

    def test_an_unreadable_payload_is_reported_not_guessed_at(self):
        proc = self._run("claude", "not json at all")

        self.assertEqual(proc.returncode, 2)
        self.assertEqual(proc.stdout, "")


class CliLaunchAgentReportsUsage(unittest.TestCase):
    """The implementation role is launched straight from the tick, so its own
    invocation is accounted for at the launcher rather than in a round."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = self.tmp.name

    def _write(self, name, text):
        path = os.path.join(self.root, name)
        with open(path, "w") as fh:
            fh.write(text)
        return path

    def _provider(self, name, output):
        path = self._write(name, "")
        with open(path, "w") as fh:
            fh.write('#!/usr/bin/env bash\ncat >/dev/null\ncat <<\'OUT\'\n%s\nOUT\n'
                     % output)
        os.chmod(path, os.stat(path).st_mode | stat.S_IEXEC)

    def run_launch(self, role="implementation"):
        env = dict(os.environ, PATH=self.root + os.pathsep + os.environ["PATH"],
                   RALPH_RUN_ID="tick-under-test")
        for name in ("RALPH_CLAUDE", "RALPH_CODEX"):
            env.pop(name, None)
        story_path = self._write("story.json", json.dumps(
            {"number": 62, "title": "Account", "body": "",
             "labels": [{"name": "state:in-progress"}, {"name": "type:afk"},
                        {"name": "model:impl:claude-opus-5"},
                        {"name": "model:review:gpt-5-codex"}]}))
        return subprocess.run(
            [os.path.join(REPO_ROOT, "bin", "ralph"), "--launch-agent", role,
             os.path.join(FIXTURES, "valid", "models.yml"),
             "--story", story_path],
            cwd=self.root, env=env, input="a prompt",
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

    def event_from(self, stderr):
        lines = [line for line in stderr.splitlines()
                 if line.startswith(ralph_usage.EVENT_MARKER)]
        self.assertEqual(len(lines), 1, stderr)
        return json.loads(lines[0][len(ralph_usage.EVENT_MARKER):])

    def test_an_implementation_launch_emits_one_event(self):
        self._provider("claude", CLAUDE_ENVELOPE)

        proc = self.run_launch()

        self.assertEqual(proc.returncode, 0, proc.stderr)
        event = self.event_from(proc.stderr)
        self.assertEqual(event["role"], "implementation")
        self.assertEqual(event["phase"], ralph_usage.IMPLEMENTATION)
        self.assertEqual(event["story"], 62)
        self.assertEqual(event["model"], "claude-opus-5")
        self.assertEqual(event["run"], "tick-under-test")
        self.assertEqual(event["usage"]["output"], 40)

    def test_the_agent_s_own_words_still_reach_stdout_unchanged(self):
        # Accounting rides on stderr precisely so it cannot alter what the tick
        # reads: the done-signal marker has to survive the envelope.
        self._provider("claude", json.dumps(
            {"result": "done\nRALPH-STORY-COMPLETE\n", "usage": CLAUDE_USAGE}))

        proc = self.run_launch()

        self.assertIn("RALPH-STORY-COMPLETE", proc.stdout)
        self.assertNotIn(ralph_usage.EVENT_MARKER, proc.stdout)

    def test_a_provider_reporting_nothing_still_runs_and_still_reports(self):
        self._provider("claude", "plain prose, no envelope")

        proc = self.run_launch()

        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("plain prose", proc.stdout)
        event = self.event_from(proc.stderr)
        self.assertEqual(event["availability"]["output"],
                         ralph_usage.UNAVAILABLE)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
