"""Contract tests for review round one (#53).

One round is: a fresh, credential-less Review Agent judges an exact
pull-request head, its output is validated by the trusted wrapper, and only
then is it published. These tests assert that sequence through the public
seams, never how the prompt or the plan is assembled.
"""
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(REPO_ROOT, "lib"))

import ralph_agent  # noqa: E402
import ralph_review  # noqa: E402
import ralph_review_render  # noqa: E402
import ralph_review_result  # noqa: E402
import ralph_review_round  # noqa: E402

FIXTURES = os.path.join(REPO_ROOT, "test", "fixtures", "reviews")
HEAD = "9f1c2d3e4b5a60718293a4b5c6d7e8f90a1b2c3d"


def fixture(name):
    with open(os.path.join(FIXTURES, name)) as fh:
        return json.load(fh)


def story(number=53):
    return {"number": number, "title": "Round one",
            "body": "## Acceptance Criteria\n\n- [ ] a fresh reviewer runs\n",
            "labels": [{"name": "state:in-review"}, {"name": "type:afk"}]}


def pull_request(head=HEAD, reviews=None):
    return {"number": 70, "headRefOid": head, "baseRefOid": "b" * 40,
            "body": ralph_review.MANAGED_PR_MARKER,
            "reviews": reviews or [], "comments": []}


class Recorder:
    """A stand-in for the two side-effecting seams of a round."""

    def __init__(self, output=None, kind=ralph_agent.NORMAL, publish_ok=True):
        self.output = output if output is not None else json.dumps(
            fixture("valid-inline.json"))
        self.kind = kind
        self.publish_ok = publish_ok
        self.prompts = []
        self.published = []

    def launch(self, prompt):
        self.prompts.append(prompt)
        return ralph_agent.Outcome(self.kind, "claude", "claude-opus-5", 0,
                                   self.output), []

    def publish(self, result):
        self.published.append(result)
        return self.publish_ok, [] if self.publish_ok else ["gh: boom"]


