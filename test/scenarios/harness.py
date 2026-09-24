"""The scenario harness: the real Tick, repeatedly, against a stateful world
(#87, PRD #85).

A scenario is data (`worlds/*.json`): the starting GitHub state, the committed
`.ralph.yml`, the behaviour profile per agent role, a Tick budget and the
expected terminal state of each Story.  `run_scenario` builds that world --

  * a real git repository with a local **bare remote** (pushes, ancestry and
    branch creation behave as in production),
  * the stateful fake `gh` (`fakes/gh`) seeded from the scenario, and
  * the scripted agent (`fakes/agent`) on PATH as `claude` and `codex` --

then runs the real `ralph --run` entry point up to the Tick budget, stopping
early once every expected Story has reached its state.  Every `gh` and agent
call lands in one call log, which is what a failure prints.

The harness never interprets Ralph's markers itself: the Loop's own parsers do
that.  It only reads the durable outcome -- issue state and labels, pull
requests, the remote.
"""
import datetime
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(os.path.dirname(HERE))
RALPH = os.path.join(REPO_ROOT, "bin", "ralph")
FAKES = os.path.join(HERE, "fakes")
WORLDS = os.path.join(HERE, "worlds")

sys.path.insert(0, os.path.join(REPO_ROOT, "lib"))
import yaml  # noqa: E402

import ralph_agent  # noqa: E402
import ralph_init  # noqa: E402

import invariants  # noqa: E402

GH_FAKE = os.path.join(FAKES, "gh")

# What each expected terminal state means, read off the Story's durable state.
TERMINAL = {
    "passing": lambda issue: issue["state"] == "CLOSED",
    "blocked": lambda issue: "state:blocked" in issue["labels"],
    "needs-human": lambda issue: "needs-human" in issue["labels"],
    "awaiting-bench": lambda issue: "state:awaiting-bench" in issue["labels"],
    # Declared expectations that a Story is *not* finished -- a provider out of
    # session budget makes no progress by design. Exempt from the terminal
    # invariant only because the scenario says so, explicitly.
    "in-progress": lambda issue: "state:in-progress" in issue["labels"],
    "in-review": lambda issue: "state:in-review" in issue["labels"],
}


class ScenarioFailure(AssertionError):
    pass


def load(name_or_path):
    path = name_or_path if os.path.isabs(name_or_path) else os.path.join(
        WORLDS, name_or_path if name_or_path.endswith(".json") else name_or_path + ".json")
    with open(path) as fh:
        spec = json.load(fh)
    spec.setdefault("name", os.path.splitext(os.path.basename(path))[0])
    return spec


def all_worlds():
    return sorted(f[:-5] for f in os.listdir(WORLDS) if f.endswith(".json"))


def variants(spec):
    """The world itself, then one spec per (role or phase, profile) pair its
    `matrix` declares: `{"review": {"empty-usage": {"expect": ..., "known_defect":
    ...}}}` runs the world with that profile swapped in, against the terminal
    state that pair declares."""
    base = {k: v for k, v in spec.items() if k != "matrix"}
    out = [base]
    for slot, profiles in sorted(spec.get("matrix", {}).items()):
        for profile, case in sorted(profiles.items()):
            variant = json.loads(json.dumps(base))
            variant["agents"] = dict(base.get("agents", {}), **{slot: profile})
            variant["name"] = "%s__%s_%s" % (base["name"], slot, profile.replace("-", "_"))
            variant["description"] = case.get("why") or "%s with a %s %s" % (
                base["name"], profile, slot)
            variant["expect"] = case["expect"]
            variant.pop("known_defect", None)
            if case.get("known_defect"):
                variant["known_defect"] = case["known_defect"]
            if "ticks" in case:
                variant["ticks"] = case["ticks"]
            out.append(variant)
    return out


def _git(args, cwd, env):
    return subprocess.run(["git"] + args, cwd=cwd, env=env, check=True,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          text=True).stdout.strip()


def load_capture(path):
    """A `ralph --capture` fixture, resolved relative to `worlds/`."""
    with open(path if os.path.isabs(path) else os.path.join(WORLDS, path)) as fh:
        return json.load(fh)


