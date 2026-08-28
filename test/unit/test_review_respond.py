"""Contract tests for answering a review's findings (#55).

A review that requests changes is answered by a fresh implementation round: fix
commits are appended, never amended, each accepted finding gets a reply in its
own thread, and one machine-readable record states the disposition of every
open finding.
"""
import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(REPO_ROOT, "lib"))

import ralph_agent  # noqa: E402
import ralph_review  # noqa: E402
import ralph_review_respond  # noqa: E402

FIXTURES = os.path.join(REPO_ROOT, "test", "fixtures", "reviews")
HEAD = "9f1c2d3e4b5a60718293a4b5c6d7e8f90a1b2c3d"
FIXED = "1a2b3c4d5e6f70819293a4b5c6d7e8f90a1b2c3d"


def fixture(name):
    with open(os.path.join(FIXTURES, name)) as fh:
        return json.load(fh)


def story(number=55):
    return {"number": number, "title": "Accept findings",
            "body": "## Acceptance Criteria\n\n- [ ] fixes are appended\n",
            "labels": [{"name": "state:in-review"}, {"name": "type:afk"}]}


def pull_request(head=HEAD):
    return {"number": 70, "headRefOid": head, "baseRefOid": "b" * 40,
            "state": "OPEN", "body": ralph_review.MANAGED_PR_MARKER,
            "reviews": [{"body": ralph_review.review_marker(head)}],
            "comments": []}


def response(head=HEAD, ids=("F-3",), disposition="accepted"):
    return {"contract": ralph_review_respond.CONTRACT_VERSION,
            "head": head, "round": 1, "model": "claude-opus-5",
            "summary": "Added the missing test and re-ran the gate.",
            "dispositions": [
                {"id": ident, "disposition": disposition,
                 "note": "Added a fixture over the byte limit and asserted the "
                         "validator rejects it."}
                for ident in ids]}


class Checkout:
    """The two git facts a response round needs, and nothing else."""

    def __init__(self, after=FIXED, descendant=True):
        self.after = after
        self.descendant = descendant

    def head(self):
        return self.after

    def is_ancestor(self, old, new):
        return self.descendant


class Agent:
    def __init__(self, output=None, kind=ralph_agent.NORMAL, publish_ok=True):
        self.output = json.dumps(response()) if output is None else output
        self.kind = kind
        self.publish_ok = publish_ok
        self.prompts = []
        self.published = []

    def launch(self, prompt):
        self.prompts.append(prompt)
        return ralph_agent.Outcome(self.kind, "claude", "claude-opus-5", 0,
                                   self.output), []

    def publish(self, answer, head):
        self.published.append((answer, head))
        return self.publish_ok, [] if self.publish_ok else ["gh: boom"]


