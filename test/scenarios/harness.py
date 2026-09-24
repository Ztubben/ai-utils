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
import json
import os
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

GH_FAKE = os.path.join(FAKES, "gh")

# What each expected terminal state means, read off the Story's durable state.
TERMINAL = {
    "passing": lambda issue: issue["state"] == "CLOSED",
    "blocked": lambda issue: "state:blocked" in issue["labels"],
    "needs-human": lambda issue: "needs-human" in issue["labels"],
    "awaiting-bench": lambda issue: "state:awaiting-bench" in issue["labels"],
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


def _git(args, cwd, env):
    return subprocess.run(["git"] + args, cwd=cwd, env=env, check=True,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          text=True).stdout.strip()


def initial_state(spec):
    """The fake's state file for the scenario's starting GitHub world."""
    github = spec.get("github", {})
    base = spec["config"].get("branching", {}).get("base", "develop")
    state = {"repo": {"owner": "acme", "name": "target", "defaultBranch": base},
             "labels": [{"name": name, "color": color, "description": desc}
                        for name, color, desc in ralph_init.canonical_labels()],
             "issues": {}, "pulls": {}, "statuses": {},
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
        with open(self.state_path, "w") as fh:
            json.dump(initial_state(self.spec), fh, indent=1)
        open(self.log_path, "w").close()

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
                call["tool"], call["role"], call["profile"], call.get("issue"),
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
            if all(reached(world, expect).values()):
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
