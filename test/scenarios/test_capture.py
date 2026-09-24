"""Incident capture round-trips into a scenario world (#90, PRD #85).

The source is a live scenario world left mid-incident: a Story whose review
negotiation ran two rounds and escalated, so its open pull request carries
reviews, inline threads, replies, responses and statuses.  `ralph --capture`
reads it through the fake `gh`; a second world loads the capture; and the Loop's
*own* readers must see the same Story state in both, commit ids translated.
"""
import json
import os
import subprocess
import tempfile
import unittest

import harness

READER = r"""
import json, sys
sys.path.insert(0, %(lib)r)
import ralph_review, ralph_review_round
story = json.loads(__import__("subprocess").run(
    ["gh", "issue", "view", "1", "--json", "number,title,labels,body,state,comments"],
    check=True, stdout=-1, text=True).stdout)
pr, errors = ralph_review_round.discover_pull_request(story)
comments = ralph_review_round.story_comments(1, None)
head = pr["headRefOid"]
print(json.dumps({
    "story": story, "pull_request": pr, "errors": errors,
    "needs_review": ralph_review.needs_review(pr, comments),
    "next_round": ralph_review_round.next_round(comments),
    "rounds_spent": ralph_review.rounds_spent(comments),
    "latest_result": ralph_review.latest_result(comments, head),
    "reviewed_heads": sorted(ralph_review.reviewed_heads(pr)),
}, sort_keys=True))
""" % {"lib": os.path.join(harness.REPO_ROOT, "lib")}

READS = {("issue", "view"), ("issue", "list"), ("pr", "view"), ("pr", "list"),
         ("repo", "view")}


def is_read(argv):
    if tuple(argv[:2]) in READS:
        return True
    return argv[0] == "api" and "--method" not in argv and "-f" not in argv \
        and "--input" not in argv


class CaptureRoundTrip(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        spec = harness.load("afk_happy_path")
        spec["agents"] = {"implementation": "cooperative", "review": "strict"}
        cls.source = harness.World(spec)
        tick = cls.source.tick()
        assert tick.returncode == 0, tick.stdout
        cls.before = len(cls.source.calls())
        cls.out = tempfile.mkdtemp(prefix="ralph-capture-")
        cls.captured = subprocess.run([harness.RALPH, "--capture", "1", "--out", cls.out],
                                 cwd=cls.source.checkout, env=cls.source.env,
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        cls.path = os.path.join(cls.out, "story-1.json")
        replay_spec = dict(spec, github={"capture": cls.path})
        cls.replay = harness.World(replay_spec)

    @classmethod
    def tearDownClass(cls):
        cls.source.cleanup()
        cls.replay.cleanup()
        subprocess.run(["rm", "-rf", cls.out])

    def read(self, world):
        proc = subprocess.run(["python3", "-c", READER], cwd=world.checkout, env=world.env,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return json.loads(proc.stdout)

    def test_capture_writes_the_fixture(self):
        self.assertEqual(self.captured.returncode, 0, self.captured.stderr)
        self.assertEqual(self.captured.stdout.strip(), self.path)
        with open(self.path) as fh:
            captured = json.load(fh)
        (pr,) = captured["pulls"].values()
        self.assertEqual(captured["format"], "ralph-capture/v1")
        self.assertTrue(captured["issues"]["1"]["comments"])
        self.assertEqual(len(pr["reviews"]), 2)
        self.assertTrue(any(c["in_reply_to_id"] for c in pr["reviewComments"]))
        self.assertTrue(pr["comments"])
        self.assertEqual(len(captured["git"]["commits"]), 2)

    def test_capture_makes_no_mutating_github_call(self):
        calls = [c for c in self.source.calls()[self.before:] if c["tool"] == "gh"]
        self.assertTrue(calls)
        self.assertEqual([c["argv"] for c in calls if not is_read(c["argv"])], [])

    def test_the_loop_reads_the_same_story_state_from_the_replay(self):
        source = harness.translate_oids(self.read(self.source), self.replay.oid_map)
        replay = self.read(self.replay)
        for view in (source, replay):
            view["reviewed_heads"].sort()
        self.assertEqual(replay["errors"], [])
        self.assertEqual(replay["rounds_spent"], 2)
        self.assertEqual(replay, source)

    def test_ralph_run_ticks_against_the_capture(self):
        proc = self.replay.tick()
        self.assertEqual(proc.returncode, 0, proc.stdout)
        harness.check_calls(self.replay, 1, proc.stdout)
        # The escalated Story is blocked, so the tick's arbitration pass is
        # what reads it -- and finds no human decision in the captured reviews.
        argv = [c["argv"] for c in self.replay.calls() if c["tool"] == "gh"]
        self.assertIn(["issue", "view", "1", "--json", "number,title,labels,body,state"], argv)
        self.assertIn("state:blocked", self.replay.state()["issues"]["1"]["labels"])

    def test_every_recorded_head_exists_in_the_replay_remote(self):
        replay = self.read(self.replay)
        for head in replay["reviewed_heads"]:
            proc = subprocess.run(["git", "--git-dir", self.replay.remote, "cat-file", "-e",
                                   head + "^{commit}"])
            self.assertEqual(proc.returncode, 0, head)


if __name__ == "__main__":
    unittest.main()