class ConductResponse(unittest.TestCase):
    def conduct(self, agent, checkout=None, result=None):
        return ralph_review_respond.conduct(
            story(), pull_request(),
            result or fixture("cross-cutting.json"),
            "# Ralph Review Context v1\n",
            launch=agent.launch, publish=agent.publish,
            checkout=checkout or Checkout())

    def test_the_implementation_model_is_launched_with_the_open_findings(self):
        agent = Agent()

        outcome = self.conduct(agent)

        self.assertTrue(outcome.ok, outcome.errors)
        self.assertEqual(len(agent.prompts), 1)
        prompt = agent.prompts[0]
        self.assertIn("Ralph Response Prompt", prompt)
        self.assertIn("F-3", prompt)
        self.assertIn("No test exercises an oversized payload", prompt)

    def two_findings(self):
        result = fixture("cross-cutting.json")
        second = dict(result["findings"][0], id="F-4")
        return dict(result, findings=result["findings"] + [second])

    def test_a_finding_left_unanswered_refuses_the_whole_response(self):
        agent = Agent()  # answers F-3 only

        outcome = self.conduct(agent, result=self.two_findings())

        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.kind, ralph_review_respond.INVALID_OUTPUT)
        self.assertEqual(agent.published, [])
        self.assertIn("F-4", " ".join(outcome.errors))

    def test_an_answer_to_a_finding_nobody_raised_is_refused(self):
        agent = Agent(output=json.dumps(response(ids=("F-3", "F-99"))))

        outcome = self.conduct(agent)

        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.kind, ralph_review_respond.INVALID_OUTPUT)
        self.assertIn("F-99", " ".join(outcome.errors))

    def test_output_that_is_not_a_response_reaches_no_pull_request(self):
        agent = Agent(output="I fixed it, trust me.")

        outcome = self.conduct(agent)

        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.kind, ralph_review_respond.INVALID_OUTPUT)
        self.assertEqual(agent.published, [])

    def test_a_response_answering_another_commits_review_is_refused(self):
        agent = Agent(output=json.dumps(response(head="0" * 40)))

        outcome = self.conduct(agent)

        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.kind, ralph_review_respond.INVALID_OUTPUT)
        self.assertEqual(agent.published, [])

    def test_a_rewritten_head_is_refused_so_nothing_is_posted(self):
        # An amend, a rebase or a force-push leaves a head that is not a
        # descendant of the reviewed commit: the review threads and checks
        # would point at a commit that is no longer in the branch.
        agent = Agent()

        outcome = self.conduct(agent, checkout=Checkout(descendant=False))

        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.kind, ralph_review_respond.NOT_APPEND_ONLY)
        self.assertEqual(agent.published, [])
        reason = " ".join(outcome.errors)
        self.assertIn(HEAD, reason)
        self.assertIn(FIXED, reason)

    def test_an_accepted_finding_with_no_new_commit_is_refused(self):
        # "Accepted" with an unchanged head is a claim with no fix behind it.
        agent = Agent()

        outcome = self.conduct(agent, checkout=Checkout(after=HEAD))

        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.kind, ralph_review_respond.NOT_APPEND_ONLY)
        self.assertEqual(agent.published, [])

    def disputed(self, ids=("F-3",)):
        answer = response(ids=ids, disposition=ralph_review_respond.DISPUTED)
        for disposition in answer["dispositions"]:
            disposition["evidence"] = "The oversized fixture is loaded at " \
                                      "test/unit/test_review_result.py:210."
        return json.dumps(answer)

    def test_a_dispute_answers_the_round_without_moving_the_head(self):
        agent = Agent(output=self.disputed())

        outcome = self.conduct(agent, checkout=Checkout(after=HEAD))

        self.assertTrue(outcome.ok, outcome.errors)
        self.assertEqual(outcome.new_head, HEAD)
        self.assertEqual(len(agent.published), 1)

    def test_a_dispute_that_changed_code_anyway_is_refused(self):
        # A dispute argues; it does not edit. A commit nothing accepted is a
        # change the reviewer was never told about and never asked for.
        agent = Agent(output=self.disputed())

        outcome = self.conduct(agent, checkout=Checkout(after=FIXED))

        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.kind, ralph_review_respond.NOT_APPEND_ONLY)
        self.assertEqual(agent.published, [])
        self.assertIn(FIXED, " ".join(outcome.errors))


class DisputingAFinding(unittest.TestCase):
    """A model may answer a finding with evidence instead of obedience (#56)."""

    def dispute(self, evidence="test/unit/test_review_result.py:210 already "
                               "loads an oversized fixture and asserts the "
                               "refusal names the byte limit."):
        answer = response(ids=("F-3",), disposition=ralph_review_respond.DISPUTED)
        if evidence is not None:
            answer["dispositions"][0]["evidence"] = evidence
        return answer

    def test_a_dispute_backed_by_evidence_is_a_well_formed_answer(self):
        problems = ralph_review_respond.validate_response(
            self.dispute(), fixture("cross-cutting.json"))

        self.assertEqual(problems, [])

    def test_a_dispute_with_no_evidence_is_refused(self):
        # Disobeying a finding is only legitimate when it is checkable; a bare
        # "I disagree" is the hallucination this contract exists to catch.
        problems = ralph_review_respond.validate_response(
            self.dispute(evidence=None), fixture("cross-cutting.json"))

        self.assertTrue(problems)
        self.assertIn("evidence", " ".join(problems))


