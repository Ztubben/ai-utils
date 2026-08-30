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
import ralph_review_respond  # noqa: E402
import ralph_review_result  # noqa: E402
import ralph_review_round  # noqa: E402

FIXTURES = os.path.join(REPO_ROOT, "test", "fixtures", "reviews")
HEAD = "9f1c2d3e4b5a60718293a4b5c6d7e8f90a1b2c3d"


def fixture(name):
    with open(os.path.join(FIXTURES, name)) as fh:
        return json.load(fh)


def escaped(text):
    """*text* as it appears inside the JSON payload `gh --input` is handed."""
    return json.dumps(text)[1:-1]


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
        self.usage_events = []

    def launch(self, prompt):
        self.prompts.append(prompt)
        return ralph_agent.Outcome(self.kind, "claude", "claude-opus-5", 0,
                                   self.output), []

    def publish(self, result, usage_event=None):
        self.published.append(result)
        self.usage_events.append(usage_event)
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
                     'if [[ "$1 $2" == "issue view" ]]; then\n'
                     '  cat "%(root)s/issue.json" 2>/dev/null || echo "{}"\n'
                     '  exit 0\n'
                     'fi\n'
                     'if [[ "$1" == "api" && "$2" == *"/comments" ]]; then\n'
                     '  cat "%(root)s/threads.json" 2>/dev/null || echo "[]"\n'
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

    def test_a_published_review_is_recorded_on_the_story_for_later_rounds(self):
        self._provider("claude", self.review(model="claude-opus-5"))
        self._gh()

        proc, calls = self.run_round()

        self.assertEqual(proc.returncode, 0, proc.stderr)
        # Two Story comments, each with its own job: the machine-readable
        # review record, and the token ledger (#63).
        posted = [ln for ln in calls.splitlines()
                  if ln.startswith("gh issue comment 53")]
        self.assertEqual(len(posted), 2, calls)
        self.assertEqual(
            len([ln for ln in posted
                 if ralph_review.RESULT_MARKER_TEMPLATE.split("%s")[0] in ln]),
            1, calls)
        # The record is recovered from the log exactly the way a later round
        # recovers it from the Story's comments.
        recorded = ralph_review.latest_result([{"body": calls}], self.head)
        self.assertEqual(recorded["verdict"], "request_changes")
        self.assertEqual([f["id"] for f in recorded["findings"]], ["F-3"])

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

        self.assertEqual(proc.returncode, ralph_review_round.EXIT_INVALID_OUTPUT)
        # Reading the Story's rounds is a read; nothing was written anywhere.
        self.assertNotIn("--method POST", calls)
        self.assertIn("verdict", proc.stderr)

    def test_a_finding_pointing_outside_the_reviewed_diff_is_never_posted(self):
        invented = self.review(model="claude-opus-5")
        invented["findings"][0]["location"] = {"path": "lib/never.py", "line": 9}
        self._provider("claude", invented)
        self._gh()

        proc, calls = self.run_round()

        self.assertEqual(proc.returncode, ralph_review_round.EXIT_INVALID_OUTPUT)
        self.assertNotIn("--method POST", calls)
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
        self.assertNotIn("claude", calls)
        self.assertNotIn("reviews", calls)
        self.assertIn("already reviewed", proc.stdout)

    def test_a_disputed_head_goes_back_to_a_fresh_reviewer(self):
        # The commit has not moved, but the Story records an answer to its
        # review: the reviewer is owed the chance to withdraw or uphold.
        self._provider("claude", self.review(model="claude-opus-5", round=2))
        self._gh()
        self._write("issue.json", json.dumps({"comments": [
            {"body": ralph_review.result_record(self.review())},
            {"body": ralph_review.response_record({
                "contract": ralph_review_respond.CONTRACT_VERSION,
                "head": self.head, "round": 1, "model": "gpt-5-codex",
                "summary": "The finding is mistaken.",
                "dispositions": [{
                    "id": "F-3", "disposition": ralph_review_respond.DISPUTED,
                    "note": "The test the finding asks for already exists.",
                    "evidence": "test/unit/test_review_result.py:210"}]})}]}))
        reviewed = self.pr_file(reviews=[
            {"body": ralph_review.review_marker(self.head)}])

        proc, calls = self.run_round(pr_path=reviewed)

        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("--safe-mode", calls)
        self.assertIn("repos/{owner}/{repo}/pulls/70/reviews", calls)
        # The reviewer restated F-3, so the published review says so by
        # identifier; the disputing model reads its own answer's outcome.
        self.assertIn("Earlier findings", calls)
        self.assertIn(escaped("**F-3** — upheld"), calls)

    def test_a_withdrawn_finding_is_named_as_withdrawn_in_the_next_review(self):
        approved = dict(self.review(model="claude-opus-5", round=2),
                        verdict="approve", findings=[],
                        summary="The dispute is right; the finding is withdrawn.")
        self._provider("claude", approved)
        self._gh()
        self._write("issue.json", json.dumps({"comments": [
            {"body": ralph_review.result_record(self.review())},
            {"body": ralph_review.response_record({
                "contract": ralph_review_respond.CONTRACT_VERSION,
                "head": self.head, "round": 1, "model": "gpt-5-codex",
                "summary": "The finding is mistaken.",
                "dispositions": [{
                    "id": "F-3", "disposition": ralph_review_respond.DISPUTED,
                    "note": "The test the finding asks for already exists.",
                    "evidence": "test/unit/test_review_result.py:210"}]})}]}))
        reviewed = self.pr_file(reviews=[
            {"body": ralph_review.review_marker(self.head)}])

        proc, calls = self.run_round(pr_path=reviewed)

        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn(escaped("**F-3** — withdrawn"), calls)
        self.assertIn("state=success", calls)

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

    def test_a_human_reply_in_a_thread_is_evidence_for_the_next_round(self):
        # A human who answers in the threads before the negotiation deadlocks
        # has already arbitrated; the next fresh reviewer must read it.
        self._provider("claude", self.review(model="claude-opus-5"))
        self._gh()
        self._write("threads.json", json.dumps([
            {"id": 101, "user": {"login": "carl"}, "path": "lib/thing.py",
             "line": 2, "body": "F-3 is right; fix it in the caller."}]))
        self.pr_file()
        self._write("prs.json", json.dumps([
            {"number": 70, "body": ralph_review.MANAGED_PR_MARKER
                                   + "\n\nRefs #53\n"}]))

        proc, calls = self.run_round(discover=True)

        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("pulls/70/comments", calls)
        with open(os.path.join(self.root, "claude.prompt")) as fh:
            prompt = fh.read()
        self.assertIn("fix it in the caller", prompt)
        self.assertIn("carl", prompt)

    def test_a_story_with_no_marked_pull_request_spends_no_invocation(self):
        self._provider("claude", self.review(model="claude-opus-5"))
        self._gh()
        self._write("prs.json", json.dumps([
            {"number": 12, "body": "an unrelated human PR"}]))

        proc, calls = self.run_round(discover=True)

        self.assertEqual(proc.returncode, 2)
        self.assertNotIn("claude", calls)
        self.assertIn("no Ralph-managed pull request", proc.stderr)


