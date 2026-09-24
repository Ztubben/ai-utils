"""The scenario tier (#87, PRD #85): every world in `worlds/` run through the
real Tick, plus contract tests for the harness's own fakes.

A scenario test is generated per world file, so adding a scenario means adding
a fixture, never harness code.
"""
import json
import os
import subprocess
import unittest

import harness


def _scenario_test(name):
    spec = harness.load(name)
    defect = spec.get("known_defect")

    def test(self):
        if defect is None:
            result = harness.run_scenario(harness.load(name))
            self.assertLessEqual(result.ticks, spec["ticks"])
            return
        # A world may be committed red ahead of its fix (PRD #85: every Loop
        # bug is reproduced as a scenario first). It must stay red for the
        # named reason -- any other failure is a real one -- and once it goes
        # green the Story that fixed it has to remove the declaration.
        try:
            harness.run_scenario(harness.load(name))
        except harness.ScenarioFailure as exc:
            self.assertIn("invariant %s violated" % defect["invariant"], str(exc))
        else:
            self.fail("%s now passes: %s is fixed, remove known_defect from the world"
                      % (name, defect["story"]))
    test.__doc__ = spec.get("description")
    return test


class Scenarios(unittest.TestCase):
    pass


for _name in harness.all_worlds():
    setattr(Scenarios, "test_" + _name, _scenario_test(_name))


class HappyPathIsReallyDriven(unittest.TestCase):
    """What the one happy-path run left behind, beyond "the Story closed"."""

    @classmethod
    def setUpClass(cls):
        cls.result = harness.run_scenario(harness.load("afk_happy_path"), keep=True)
        cls.world = cls.result.world
        cls.state = cls.world.state()
        cls.calls = cls.world.calls()

    @classmethod
    def tearDownClass(cls):
        cls.world.cleanup()

    def test_the_story_pull_request_merged_into_the_base_on_the_remote(self):
        (pr,) = self.state["pulls"].values()
        self.assertEqual(pr["state"], "MERGED")
        base = subprocess.run(
            ["git", "--git-dir", self.world.remote, "rev-parse", "refs/heads/develop"],
            stdout=subprocess.PIPE, text=True, check=True).stdout.strip()
        self.assertEqual(base, pr["mergeCommit"])

    def test_both_roles_ran_their_configured_provider_and_prompt_was_recorded(self):
        agents = [c for c in self.calls if c["tool"] != "gh"]
        self.assertEqual([(c["tool"], c["role"], c["phase"]) for c in agents],
                         [("claude", "implementation", "iteration"), ("codex", "review", "review")])
        self.assertIn("Next action: start #1", agents[0]["prompt"])
        self.assertIn("Exact head commit:", agents[1]["prompt"])

    def test_the_review_status_is_bound_to_the_merged_head(self):
        (pr,) = self.state["pulls"].values()
        statuses = self.state["statuses"][pr["mergedHeadOid"]]
        self.assertEqual([(s["context"], s["state"]) for s in statuses],
                         [("ralph/model-review", "success")])


class RunnerGoesRed(unittest.TestCase):
    def test_a_story_short_of_its_expected_state_fails_the_scenario(self):
        spec = harness.load("afk_happy_path")
        spec.update(ticks=1, expect={"1": "blocked"})
        with self.assertRaises(harness.ScenarioFailure) as caught:
            harness.run_scenario(spec)
        self.assertIn("#1 expected blocked", str(caught.exception))