class PublishingTheResponse(unittest.TestCase):
    """The answer reaches the pull request as ordinary review artifacts."""

    def threads(self):
        return [{"id": 101, "body": "**F-1** (defect, blocking)\n\nboom"},
                {"id": 102, "body": "**F-2** (missing_tests, blocking)\n\nno test"}]

    def test_each_answered_finding_gets_a_reply_in_its_own_thread(self):
        commands = ralph_review_respond.reply_commands(
            response(ids=("F-1", "F-2")), self.threads(), pull_number=70)

        self.assertEqual(len(commands), 2)
        for command, thread in zip(commands, (101, 102)):
            self.assertIn("repos/{owner}/{repo}/pulls/70/comments/%d/replies" % thread,
                          command)
        bodies = [c[c.index("-f") + 1] for c in commands]
        self.assertIn("F-1", bodies[0])
        self.assertIn("accepted", bodies[0])
        self.assertIn("F-2", bodies[1])

    def test_a_finding_with_no_thread_is_left_to_the_consolidated_record(self):
        # A cross-cutting finding was rendered in the review body, not inline,
        # so there is no thread to answer in.
        commands = ralph_review_respond.reply_commands(
            response(ids=("F-3",)), self.threads(), pull_number=70)

        self.assertEqual(commands, [])

    def disputed(self, ids=("F-1",)):
        answer = response(ids=ids, disposition=ralph_review_respond.DISPUTED)
        for disposition in answer["dispositions"]:
            disposition["evidence"] = "test_review_result.py:210 loads a " \
                                      "payload over the byte limit."
        return answer

    def test_a_disputed_findings_thread_reply_carries_its_evidence(self):
        commands = ralph_review_respond.reply_commands(
            self.disputed(), self.threads(), pull_number=70)

        body = commands[0][commands[0].index("-f") + 1]
        self.assertIn("F-1", body)
        self.assertIn("disputed", body)
        self.assertIn("test_review_result.py:210", body)

    def test_the_consolidated_record_carries_a_disputes_evidence(self):
        body = ralph_review_respond.response_comment(self.disputed(), HEAD)

        # The prose half, not only the machine record underneath it: whoever
        # arbitrates this dispute reads the comment, not the JSON.
        prose = body.split("<!--")[0]
        self.assertIn("disputed", prose)
        self.assertIn("test_review_result.py:210", prose)

    def test_the_consolidated_record_states_every_disposition(self):
        answer = response(ids=("F-1", "F-2"))

        body = ralph_review_respond.response_comment(answer, FIXED)

        self.assertIn("F-1", body)
        self.assertIn("F-2", body)
        self.assertIn(FIXED, body)
        # And it reads back exactly, which is how the loop advances state.
        self.assertEqual(ralph_review.latest_response([{"body": body}], HEAD),
                         answer)


class RespondPromptV1(unittest.TestCase):
    """The judgement half of a response round is checked in, so it is guarded."""

    def setUp(self):
        self.assertTrue(os.path.isfile(ralph_review_respond.RESPOND_PROMPT),
                        "prompts/respond.v1.md must be checked in")
        with open(ralph_review_respond.RESPOND_PROMPT) as fh:
            self.text = fh.read()

    def test_forbids_every_way_of_rewriting_the_reviewed_commit(self):
        low = self.text.lower()
        for needle in ["never amend", "force-push", "rebase", "squash",
                       "descendant"]:
            self.assertIn(needle, low, "respond.v1 prompt missing: %s" % needle)

    def test_requires_an_answer_to_every_finding_by_identifier(self):
        for needle in ("dispositions", "id", ralph_review_respond.ACCEPTED,
                       ralph_review_respond.UNRESOLVED,
                       ralph_review_respond.CONTRACT_VERSION):
            self.assertIn(needle, self.text,
                          "respond.v1 prompt missing: %s" % needle)
        self.assertIn("every open finding", self.text.lower())

    def test_keeps_the_fix_test_first_and_the_gate_green(self):
        low = self.text.lower()
        self.assertIn("failing test", low)
        self.assertIn("ralph --run-gating", low)

    def test_a_dispute_must_be_verified_evidenced_and_code_free(self):
        low = self.text.lower()
        self.assertIn(ralph_review_respond.DISPUTED, low)
        self.assertIn("evidence", low)
        self.assertIn("changes no code", low)
        self.assertIn("verification", low)

    def test_says_a_dispute_is_adjudicated_by_a_fresh_reviewer(self):
        low = self.text.lower()
        self.assertIn("fresh", low)
        self.assertIn("withdraw", low)
        self.assertIn("human", low)

    def test_uses_hil_terminology_not_hitl(self):
        self.assertNotIn("HITL", self.text)