def rewind(state, moment):
    """Drop everything a capture recorded after *moment* (ISO-8601, UTC).

    A capture is the Story as it is *now*; an incident is replayed from the
    moment it started.  Comment edits cannot be undone (GitHub keeps no
    history in the capture), so a rewound world keeps, say, the ledger as last
    edited -- telemetry, which gates nothing.  The fake's clock moves past the
    last kept moment, so the replay's own writes sort after it.
    """
    def kept(items, field):
        return [i for i in items if (i.get(field) or "") <= moment]
    for issue in list(state["issues"].values()) + list(state["pulls"].values()):
        issue["comments"] = kept(issue["comments"], "createdAt")
        if "reviews" in issue:
            issue["reviews"] = kept(issue["reviews"], "submittedAt")
            issue["reviewComments"] = kept(issue["reviewComments"], "createdAt")
    epoch = datetime.datetime(2026, 1, 1, tzinfo=datetime.timezone.utc)
    at = datetime.datetime.strptime(moment, "%Y-%m-%dT%H:%M:%SZ").replace(
        tzinfo=datetime.timezone.utc)
    state["clock"] = max(state.get("clock", 0), int((at - epoch).total_seconds()))


def initial_state(spec):
    """The fake's state file for the scenario's starting GitHub world.

    A world may start from a capture (`github.capture`); its issues, pull
    request and records are served as captured, with the canonical labels
    added and any `issues`/`ci` the scenario declares layered on top.
    """
    github = spec.get("github", {})
    base = spec["config"].get("branching", {}).get("base", "develop")
    canonical = [{"name": name, "color": color, "description": desc}
                 for name, color, desc in ralph_init.canonical_labels()]
    if github.get("capture"):
        state = load_capture(github["capture"])
        state.pop("format", None)
        state.pop("git", None)
        known = {label["name"] for label in canonical}
        state["labels"] = canonical + [l for l in state["labels"] if l["name"] not in known]
        if "ci" in github:
            state["ci"] = github["ci"]
        if github.get("rewind_to"):
            rewind(state, github["rewind_to"])
        for number, labels in github.get("labels", {}).items():
            state["issues"][str(number)]["labels"] = list(labels)
            state["labels"] += [{"name": n} for n in labels
                                if n not in {l["name"] for l in state["labels"]}]
    else:
        state = {"repo": {"owner": "acme", "name": "target", "defaultBranch": base},
                 "labels": canonical, "issues": {}, "pulls": {}, "statuses": {},
                 "ci": github.get("ci", []), "next_number": 1, "next_id": 1000,
                 "clock": 0}
    for issue in github.get("issues", []):
        number = issue["number"]
        state["issues"][str(number)] = {
            "number": number, "title": issue["title"], "body": issue["body"],
            "state": issue.get("state", "OPEN"), "labels": list(issue["labels"]),
            "comments": [],
            "url": "https://github.com/acme/target/issues/%d" % number}
        state["next_number"] = max(state["next_number"], number + 1)
    return state


def translate_oids(value, mapping):
    """*value* with every captured commit id -- whole or abbreviated to 7+
    characters -- replaced by its replay counterpart, the same length.

    Plain text substitution over the JSON: the harness never interprets the
    Loop's markers, it only makes the ids they carry point at real commits.
    """
    if not mapping:
        return value

    def swap(match):
        token = match.group(0)
        hits = {new for old, new in mapping.items() if old.startswith(token)}
        return next(iter(hits))[:len(token)] if len(hits) == 1 else token

    return json.loads(re.sub(r"\b[0-9a-f]{7,40}\b", swap, json.dumps(value)))