class ConductRound(unittest.TestCase):
    def test_a_marked_head_is_reviewed_once_and_the_result_published(self):
        agent = Recorder()
        result = ralph_review_round.conduct(
            story(), pull_request(), "# Ralph Review Context v1\n",
            launch=agent.launch, publish=agent.publish)

        self.assertTrue(result.ok, result.errors)
        self.assertEqual(result.kind, ralph_review_round.PUBLISHED)
        self.assertEqual(len(agent.prompts), 1)
        self.assertEqual(len(agent.published), 1)
        self.assertEqual(agent.published[0]["head"], HEAD)
        self.assertEqual(result.round_no, 1)

    def test_a_head_already_reviewed_spends_no_second_invocation(self):
        agent = Recorder()
        reviewed = pull_request(reviews=[
            {"body": ralph_review.review_marker(HEAD) + "\nRalph model review"}])

        result = ralph_review_round.conduct(
            story(), reviewed, "# Ralph Review Context v1\n",
            launch=agent.launch, publish=agent.publish)

        self.assertTrue(result.ok, result.errors)
        self.assertEqual(result.kind, ralph_review_round.ALREADY_REVIEWED)
        self.assertEqual(result.invocations, 0)
        self.assertEqual(agent.prompts, [])
        self.assertEqual(agent.published, [])

    def test_a_review_of_an_earlier_head_does_not_cover_the_current_one(self):
        agent = Recorder()
        moved = pull_request(reviews=[
            {"body": ralph_review.review_marker("0" * 40) + "\nRalph model review"}])

        result = ralph_review_round.conduct(
            story(), moved, "# Ralph Review Context v1\n",
            launch=agent.launch, publish=agent.publish)

        self.assertEqual(result.kind, ralph_review_round.PUBLISHED)
        self.assertEqual(result.invocations, 1)

    def test_a_published_review_stamps_the_head_the_next_round_reads(self):
        posted = ralph_review_render.review_body(fixture("valid-inline.json"))
        agent = Recorder()

        result = ralph_review_round.conduct(
            story(), pull_request(reviews=[{"body": posted}]),
            "# Ralph Review Context v1\n",
            launch=agent.launch, publish=agent.publish)

        self.assertEqual(result.kind, ralph_review_round.ALREADY_REVIEWED)
        self.assertEqual(agent.prompts, [])

    def test_output_that_is_not_a_review_result_reaches_no_pull_request(self):
        agent = Recorder(output="I had a look and it seems fine to me.")

        result = ralph_review_round.conduct(
            story(), pull_request(), "# Ralph Review Context v1\n",
            launch=agent.launch, publish=agent.publish)

        self.assertFalse(result.ok)
        self.assertEqual(result.kind, ralph_review_round.INVALID_OUTPUT)
        self.assertEqual(agent.published, [])
        self.assertTrue(result.errors)

    def test_a_result_judging_another_commit_is_not_publishable(self):
        elsewhere = dict(fixture("valid-inline.json"), head="0" * 40)
        agent = Recorder(output=json.dumps(elsewhere))

        result = ralph_review_round.conduct(
            story(), pull_request(), "# Ralph Review Context v1\n",
            launch=agent.launch, publish=agent.publish)

        self.assertFalse(result.ok)
        self.assertEqual(result.kind, ralph_review_round.INVALID_OUTPUT)
        self.assertEqual(agent.published, [])
        self.assertIn(HEAD, " ".join(result.errors))

    def test_a_result_fenced_inside_the_agents_prose_is_still_published(self):
        agent = Recorder(output="I reviewed the head. Here is the result:\n\n"
                                "```json\n%s\n```\n\nThat is everything.\n"
                                % json.dumps(fixture("valid-inline.json")))

        result = ralph_review_round.conduct(
            story(), pull_request(), "# Ralph Review Context v1\n",
            launch=agent.launch, publish=agent.publish)

        self.assertTrue(result.ok, result.errors)
        self.assertEqual(result.kind, ralph_review_round.PUBLISHED)
        self.assertEqual(agent.published[0]["head"], HEAD)

    def test_a_provider_that_died_is_reported_as_such_not_as_bad_output(self):
        agent = Recorder(output="", kind=ralph_agent.INFRASTRUCTURE_FAILURE)

        result = ralph_review_round.conduct(
            story(), pull_request(), "# Ralph Review Context v1\n",
            launch=agent.launch, publish=agent.publish)

        self.assertFalse(result.ok)
        self.assertEqual(result.kind, ralph_agent.INFRASTRUCTURE_FAILURE)
        self.assertEqual(agent.published, [])

    def test_an_exhausted_session_is_distinguishable_from_a_crash(self):
        agent = Recorder(output="", kind=ralph_agent.SESSION_EXHAUSTED)

        result = ralph_review_round.conduct(
            story(), pull_request(), "# Ralph Review Context v1\n",
            launch=agent.launch, publish=agent.publish)

        self.assertEqual(result.kind, ralph_agent.SESSION_EXHAUSTED)
        self.assertEqual(agent.published, [])

    def test_the_reviewer_is_launched_with_the_checked_in_prompt_and_evidence(self):
        agent = Recorder()
        bundle = "# Ralph Review Context v1\n\nExact head commit: %s\n" % HEAD

        ralph_review_round.conduct(story(), pull_request(), bundle,
                                   launch=agent.launch, publish=agent.publish)

        prompt = agent.prompts[0]
        self.assertIn(bundle, prompt)
        # The instructions precede the evidence they govern.
        self.assertLess(prompt.index("Ralph Review Prompt"), prompt.index(bundle))
        self.assertIn(ralph_review_result.CONTRACT_VERSION, prompt)