class CliRespondReview(unittest.TestCase):
    """Executed against a real checkout, so append-only is checked for real."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = self.tmp.name
        self.log = os.path.join(self.root, "calls.log")
        self.base, self.head = self._repo()
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

    def _run(self, *args):
        subprocess.run(args, cwd=self.root, check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def _rev(self, ref="HEAD"):
        return subprocess.run(["git", "rev-parse", ref], cwd=self.root,
                              stdout=subprocess.PIPE, text=True).stdout.strip()

    def _repo(self):
        self._run("git", "init", "-q", ".")
        self._run("git", "config", "user.email", "ralph@example.invalid")
        self._run("git", "config", "user.name", "Ralph")
        self._write("lib/thing.py", "def thing():\n    return 1\n")
        self._run("git", "add", "-A")
        self._run("git", "commit", "-qm", "base")
        base = self._rev()
        self._write("lib/thing.py", "def thing():\n    return 2\n")
        self._run("git", "commit", "-aqm", "head")
        # A real remote, so "the push creates a new head" is observable.
        self.remote = os.path.join(self.root, "remote.git")
        subprocess.run(["git", "init", "-q", "--bare", self.remote], check=True,
                       stdout=subprocess.DEVNULL)
        self._run("git", "remote", "add", "origin", self.remote)
        self._run("git", "push", "-q", "-u", "origin", "HEAD")
        return base, self._rev()

    def _write(self, name, text):
        path = os.path.join(self.root, name)
        if os.path.dirname(name):
            os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as fh:
            fh.write(text)
        return path

    def review(self):
        result = dict(fixture("cross-cutting.json"), head=self.head, round=1)
        result["findings"][0]["id"] = "F-1"
        return result

    DISPUTE_EVIDENCE = "test/unit/test_review_result.py:210 already loads an " \
                       "oversized payload and asserts the refusal."

    def _agent(self, fix="append"):
        """A mock implementation CLI that really changes the branch.

        `fix="dispute"` is the one mode that does not: a dispute answers the
        finding and leaves the reviewed commit exactly where it was.
        """
        answer = dict(response(head=self.head, ids=("F-1",)),
                      model="claude-opus-5")
        if fix == "dispute":
            answer["dispositions"][0].update(
                disposition=ralph_review_respond.DISPUTED,
                note="The finding asks for a test that already exists.",
                evidence=self.DISPUTE_EVIDENCE)
        self._write("answer.json", json.dumps(answer))
        edit = {"append": "printf '\\n# fix\\n' >> lib/thing.py\n"
                          "git commit -aqm 'fix(#55): address F-1' >/dev/null 2>&1",
                "amend": "printf '\\n# fix\\n' >> lib/thing.py\n"
                         "git commit -aqm 'amended' --amend >/dev/null 2>&1",
                "dispute": ":"}[fix]
        path = self._write("claude", "")
        with open(path, "w") as fh:
            fh.write('#!/usr/bin/env bash\n'
                     'echo "claude $*" >> "$RALPH_LOG"\n'
                     'cat > /dev/null\n'
                     'cd "%(root)s"\n'
                     '%(edit)s\n'
                     'cat "%(root)s/answer.json"\n'
                     % {"root": self.root, "edit": edit})
        os.chmod(path, os.stat(path).st_mode | stat.S_IEXEC)

    def _gh(self):
        marked = ralph_review.MANAGED_PR_MARKER + "\n\nRefs #55\n"
        self._write("prs.json", json.dumps([{"number": 70, "body": marked}]))
        self._write("pr.json", json.dumps({
            "number": 70, "body": marked, "state": "OPEN",
            "headRefOid": self.head, "baseRefOid": self.base,
            "reviews": [{"body": ralph_review.review_marker(self.head)}],
            "comments": []}))
        self._write("issue-comments.json", json.dumps({"comments": [
            {"body": ralph_review.result_record(self.review())}]}))
        self._write("threads.json", json.dumps([
            {"id": 101, "body": "**F-1** (missing_tests, blocking)\n\nno test"}]))
        path = self._write("gh", "")
        with open(path, "w") as fh:
            fh.write('#!/usr/bin/env bash\n'
                     'echo "gh $*" >> "$RALPH_LOG"\n'
                     'case "$1 $2" in\n'
                     '  "pr list") cat "%(r)s/prs.json";;\n'
                     '  "pr view") cat "%(r)s/pr.json";;\n'
                     '  "issue view") cat "%(r)s/issue-comments.json";;\n'
                     '  "api repos/{owner}/{repo}/pulls/70/comments")'
                     ' cat "%(r)s/threads.json";;\n'
                     'esac\n'
                     'exit 0\n' % {"r": self.root})
        os.chmod(path, os.stat(path).st_mode | stat.S_IEXEC)

    def run_respond(self):
        assigned = dict(story(), labels=story()["labels"] + [
            {"name": "model:impl:claude-opus-5"},
            {"name": "model:review:gpt-5-codex"}])
        self._write("story.json", json.dumps(assigned))
        env = dict(os.environ, PATH=self.root + os.pathsep + os.environ["PATH"],
                   RALPH_LOG=self.log)
        for name in ("RALPH_CLAUDE", "RALPH_CODEX"):
            env.pop(name, None)
        proc = subprocess.run(
            [os.path.join(REPO_ROOT, "bin", "ralph"), "--respond-review",
             os.path.join(self.root, "story.json"), ".ralph.yml", self.root],
            cwd=self.root, env=env, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True)
        calls = ""
        if os.path.exists(self.log):
            with open(self.log) as fh:
                calls = fh.read()
        return proc, calls

    def test_findings_are_answered_with_an_appended_commit_and_replies(self):
        self._agent(fix="append")
        self._gh()

        proc, calls = self.run_respond()

        self.assertEqual(proc.returncode, 0, proc.stderr)
        # The Story's *assigned* implementation model ran, in its own role --
        # not the reviewer's read-only launch.
        self.assertIn("claude --dangerously-skip-permissions", calls)
        self.assertIn("--model claude-opus-5", calls)
        self.assertNotIn("--safe-mode", calls)
        self.assertNotEqual(self._rev(), self.head)
        self.assertEqual(subprocess.run(
            ["git", "merge-base", "--is-ancestor", self.head, "HEAD"],
            cwd=self.root).returncode, 0)
        # Each answered finding got a reply in its own thread.
        self.assertIn("pulls/70/comments/101/replies", calls)
        self.assertIn("F-1", calls)
        # And one consolidated record states every disposition.
        self.assertIn("ralph-review-response:v1", calls)
        # The fix reached the remote: that new head is what a fresh review
        # round will judge, and it still contains the reviewed commit.
        remote_head = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=self.remote,
            stdout=subprocess.PIPE, text=True).stdout.strip()
        self.assertEqual(remote_head, self._rev())
        self.assertNotEqual(remote_head, self.head)

    def test_a_refused_response_never_reaches_the_remote(self):
        self._agent(fix="amend")
        self._gh()

        self.run_respond()

        remote_head = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=self.remote,
            stdout=subprocess.PIPE, text=True).stdout.strip()
        self.assertEqual(remote_head, self.head)

    def test_a_dispute_answers_in_the_thread_and_the_record_but_writes_no_commit(self):
        self._agent(fix="dispute")
        self._gh()

        proc, calls = self.run_respond()

        self.assertEqual(proc.returncode, 0, proc.stderr)
        # The argument reached both places a reader meets it.
        self.assertIn("pulls/70/comments/101/replies", calls)
        self.assertIn(ralph_review_respond.DISPUTED, calls)
        self.assertIn(self.DISPUTE_EVIDENCE, calls)
        self.assertIn("ralph-review-response:v1", calls)
        # And the commit under review is untouched, locally and on the remote.
        self.assertEqual(self._rev(), self.head)
        self.assertEqual(subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=self.remote,
            stdout=subprocess.PIPE, text=True).stdout.strip(), self.head)

    def test_a_round_that_is_already_answered_is_not_answered_twice(self):
        # The answered round is owed a fresh review, not a second answer;
        # answering again would spend an invocation on a settled question.
        self._agent(fix="append")
        self._gh()
        answered = {"contract": ralph_review_respond.CONTRACT_VERSION,
                    "head": self.head, "round": 1, "model": "claude-opus-5",
                    "summary": "Disputed.",
                    "dispositions": [{"id": "F-1",
                                      "disposition": ralph_review_respond.DISPUTED,
                                      "note": "The test exists.",
                                      "evidence": "test_review_result.py:210"}]}
        self._write("issue-comments.json", json.dumps({"comments": [
            {"body": ralph_review.result_record(self.review())},
            {"body": ralph_review.response_record(answered)}]}))

        proc, calls = self.run_respond()

        self.assertEqual(proc.returncode, 2)
        self.assertNotIn("claude", calls)
        self.assertEqual(self._rev(), self.head)

    def test_an_amended_head_is_refused_and_no_reply_is_posted(self):
        self._agent(fix="amend")
        self._gh()

        proc, calls = self.run_respond()

        self.assertEqual(proc.returncode, 2)
        self.assertIn("append", proc.stderr)
        self.assertNotIn("replies", calls)
        self.assertNotIn("ralph-review-response:v1", calls)


if __name__ == "__main__":
    unittest.main()