class World:
    """One scenario's temp directory: remote, checkout, fakes, state, log."""

    def __init__(self, spec):
        self.spec = spec
        self.root = tempfile.mkdtemp(prefix="ralph-scenario-")
        self.remote = os.path.join(self.root, "remote.git")
        self.checkout = os.path.join(self.root, "checkout")
        self.bin = os.path.join(self.root, "bin")
        self.state_path = os.path.join(self.root, "github.json")
        self.log_path = os.path.join(self.root, "calls.jsonl")
        self.env = self._environment()
        self._build()

    def _environment(self):
        env = dict(os.environ)
        # A scenario run from inside a live Ralph iteration inherits that
        # iteration's knobs, and a provider override would launch a *real*
        # agent (AGENTS.md, tick-harness gotcha).  None of them may leak in.
        for name in list(env):
            if name.startswith(("RALPH_", "SCENARIO_", "GIT_", "GH_")):
                del env[name]
        for adapter in ralph_agent.PROVIDERS.values():
            env.pop(adapter.binary_env, None)
        env.update({
            "PATH": self.bin + os.pathsep + env.get("PATH", ""),
            "SCENARIO_STATE": self.state_path,
            "SCENARIO_LOG": self.log_path,
            "SCENARIO_REMOTE": self.remote,
            "SCENARIO_RALPH": RALPH,
            "SCENARIO_AGENTS": json.dumps(self.spec.get("agents", {})),
            "GIT_AUTHOR_NAME": "Scenario", "GIT_AUTHOR_EMAIL": "scenario@example.invalid",
            "GIT_COMMITTER_NAME": "Scenario",
            "GIT_COMMITTER_EMAIL": "scenario@example.invalid",
            "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_TERMINAL_PROMPT": "0",
        })
        return env

    def _build(self):
        os.makedirs(self.bin)
        os.symlink(GH_FAKE, os.path.join(self.bin, "gh"))
        for adapter in ralph_agent.PROVIDERS.values():
            os.symlink(os.path.join(FAKES, "agent"),
                       os.path.join(self.bin, adapter.default_binary))
        base = self.spec["config"].get("branching", {}).get("base", "develop")
        _git(["init", "-q", "--bare", "-b", base, self.remote], self.root, self.env)
        _git(["clone", "-q", self.remote, self.checkout], self.root, self.env)
        _git(["checkout", "-q", "-b", base], self.checkout, self.env)
        with open(os.path.join(self.checkout, ".ralph.yml"), "w") as fh:
            yaml.safe_dump(self.spec["config"], fh, sort_keys=False)
        with open(os.path.join(self.checkout, "README.md"), "w") as fh:
            fh.write("Scenario target repository: %s\n" % self.spec["name"])
        _git(["add", "-A"], self.checkout, self.env)
        _git(["commit", "-q", "-m", "chore: scenario world"], self.checkout, self.env)
        _git(["push", "-q", "-u", "origin", base], self.checkout, self.env)
        state = initial_state(self.spec)
        self.oid_map = {}
        capture = self.spec.get("github", {}).get("capture")
        if capture:
            git = load_capture(capture).get("git")
            if git:
                self.oid_map = self._replay_chain(git, state)
                state = translate_oids(state, self.oid_map)
        with open(self.state_path, "w") as fh:
            json.dump(state, fh, indent=1)
        open(self.log_path, "w").close()

    def _replay_chain(self, git, state):
        """Rebuild a captured pull request's commit chain on the scenario base.

        Content is synthetic; the *shape* is the capture's: the captured base
        (and merge base) become this world's base commit, each captured commit
        one commit on top of it, pushed where GitHub would have it -- the head
        branch while the pull request is open, and `refs/pull/N/head` always.
        """
        base_oid = _git(["rev-parse", "HEAD"], self.checkout, self.env)
        mapping = {oid: base_oid for oid in (git.get("base"), git.get("merge_base")) if oid}
        (pr,) = state["pulls"].values()
        for i, commit in enumerate(git["commits"]):
            path = os.path.join(self.checkout, "replay-%d.txt" % i)
            with open(path, "w") as fh:
                fh.write("captured commit %s\n" % commit["oid"])
            _git(["add", path], self.checkout, self.env)
            _git(["commit", "-q", "-m", commit.get("subject") or "replay"],
                 self.checkout, self.env)
            mapping[commit["oid"]] = _git(["rev-parse", "HEAD"], self.checkout, self.env)
        refs = ["HEAD:refs/pull/%s/head" % pr["number"]]
        if pr["state"] == "OPEN":
            refs.append("HEAD:refs/heads/%s" % pr["headRefName"])
            # The orchestration checkout that ran the incident had the Story
            # branch locally too (the agent committed on it), and the response
            # round reads it there.
            _git(["branch", "-f", pr["headRefName"], "HEAD"], self.checkout, self.env)
        if _git(["ls-remote", "origin", "refs/heads/%s" % pr["baseRefName"]],
                self.checkout, self.env) == "":
            refs.append("%s:refs/heads/%s" % (base_oid, pr["baseRefName"]))
        _git(["push", "-q", "origin"] + refs, self.checkout, self.env)
        base = self.spec["config"].get("branching", {}).get("base", "develop")
        _git(["checkout", "-q", "--detach", base_oid], self.checkout, self.env)
        _git(["checkout", "-q", "-B", base, base_oid], self.checkout, self.env)
        return mapping

    # --- reading the world back ---------------------------------------------

    def state(self):
        with open(self.state_path) as fh:
            return json.load(fh)

    def calls(self):
        with open(self.log_path) as fh:
            return [json.loads(line) for line in fh if line.strip()]

    def gh(self, *args):
        """Run the fake `gh` against this world (for tests of the fake itself)."""
        return subprocess.run([GH_FAKE] + list(args), cwd=self.checkout, env=self.env,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

    def tick(self, timeout=300):
        return subprocess.run([RALPH, "--run"], cwd=self.checkout, env=self.env,
                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                              text=True, timeout=timeout)

    def cleanup(self):
        shutil.rmtree(self.root, ignore_errors=True)


def trace(calls, last=25):
    lines = []
    for call in calls[-last:]:
        if call["tool"] == "gh":
            argv = " ".join(a if len(a) < 60 else a[:57] + "..." for a in call["argv"])
            lines.append("  gh %s -> %s%s" % (argv, call.get("rc"),
                                               " UNEMULATED" if call.get("unemulated") else ""))
        else:
            lines.append("  %s[%s/%s] #%s -> %s%s" % (
                call["tool"], call.get("phase"), call["profile"], call.get("story"),
                call.get("rc"), (" " + call["error"]) if call.get("error") else ""))
    return "\n".join(lines)


def check_calls(world, tick_no, output):
    """Fail on any call the fakes could not answer: the scenario is unsound."""
    for call in world.calls():
        problem = call.get("unemulated") or call.get("error")
        if problem:
            raise ScenarioFailure(
                "tick %d: %s call failed in the harness: %s\n  argv: %s\n"
                "last calls:\n%s\ntick output:\n%s" % (
                    tick_no, call["tool"], problem, call["argv"],
                    trace(world.calls()), output[-3000:]))


def reached(world, expect):
    issues = world.state()["issues"]
    return {number: TERMINAL[want](issues[str(number)])
            for number, want in expect.items()}


class Result:
    def __init__(self, world, ticks, outputs):
        self.world = world
        self.ticks = ticks
        self.outputs = outputs


def run_scenario(spec, keep=False):
    """Run *spec* for its Tick budget; return a Result or raise ScenarioFailure."""
    world = World(spec)
    watch = invariants.Watch(world)
    watch.check()           # the starting branch heads are the first baseline
    outputs = []
    try:
        expect = spec["expect"]
        for tick_no in range(1, spec["ticks"] + 1):
            proc = world.tick()
            outputs.append(proc.stdout)
            check_calls(world, tick_no, proc.stdout)
            if proc.returncode != 0:
                raise ScenarioFailure("tick %d exited %d\n%s\nlast calls:\n%s" % (
                    tick_no, proc.returncode, proc.stdout[-3000:], trace(world.calls())))
            done = all(reached(world, expect).values())
            violations = watch.check(final=done or tick_no == spec["ticks"])
            if violations:
                raise ScenarioFailure(invariants.report(tick_no, violations,
                                                        trace(world.calls())))
            if done:
                return Result(world, tick_no, outputs)
        issues = world.state()["issues"]
        raise ScenarioFailure(
            "%s: not every Story reached its expected state within %d ticks:\n%s\n"
            "last calls:\n%s\nlast tick output:\n%s" % (
                spec["name"], spec["ticks"],
                "\n".join("  #%s expected %s, is %s %s" % (
                    n, want, issues[str(n)]["state"], issues[str(n)]["labels"])
                    for n, want in expect.items()),
                trace(world.calls()), outputs[-1][-3000:] if outputs else ""))
    finally:
        if not keep:
            world.cleanup()