class CliReviewRound(unittest.TestCase):
    """Executed against a real checkout, with mock providers and `gh` on PATH.

    The bundle is bound to real commits, so the round is exercised the way a
    tick runs it: resolve the head, review it, publish the result.
    """

    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.root, True)
        self.log = os.path.join(self.root, "calls.log")
        self.base, self.head = self._repo()
        self._config()

    def _run(self, *args):
        subprocess.run(args, cwd=self.root, check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def _repo(self):
        self._run("git", "init", "-q", ".")
        self._run("git", "config", "user.email", "ralph@example.invalid")
        self._run("git", "config", "user.name", "Ralph")
        self._write("lib/thing.py", "def thing():\n    return 1\n")
        self._write("AGENTS.md", "# notes\n")
        self._run("git", "add", "-A")
        self._run("git", "commit", "-qm", "base")
        base = subprocess.run(["git", "rev-parse", "HEAD"], cwd=self.root,
                              stdout=subprocess.PIPE, text=True).stdout.strip()
        self._write("lib/thing.py", "def thing():\n    return 2\n")
        self._run("git", "commit", "-aqm", "head")
        head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=self.root,
                              stdout=subprocess.PIPE, text=True).stdout.strip()
        return base, head

    def _write(self, name, text):
        path = os.path.join(self.root, name)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as fh:
            fh.write(text)
        return path

    def _config(self):
        self._write(".ralph.yml", """version: 1
gating:
  - name: test
    run: "true"
models:
  profiles:
    - key: claude-opus
      provider: claude
      model: claude-opus-5
    - key: codex-high
      provider: codex
      model: gpt-5-codex
  defaults:
    implementation: claude-opus
    review: codex-high
notify:
  github: someone
""")

    def _provider(self, name, review=None, exit_code=0):
        """A mock provider CLI that records its argv, credentials, and prompt."""
        path = self._write(name, "")
        payload = os.path.join(self.root, "%s-review.json" % name)
        with open(payload, "w") as fh:
            json.dump(review or {}, fh)
        with open(path, "w") as fh:
            fh.write('#!/usr/bin/env bash\n'
                     'echo "%s $*" >> "$RALPH_LOG"\n'
                     'echo "%s GH_TOKEN=${GH_TOKEN:-<unset>} '
                     'GITHUB_TOKEN=${GITHUB_TOKEN:-<unset>}" >> "$RALPH_LOG"\n'
                     'cat > "%s/%s.prompt"\n'
                     'echo "Reviewed it. Here is the result:"\n'
                     'echo \'```json\'\n'
                     'cat "%s"\n'
                     'echo \'```\'\n'
                     'exit %d\n'
                     % (name, name, self.root, name, payload, exit_code))
        os.chmod(path, os.stat(path).st_mode | stat.S_IEXEC)

    def _gh(self, exit_code=0):
        """A mock `gh` that logs argv, serves PR reads, and echoes payloads."""
        path = self._write("gh", "")
        with open(path, "w") as fh:
            fh.write('#!/usr/bin/env bash\n'
                     'echo "gh $*" >> "$RALPH_LOG"\n'
                     'if [[ "$1 $2" == "pr list" ]]; then\n'
                     '  cat "%(root)s/prs.json" 2>/dev/null || echo "[]"\n'
                     '  exit 0\n'
                     'fi\n'
                     'if [[ "$1 $2" == "pr view" ]]; then\n'
                     '  cat "%(root)s/pr.json"\n'
                     '  exit 0\n'
                     'fi\n'
                     'prev=""\n'
                     'for arg in "$@"; do\n'
                     '  if [[ "$prev" == "--input" ]]; then cat "$arg" >> "$RALPH_LOG"; fi\n'
                     '  prev="$arg"\n'
                     'done\n'
                     'exit %(exit)d\n' % {"root": self.root, "exit": exit_code})
        os.chmod(path, os.stat(path).st_mode | stat.S_IEXEC)

    def review(self, **overrides):
        result = dict(fixture("cross-cutting.json"), head=self.head, round=1)
        result.update(overrides)
        return result

    def story_file(self, impl="gpt-5-codex", reviewer="claude-opus-5"):
        data = dict(story(), labels=[
            {"name": "state:in-review"}, {"name": "type:afk"},
            {"name": "model:impl:" + impl},
            {"name": "model:review:" + reviewer}])
        return self._write("story.json", json.dumps(data))

    def pr_file(self, reviews=None, checks=None):
        data = {"number": 70, "baseRefOid": self.base, "headRefOid": self.head,
                "body": ralph_review.MANAGED_PR_MARKER,
                "reviews": reviews or [], "comments": [],
                "statusCheckRollup": checks or []}
        return self._write("pr.json", json.dumps(data))

    def run_round(self, story_path=None, pr_path=None, discover=False):
        env = dict(os.environ, PATH=self.root + os.pathsep + os.environ["PATH"],
                   RALPH_LOG=self.log, GH_TOKEN="secret-token",
                   GITHUB_TOKEN="secret-token")
        for name in ("RALPH_CLAUDE", "RALPH_CODEX"):
            env.pop(name, None)
        args = [story_path or self.story_file(), ".ralph.yml", self.root]
        if not discover:
            args += ["--pr", pr_path or self.pr_file()]
        proc = subprocess.run(
            [os.path.join(REPO_ROOT, "bin", "ralph"), "--review-round"] + args,
            cwd=self.root, env=env, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True)
        calls = ""
        if os.path.exists(self.log):
            with open(self.log) as fh:
                calls = fh.read()
        return proc, calls

    def test_a_marked_head_is_reviewed_and_the_result_reaches_the_pull_request(self):
        self._provider("claude", self.review(model="claude-opus-5"))
        self._gh()

        proc, calls = self.run_round()

        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(calls.count("claude --print"), 1)
        self.assertIn("repos/{owner}/{repo}/pulls/70/reviews", calls)
        self.assertIn("statuses/" + self.head, calls)
        self.assertIn("F-3", calls)

    def test_the_reviewing_process_is_handed_no_github_credential(self):
        self._provider("claude", self.review(model="claude-opus-5"))
        self._gh()

        proc, calls = self.run_round()

        self.assertEqual(proc.returncode, 0, proc.stderr)
        # The tick's own environment carries both; the reviewer sees neither.
        self.assertIn("claude GH_TOKEN=<unset> GITHUB_TOKEN=<unset>", calls)
        self.assertNotIn("secret-token", calls)

    def test_the_reviewing_process_cannot_write_to_the_checkout(self):
        self._provider("claude", self.review(model="claude-opus-5"))
        self._gh()

        proc, calls = self.run_round()

        self.assertEqual(proc.returncode, 0, proc.stderr)
        launch = next(line for line in calls.splitlines()
                      if line.startswith("claude --"))
        self.assertIn("--safe-mode", launch)
        self.assertIn("--permission-mode plan", launch)
        self.assertNotIn("--dangerously-skip-permissions", launch)

    def test_claude_reviews_a_codex_implementation(self):
        self._provider("claude", self.review(model="claude-opus-5"))
        self._provider("codex", {})
        self._gh()

        proc, calls = self.run_round(story_path=self.story_file(
            impl="gpt-5-codex", reviewer="claude-opus-5"))

        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("--model claude-opus-5", calls)
        self.assertNotIn("codex exec", calls)
        self.assertIn("repos/{owner}/{repo}/pulls/70/reviews", calls)

    def test_codex_reviews_a_claude_implementation(self):
        self._provider("codex", self.review(model="gpt-5-codex"))
        self._provider("claude", {})
        self._gh()

        proc, calls = self.run_round(story_path=self.story_file(
            impl="claude-opus-5", reviewer="gpt-5-codex"))

        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("codex exec --sandbox read-only", calls)
        self.assertNotIn("claude --print", calls)
        self.assertIn("repos/{owner}/{repo}/pulls/70/reviews", calls)

    def test_a_still_pending_ci_run_neither_blocks_nor_delays_the_round(self):
        self._provider("claude", self.review(model="claude-opus-5"))
        self._gh()
        pending = self.pr_file(checks=[
            {"name": "build", "status": "IN_PROGRESS", "conclusion": None}])

        proc, calls = self.run_round(pr_path=pending)

        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("repos/{owner}/{repo}/pulls/70/reviews", calls)
        # The reviewer is told what CI is doing, and told not to wait for it.
        with open(os.path.join(self.root, "claude.prompt")) as fh:
            prompt = fh.read()
        self.assertIn("IN_PROGRESS", prompt)
        self.assertIn("never wait for a check to finish", prompt)

    def test_a_result_that_fails_the_contract_reaches_github_not_at_all(self):
        # A preference cannot block, and a blocker must request changes.
        self._provider("claude", self.review(
            model="claude-opus-5", verdict="approve"))
        self._gh()

        proc, calls = self.run_round()

        self.assertEqual(proc.returncode, 2)
        self.assertNotIn("gh ", calls)
        self.assertIn("verdict", proc.stderr)

    def test_a_finding_pointing_outside_the_reviewed_diff_is_never_posted(self):
        invented = self.review(model="claude-opus-5")
        invented["findings"][0]["location"] = {"path": "lib/never.py", "line": 9}
        self._provider("claude", invented)
        self._gh()

        proc, calls = self.run_round()

        self.assertEqual(proc.returncode, 2)
        self.assertNotIn("gh ", calls)
        self.assertIn("lib/never.py", proc.stderr)

    def test_an_unmarked_pull_request_spends_no_invocation_at_all(self):
        self._provider("claude", self.review(model="claude-opus-5"))
        self._gh()
        human = self._write("human-pr.json", json.dumps({
            "number": 71, "baseRefOid": self.base, "headRefOid": self.head,
            "body": "a human pull request", "reviews": [], "comments": []}))

        proc, calls = self.run_round(pr_path=human)

        self.assertEqual(proc.returncode, 2)
        self.assertEqual(calls, "")
        self.assertIn("not Ralph-managed", proc.stderr)

    def test_a_pull_request_without_its_exact_commits_is_refused_not_crashed(self):
        self._provider("claude", self.review(model="claude-opus-5"))
        self._gh()
        vague = self._write("vague-pr.json", json.dumps({
            "number": 70, "body": ralph_review.MANAGED_PR_MARKER,
            "reviews": [], "comments": []}))

        proc, calls = self.run_round(pr_path=vague)

        self.assertEqual(proc.returncode, 2)
        self.assertNotIn("Traceback", proc.stderr)
        self.assertIn("head", proc.stderr)
        self.assertNotIn("claude", calls)

    def test_a_head_already_reviewed_spends_no_invocation_at_all(self):
        self._provider("claude", self.review(model="claude-opus-5"))
        self._gh()
        reviewed = self.pr_file(reviews=[
            {"body": ralph_review.review_marker(self.head)}])

        proc, calls = self.run_round(pr_path=reviewed)

        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(calls, "")
        self.assertIn("already reviewed", proc.stdout)

    def test_the_storys_marked_pull_request_is_found_when_none_is_given(self):
        self._provider("claude", self.review(model="claude-opus-5"))
        self._gh()
        self.pr_file()
        self._write("prs.json", json.dumps([
            {"number": 12, "body": "an unrelated human PR"},
            {"number": 70, "body": ralph_review.MANAGED_PR_MARKER
                                   + "\n\nRefs #53\n"}]))

        proc, calls = self.run_round(discover=True)

        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("gh pr view 70", calls)
        self.assertIn("repos/{owner}/{repo}/pulls/70/reviews", calls)

    def test_a_story_with_no_marked_pull_request_spends_no_invocation(self):
        self._provider("claude", self.review(model="claude-opus-5"))
        self._gh()
        self._write("prs.json", json.dumps([
            {"number": 12, "body": "an unrelated human PR"}]))

        proc, calls = self.run_round(discover=True)

        self.assertEqual(proc.returncode, 2)
        self.assertNotIn("claude", calls)
        self.assertIn("no Ralph-managed pull request", proc.stderr)