class DurableResultRecord(unittest.TestCase):
    """A published review is also kept machine-readable on the Story.

    The response round (#55) runs in a later process, so it must recover the
    findings exactly rather than parsing back the Markdown Ralph rendered.
    """

    def test_a_recorded_result_reads_back_exactly(self):
        result = fixture("valid-inline.json")
        record = ralph_review.result_record(result)

        recovered = ralph_review.latest_result([{"body": record}], HEAD)

        self.assertEqual(recovered, result)

    def test_a_record_for_another_head_is_not_this_heads_review(self):
        record = ralph_review.result_record(
            dict(fixture("valid-inline.json"), head="0" * 40))

        self.assertIsNone(ralph_review.latest_result([{"body": record}], HEAD))

    def test_the_newest_record_for_a_head_wins(self):
        first = ralph_review.result_record(fixture("valid-inline.json"))
        second = ralph_review.result_record(
            dict(fixture("cross-cutting.json"), head=HEAD))

        recovered = ralph_review.latest_result(
            [{"body": first}, {"body": "unrelated chatter"}, {"body": second}],
            HEAD)

        self.assertEqual(recovered["round"], 2)

    def test_ordinary_story_comments_are_not_review_records(self):
        self.assertIsNone(ralph_review.latest_result(
            [{"body": "Ralph handoff: still working"}], HEAD))


class RoundNumbering(unittest.TestCase):
    """Rounds are counted on the Story, not on whatever shares its PR."""

    def recorded(self, *heads):
        return [{"body": ralph_review.result_record(
            dict(fixture("valid-inline.json"), head=head, round=index + 1))}
            for index, head in enumerate(heads)]

    def test_a_story_with_no_recorded_round_is_round_one(self):
        self.assertEqual(ralph_review_round.next_round([]), 1)
        self.assertEqual(ralph_review_round.next_round(None), 1)

    def test_each_published_review_advances_the_round(self):
        self.assertEqual(
            ralph_review_round.next_round(self.recorded("0" * 40, "1" * 40)), 3)

    def test_a_human_review_is_not_a_negotiation_round(self):
        human = [{"body": "looks good to me"}]
        self.assertEqual(ralph_review_round.next_round(human), 1)

    def test_re_reviewing_one_head_still_advances_the_round(self):
        # A disputed round is judged again at the same commit. Counting heads
        # would hand it the number it just used, so a round limit measured in
        # rounds would never be reached by a model that only ever disputes.
        self.assertEqual(
            ralph_review_round.next_round(self.recorded(HEAD, HEAD)), 3)

    def test_a_sibling_storys_rounds_are_not_this_storys(self):
        """PRD #69: the count is the Story's, however busy its Feature was.

        Under the shared pull request a Feature's later Stories inherited every
        round their predecessors had spent, and one escalated at the limit
        having never been reviewed. A Story's rounds live on the Story.
        """
        busy_pull_request = pull_request(reviews=[
            {"body": ralph_review.review_marker("a" * 40)},
            {"body": ralph_review.review_marker("b" * 40)},
            {"body": ralph_review.review_marker("c" * 40)},
        ])
        self.assertEqual(len(ralph_review.review_stamps(busy_pull_request)), 3)
        self.assertEqual(ralph_review_round.next_round([]), 1)