class NegotiationIsReallyDriven(unittest.TestCase):
    """What a request-changes -> fix -> re-review negotiation left on GitHub."""

    @classmethod
    def setUpClass(cls):
        cls.result = harness.run_scenario(harness.load("negotiation_fix"), keep=True)
        cls.world = cls.result.world
        cls.state = cls.world.state()
        cls.calls = cls.world.calls()
        (cls.pr,) = cls.state["pulls"].values()

    @classmethod
    def tearDownClass(cls):
        cls.world.cleanup()

    def test_two_rounds_were_reviewed_at_two_heads(self):
        heads = [r["commit_id"] for r in self.pr["reviews"]]
        self.assertEqual(len(heads), 2)
        self.assertNotEqual(heads[0], heads[1])
        self.assertEqual(heads[1], self.pr["mergedHeadOid"])

    def test_the_finding_thread_got_a_reply_and_the_pr_a_response(self):
        thread = [c for c in self.pr["reviewComments"] if c["in_reply_to_id"] is None]
        replies = [c for c in self.pr["reviewComments"] if c["in_reply_to_id"]]
        self.assertEqual(len(thread), 1)
        self.assertTrue(thread[0]["body"].startswith("**F-1**"))
        self.assertEqual([r["in_reply_to_id"] for r in replies], [thread[0]["id"]])
        self.assertTrue(any("accepted" in c["body"] for c in self.pr["comments"]))

    def test_each_role_was_told_the_round_and_head_it_acted_on(self):
        told = [(c["phase"], c["round"]) for c in self.calls if c["tool"] != "gh"]
        self.assertEqual(told, [("iteration", None), ("review", 1),
                                ("response", 1), ("review", 2)])
        response = next(c for c in self.calls if c.get("phase") == "response")
        self.assertIn("## Open findings (round 1", response["prompt"])
        self.assertIn("--model", response["argv"])


class FakeGhContract(unittest.TestCase):
    """The fake remembers, follows the remote, and fails closed."""

    def setUp(self):
        self.world = harness.World(harness.load("afk_happy_path"))
        self.addCleanup(self.world.cleanup)

    def gh(self, *args):
        proc = self.world.gh(*args)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return proc.stdout

    def test_writes_are_visible_to_later_reads(self):
        self.gh("issue", "comment", "1", "--body", "hello")
        self.gh("issue", "edit", "1", "--add-label", "state:in-progress",
                "--remove-label", "state:ready")
        view = json.loads(self.gh("issue", "view", "1", "--json", "labels,comments"))
        self.assertEqual([c["body"] for c in view["comments"]], ["hello"])
        names = [label["name"] for label in view["labels"]]
        self.assertIn("state:in-progress", names)
        self.assertNotIn("state:ready", names)

    def test_an_unknown_label_is_refused_like_github(self):
        proc = self.world.gh("issue", "edit", "1", "--add-label", "no-such-label")
        self.assertNotEqual(proc.returncode, 0)

    def test_pull_request_head_follows_the_remote_branch(self):
        env, checkout = self.world.env, self.world.checkout

        def git(*args):
            return subprocess.run(["git"] + list(args), cwd=checkout, env=env, check=True,
                                  stdout=subprocess.PIPE, text=True).stdout.strip()

        def commit_and_push(text):
            with open(os.path.join(checkout, "f.txt"), "a") as fh:
                fh.write(text)
            git("add", "f.txt")
            git("commit", "-qm", text)
            git("push", "-q", "origin", "HEAD:refs/heads/topic")
            return git("rev-parse", "HEAD")

        git("checkout", "-qb", "topic")
        first = commit_and_push("one\n")
        self.gh("pr", "create", "--base", "develop", "--head", "topic",
                "--title", "t", "--body", "b")
        view = lambda: json.loads(self.gh("pr", "view", "2", "--json", "headRefOid"))
        self.assertEqual(view()["headRefOid"], first)
        second = commit_and_push("two\n")
        self.assertEqual(view()["headRefOid"], second)

    def test_an_unemulated_subcommand_fails_naming_the_call(self):
        proc = self.world.gh("gist", "list")
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("unemulated gh call: gh gist list", proc.stderr)

    def test_an_unemulated_api_route_or_flag_fails_too(self):
        for args in (("api", "repos/{owner}/{repo}/branches"),
                     ("issue", "view", "1", "--json", "labels", "--web")):
            proc = self.world.gh(*args)
            self.assertNotEqual(proc.returncode, 0, args)
            self.assertIn("unemulated gh call", proc.stderr)

    def test_the_harness_fails_a_scenario_that_made_an_unemulated_call(self):
        # The Loop swallows some gh failures on purpose, so the harness must
        # read the call log rather than rely on an exit code surfacing.
        self.world.gh("gist", "list")
        with self.assertRaises(harness.ScenarioFailure) as caught:
            harness.check_calls(self.world, 1, "")
        self.assertIn("gist", str(caught.exception))

    def test_every_call_is_logged(self):
        self.world.gh("issue", "view", "1", "--json", "number")
        self.world.gh("gist", "list")
        self.assertEqual([c["argv"][:2] for c in self.world.calls()],
                         [["issue", "view"], ["gist", "list"]])


if __name__ == "__main__":
    unittest.main()