class RoundNumbering(unittest.TestCase):
    def test_a_fresh_pull_request_is_round_one(self):
        self.assertEqual(ralph_review_round.next_round(pull_request()), 1)

    def test_each_published_review_advances_the_round(self):
        reviewed = pull_request(reviews=[
            {"body": ralph_review.review_marker("0" * 40)},
            {"body": ralph_review.review_marker("1" * 40)},
        ])
        self.assertEqual(ralph_review_round.next_round(reviewed), 3)

    def test_a_human_review_is_not_a_negotiation_round(self):
        human = pull_request(reviews=[{"body": "looks good to me"}])
        self.assertEqual(ralph_review_round.next_round(human), 1)


class ReviewPromptV1(unittest.TestCase):
    """The judgement half of a round is checked in, so it is drift-guarded."""

    def setUp(self):
        self.assertTrue(os.path.isfile(ralph_review_round.REVIEW_PROMPT),
                        "prompts/review.v1.md must be checked in")
        with open(ralph_review_round.REVIEW_PROMPT) as fh:
            self.text = fh.read()

    def test_states_the_round_runs_fresh_and_read_only(self):
        low = self.text.lower()
        for needle in ["fresh-context", "read-only", "no github credential",
                       "exact head commit", "concurrently with ci"]:
            self.assertIn(needle, low, "review.v1 prompt missing: %s" % needle)

    def test_bounds_the_blocking_scope_to_the_contract_categories(self):
        for category in (ralph_review_result.BLOCKING_CATEGORIES
                         + ralph_review_result.NON_BLOCKING_CATEGORIES):
            self.assertIn(category, self.text,
                          "review.v1 prompt missing category: %s" % category)
        # The bound only holds if the prompt says what may *not* block.
        low = self.text.lower()
        self.assertIn("never** blockers", low)
        self.assertIn("if and only if", low)

    def test_requires_evidence_for_every_finding(self):
        for field in ("claim", "evidence", "requirement", "verification",
                      "location", "id"):
            self.assertIn(field, self.text,
                          "review.v1 prompt missing evidence field: %s" % field)
        self.assertIn(ralph_review_result.CONTRACT_VERSION, self.text)

    def test_uses_hil_terminology_not_hitl(self):
        self.assertNotIn("HITL", self.text)


if __name__ == "__main__":
    unittest.main()
