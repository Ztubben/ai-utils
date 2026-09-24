"""Loop invariants, checked after every Tick of every scenario (#88, PRD #85).

A fixed, named list.  No scenario spells any of them out: the runner evaluates
all of them after each Tick against the fake GitHub's state, the call log and
the bare remote, so every new scenario gets every invariant for free.

  one-result-per-round      at most one Review Agent result per (head, round)
  one-response-per-round    at most one Implementation Agent response per
                            (head, round) -- wherever it was recorded
  invocation-limit          model invocations per Story stay within
                            limits.max_attempts + 2 * review.max_rounds
                            (one implementation run per Attempt, one review
                            and one response per Negotiation Round)
  branch-only-grows         every branch head descends from the head it had
                            after the previous Tick
  empty-runs-never-count    a run with no output or zero usage never backs a
                            recorded result, a recorded response or a
                            promotion into review
  prompt-contract           every prompt an agent received carries its phase's
                            required instructions and none of its forbidden
                            ones (`contracts.py`)
  terminal-within-budget    by the scenario's last Tick every Story is Passing
                            (closed), state:blocked, needs-human or
                            state:awaiting-bench

Records are read through the Loop's own parsers (`ralph_review`), never by
re-implementing its markers here.
"""
import json
import os
import subprocess
import sys
from collections import Counter

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(HERE)), "lib"))

import contracts  # noqa: E402
import ralph_config  # noqa: E402
import ralph_review  # noqa: E402
import ralph_review_wait  # noqa: E402

PARKED_LABELS = ("state:blocked", "needs-human", "state:awaiting-bench")
# Expected states a scenario may declare for a Story that is, by design, not
# finished; the terminal invariant exempts exactly those Stories.
NON_TERMINAL = ("in-progress", "in-review")


class Violation:
    def __init__(self, invariant, detail, records=()):
        self.invariant = invariant
        self.detail = detail
        self.records = list(records)

    def __repr__(self):  # pragma: no cover - debugging aid
        return "Violation(%s: %s)" % (self.invariant, self.detail)


# --- what one Tick left behind ------------------------------------------------