class ADisputedHeadIsJudgedAgain(unittest.TestCase):
    """A dispute changes no code, so the same commit is owed a fresh round."""

    def reviewed(self):
        return pull_request(reviews=[{"body": ralph_review.review_marker(HEAD)}])

    def answered(self, disposition=ralph_review_respond.DISPUTED):
        judged = dict(fixture("valid-inline.json"), head=HEAD, round=1)
        answer = {"contract": ralph_review_respond.CONTRACT_VERSION,
                  "head": HEAD, "round": 1, "model": "claude-opus-5",
                  "summary": "The finding is wrong; here is why.",
                  "dispositions": [{"id": "F-1", "disposition": disposition,
                                    "note": "The test already exists.",
                                    "evidence": "test_review_result.py:210"}]}
        return [{"body": ralph_review.result_record(judged)},
                {"body": ralph_review.response_record(answer)}]

    def test_an_answered_head_is_reviewed_again(self):
        agent = Recorder(output=json.dumps(dict(fixture("valid-inline.json"),
                                                round=2)))

        result = ralph_review_round.conduct(
            story(), self.reviewed(), "# Ralph Review Context v1\n",
            launch=agent.launch, publish=agent.publish,
            comments=self.answered())

        self.assertTrue(result.ok, result.errors)
        self.assertEqual(result.kind, ralph_review_round.PUBLISHED)
        self.assertEqual(result.round_no, 2)
        self.assertEqual(len(agent.published), 1)

    def test_an_unanswered_reviewed_head_still_spends_nothing(self):
        agent = Recorder()

        result = ralph_review_round.conduct(
            story(), self.reviewed(), "# Ralph Review Context v1\n",
            launch=agent.launch, publish=agent.publish, comments=[])

        self.assertEqual(result.kind, ralph_review_round.ALREADY_REVIEWED)
        self.assertEqual(agent.prompts, [])

    def late(self, category):
        result = dict(fixture("cross-cutting.json"), head=HEAD, round=2)
        result["findings"] = [dict(result["findings"][0], id="F-7",
                                   category=category, blocking=True)]
        return Recorder(output=json.dumps(result))

    def test_a_late_blocker_outside_the_narrow_exception_is_not_published(self):
        # Round two adjudicates F-1; a fresh missing-tests blocker is the
        # goalposts moving, and the answer to it would be a different review.
        agent = self.late("missing_tests")

        result = ralph_review_round.conduct(
            story(), self.reviewed(), "# Ralph Review Context v1\n",
            launch=agent.launch, publish=agent.publish,
            comments=self.answered())

        self.assertFalse(result.ok)
        self.assertEqual(result.kind, ralph_review_round.INVALID_OUTPUT)
        self.assertEqual(agent.published, [])
        self.assertIn("F-7", " ".join(result.errors))

    def test_a_regression_the_fixes_caused_may_still_block_late(self):
        agent = self.late("defect")

        result = ralph_review_round.conduct(
            story(), self.reviewed(), "# Ralph Review Context v1\n",
            launch=agent.launch, publish=agent.publish,
            comments=self.answered())

        self.assertTrue(result.ok, result.errors)
        self.assertEqual(len(agent.published), 1)

    def test_a_second_review_of_the_same_head_is_not_owed_a_third(self):
        # One answer buys one re-review, not an unbounded supply of them.
        twice = pull_request(reviews=[
            {"body": ralph_review.review_marker(HEAD)},
            {"body": ralph_review.review_marker(HEAD)}])
        agent = Recorder()

        result = ralph_review_round.conduct(
            story(), twice, "# Ralph Review Context v1\n",
            launch=agent.launch, publish=agent.publish,
            comments=self.answered())

        self.assertEqual(result.kind, ralph_review_round.ALREADY_REVIEWED)
        self.assertEqual(agent.prompts, [])


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

    def test_scopes_a_later_round_to_adjudicating_what_came_before(self):
        low = " ".join(self.text.lower().split())
        self.assertIn("adjudicate", low)
        self.assertIn("uphold", low)
        self.assertIn("withdraw", low)
        self.assertIn("goalposts do not move", low)

    def test_bounds_a_late_blocker_to_the_two_categories_that_allow_one(self):
        low = " ".join(self.text.lower().split())
        for category in ralph_review_result.LATE_BLOCKING_CATEGORIES:
            self.assertIn(category, low)
        self.assertIn("does not extend the round limit", low)

    def test_uses_hil_terminology_not_hitl(self):
        self.assertNotIn("HITL", self.text)


if __name__ == "__main__":
    unittest.main()