class Observation:
    """One read of the world after a Tick; the invariants only look, never act."""

    def __init__(self, world, previous_heads, final, baseline=None):
        self.state = world.state()
        self.calls = world.calls()
        self.remote = world.remote
        self.final = final
        self.previous_heads = previous_heads
        self.expect = world.spec.get("expect", {})
        # Records the world *started* with -- a capture's production history --
        # were made by runs this scenario never saw, so they back nothing here.
        self.baseline = baseline or Counter()
        self.heads = self._branch_heads()
        config = ralph_config.load_and_validate(
            os.path.join(world.checkout, ".ralph.yml")).config
        self.max_attempts = config["limits"]["max_attempts"]
        self.max_rounds = ralph_review_wait.WaitPolicy.from_config(config).max_rounds

    def _git(self, *args):
        return subprocess.run(["git", "--git-dir", self.remote] + list(args),
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

    def _branch_heads(self):
        out = self._git("for-each-ref", "--format=%(refname:short) %(objectname)",
                        "refs/heads").stdout
        return dict(line.split() for line in out.splitlines() if line.strip())

    def is_ancestor(self, old, new):
        return self._git("merge-base", "--is-ancestor", old, new).returncode == 0

    def commits(self):
        """Every commit the remote ever received, reachable or not."""
        out = self._git("cat-file", "--batch-all-objects",
                        "--batch-check=%(objectname) %(objecttype)").stdout
        return [line.split()[0] for line in out.splitlines() if line.endswith(" commit")]

    def stories(self):
        return [issue for issue in self.state["issues"].values()
                if any(label.startswith("type:") for label in issue["labels"])]

    def all_pr_comments(self):
        return [c for pr in self.state["pulls"].values() for c in pr["comments"]]

    def agent_calls(self, **match):
        return [c for c in self.calls if c["tool"] != "gh"
                and all(c.get(k) == v for k, v in match.items())]


def is_empty(call):
    usage = call.get("usage") or {}
    counts = [v for v in usage.values() if isinstance(v, int)]
    return not (call.get("text") or "").strip() or not any(counts)


def _adds(argv, label):
    return any(a == "--add-label" and label in b.split(",")
               for a, b in zip(argv, argv[1:]))


def _per_round(records):
    return Counter(record.get("round") for record in records)


# --- the invariants -----------------------------------------------------------

def one_result_per_round(obs):
    found = []
    for story in obs.stories():
        for head in obs.commits():
            for round_no, n in _per_round(
                    ralph_review.result_records(story["comments"], head)).items():
                if n > 1:
                    found.append(Violation(
                        "one-result-per-round",
                        "#%s head %s round %s has %d Review Agent results; limit 1"
                        % (story["number"], head[:7], round_no, n),
                        ralph_review.result_records(story["comments"], head)))
    return found


def one_response_per_round(obs):
    found = []
    for story in obs.stories():
        comments = story["comments"] + obs.all_pr_comments()
        for head in obs.commits():
            records = ralph_review.response_records(comments, head)
            for round_no, n in _per_round(records).items():
                if n > 1:
                    found.append(Violation(
                        "one-response-per-round",
                        "#%s head %s round %s answered %d times; limit 1"
                        % (story["number"], head[:7], round_no, n), records))
    return found


def invocation_limit(obs):
    limit = obs.max_attempts + 2 * obs.max_rounds
    found = []
    for story in obs.stories():
        calls = obs.agent_calls(story=story["number"])
        if len(calls) > limit:
            found.append(Violation(
                "invocation-limit",
                "#%s launched %d model invocations; limit %d "
                "(max_attempts %d + 2 x max_rounds %d)"
                % (story["number"], len(calls), limit, obs.max_attempts, obs.max_rounds),
                [{k: c.get(k) for k in ("tool", "phase", "profile", "head", "round")}
                 for c in calls]))
    return found


def branch_only_grows(obs):
    found = []
    for branch, old in obs.previous_heads.items():
        new = obs.heads.get(branch)
        if new and new != old and not obs.is_ancestor(old, new):
            found.append(Violation(
                "branch-only-grows",
                "%s moved from %s to %s, which does not descend from it"
                % (branch, old[:7], new[:7]), [{"branch": branch, "was": old, "now": new}]))
    return found


def empty_runs_never_count(obs):
    found = []

    def backed(phase, head, round_no):
        return len([c for c in obs.agent_calls(phase=phase, head=head, round=round_no)
                    if not is_empty(c)])

    for story in obs.stories():
        pr_comments = obs.all_pr_comments()
        for head in obs.commits():
            for kind, phase, records in (
                    ("review result", "review",
                     ralph_review.result_records(story["comments"], head)),
                    ("response", "response",
                     ralph_review.response_records(story["comments"] + pr_comments, head))):
                for round_no, n in _per_round(records).items():
                    n -= obs.baseline[(kind, story["number"], head, round_no)]
                    if n > backed(phase, head, round_no):
                        found.append(Violation(
                            "empty-runs-never-count",
                            "#%s head %s round %s has %d %s record(s) but only %d "
                            "non-empty %s run(s)" % (story["number"], head[:7], round_no,
                                                     n, kind, backed(phase, head, round_no),
                                                     phase), records))
        promotions = [c for c in obs.calls if c["tool"] == "gh"
                      and c["argv"][:3] == ["issue", "edit", str(story["number"])]
                      and _adds(c["argv"], "state:in-review") and c.get("rc") == 0]
        runs = [c for c in obs.agent_calls(story=story["number"], phase="iteration")
                if not is_empty(c)]
        if len(promotions) > len(runs):
            found.append(Violation(
                "empty-runs-never-count",
                "#%s was promoted into review %d time(s) but only %d non-empty "
                "iteration(s) ran" % (story["number"], len(promotions), len(runs)),
                [c["argv"] for c in promotions]))
    return found


def record_counts(obs):
    """(kind, story, head, round) -> how many records of that kind exist."""
    counts = Counter()
    pr_comments = obs.all_pr_comments()
    for story in obs.stories():
        for head in obs.commits():
            for kind, records in (
                    ("review result", ralph_review.result_records(story["comments"], head)),
                    ("response", ralph_review.response_records(
                        story["comments"] + pr_comments, head))):
                for round_no, n in _per_round(records).items():
                    counts[(kind, story["number"], head, round_no)] += n
    return counts


def prompt_contract(obs):
    found = []
    for call in obs.agent_calls():
        for kind, phrase in contracts.breaches(call.get("phase"), call.get("prompt") or ""):
            found.append(Violation(
                "prompt-contract",
                "#%s %s prompt (%s round %s): %s %r" % (
                    call.get("story"), call.get("phase"), call["tool"], call.get("round"),
                    kind, phrase),
                [{"phase": call.get("phase"), "argv": call["argv"]}]))
    return found


def terminal_within_budget(obs):
    if not obs.final:
        return []
    declared = {str(n) for n, want in obs.expect.items() if want in NON_TERMINAL}
    return [Violation("terminal-within-budget",
                      "#%s is still %s %s at the end of the Tick budget"
                      % (story["number"], story["state"], story["labels"]),
                      [{"number": story["number"], "labels": story["labels"]}])
            for story in obs.stories()
            if str(story["number"]) not in declared
            and story["state"] != "CLOSED"
            and not any(label in story["labels"] for label in PARKED_LABELS)]


INVARIANTS = [
    ("one-result-per-round", one_result_per_round),
    ("one-response-per-round", one_response_per_round),
    ("invocation-limit", invocation_limit),
    ("branch-only-grows", branch_only_grows),
    ("empty-runs-never-count", empty_runs_never_count),
    ("prompt-contract", prompt_contract),
    ("terminal-within-budget", terminal_within_budget),
]


class Watch:
    """Evaluates every invariant after each Tick, remembering branch heads."""

    def __init__(self, world):
        self.world = world
        self.heads = {}
        self.baseline = None

    def check(self, final=False):
        obs = Observation(self.world, self.heads, final, self.baseline)
        if self.baseline is None:
            self.baseline = record_counts(obs)
            obs.baseline = self.baseline
        violations = [v for _name, invariant in INVARIANTS for v in invariant(obs)]
        self.heads = obs.heads
        return violations


def report(tick_no, violations, trace):
    lines = []
    for v in violations:
        lines.append("tick %d: invariant %s violated: %s" % (tick_no, v.invariant, v.detail))
        for record in v.records[:5]:
            lines.append("  record: %s" % json.dumps(record, sort_keys=True)[:400])
    lines += ["last calls:", trace]
    return "\n".join(lines)
