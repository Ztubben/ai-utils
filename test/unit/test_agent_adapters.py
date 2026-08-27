"""Provider-neutral agent adapters (#45, PRD #42).

One adapter contract launches either role, so the orchestration that drives the
Implementation Agent and the Review Agent never names a provider. Each adapter
knows three things and nothing else: how to spell its provider's launch command
for the configured model identity, how to hand it a fresh process carrying no
inherited session state, and how to classify what came back -- normal
completion, session exhaustion, or infrastructure failure.

These tests drive the seam directly (with the process launch injected) and the
exit-code contract `bin/ralph.sh` reads. The tick end-to-end against fake
provider binaries on PATH lives in test_orchestrate.py, next to the harness.
"""
import os
import re
import sys
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(REPO_ROOT, "lib"))

import ralph_agent  # noqa: E402
import ralph_config  # noqa: E402

FIXTURES = os.path.join(REPO_ROOT, "test", "fixtures", "config", "valid")
RALPH_SH = os.path.join(REPO_ROOT, "bin", "ralph.sh")


def config(name):
    result = ralph_config.load_and_validate(os.path.join(FIXTURES, name))
    assert result.ok, result.errors
    return result.config


class Recorder:
    """Stands in for the process launch: records the call, returns a canned
    (exit code, combined output)."""

    def __init__(self, rc=0, output=""):
        self.calls = []
        self.rc = rc
        self.output = output

    def __call__(self, argv, prompt, env):
        self.calls.append({"argv": list(argv), "prompt": prompt, "env": dict(env)})
        return self.rc, self.output

    @property
    def argv(self):
        return self.calls[-1]["argv"]

    @property
    def env(self):
        return self.calls[-1]["env"]


class OneAdapterInterfaceLaunchesBothRoles(unittest.TestCase):
    """AC: both roles are launched through one adapter interface; the role
    orchestration contains no provider conditionals."""

    def test_every_provider_in_the_catalog_has_an_adapter(self):
        # The schema's provider enum and the adapter registry are the same set:
        # a config that validates can always be launched.
        schema_providers = set(ralph_config.provider_enum())
        self.assertEqual(set(ralph_agent.PROVIDERS), schema_providers)

    def test_every_adapter_implements_the_one_contract(self):
        for provider, cls in ralph_agent.PROVIDERS.items():
            self.assertTrue(issubclass(cls, ralph_agent.AgentAdapter), provider)
            self.assertEqual(cls.provider, provider)

    def test_both_roles_are_launched_through_the_same_call(self):
        # implementation -> claude, review -> codex in this catalog, yet the
        # caller says only which role it wants: same call, same shape back.
        cfg = config("models.yml")
        seen = []
        for role in ("implementation", "review"):
            rec = Recorder(output="hello")
            outcome, errors = ralph_agent.launch_role(cfg, role, "prompt", run=rec)
            self.assertEqual(errors, [])
            self.assertEqual(outcome.kind, ralph_agent.NORMAL)
            self.assertEqual(outcome.output, "hello")
            self.assertEqual(rec.calls[-1]["prompt"], "prompt")
            seen.append((outcome.provider, rec.argv[0]))
        self.assertEqual([p for p, _ in seen], ["claude", "codex"])
        self.assertNotEqual(seen[0][1], seen[1][1])

    def test_an_unknown_role_is_refused(self):
        outcome, errors = ralph_agent.launch_role(config("models.yml"), "auditor",
                                                  "prompt", run=Recorder())
        self.assertIsNone(outcome)
        self.assertTrue(errors)

    def test_a_role_override_selects_the_other_provider(self):
        rec = Recorder()
        outcome, errors = ralph_agent.launch_role(
            config("models.yml"), "implementation", "prompt",
            implementation="codex-review", review="claude-impl", run=rec)
        self.assertEqual(errors, [])
        self.assertEqual(outcome.provider, "codex")

    def test_role_independence_is_still_enforced_at_launch(self):
        # #44's invariant survives: a pair collapsing to one model identity is
        # refused rather than quietly launched.
        outcome, errors = ralph_agent.launch_role(
            config("models.yml"), "implementation", "prompt",
            implementation="claude-impl", review="claude-impl", run=Recorder())
        self.assertIsNone(outcome)
        self.assertTrue(any("same model identity" in e for e in errors), errors)

    def test_orchestration_names_no_provider(self):
        # The tick drives the adapter seam only; a provider name appearing in
        # bin/ralph.sh means orchestration grew a provider conditional again.
        with open(RALPH_SH) as fh:
            offenders = [ln.strip() for ln in fh
                         if re.search(r"claude|codex|gpt-|anthropic|openai",
                                      ln, re.IGNORECASE)]
        self.assertEqual(offenders, [])

    def test_a_target_repository_without_a_catalog_still_launches(self):
        # `models:` is optional (#44), so a config that never opted in keeps
        # ticking on the compatibility default rather than refusing.
        rec = Recorder()
        outcome, errors = ralph_agent.launch_role(config("minimal.yml"),
                                                  "implementation", "prompt",
                                                  run=rec)
        self.assertEqual(errors, [])
        self.assertEqual(outcome.provider, "claude")
        self.assertNotIn("--model", rec.argv)


class AdaptersLaunchTheirModelInAFreshProcess(unittest.TestCase):
    """AC: the Claude and Codex adapters each launch their configured model
    identity in a fresh process."""

    def launch(self, provider, model, **env):
        adapter = ralph_agent.PROVIDERS[provider](model)
        rec = Recorder()
        old = dict(os.environ)
        os.environ.update(env)
        try:
            adapter.launch("prompt", run=rec)
        finally:
            os.environ.clear()
            os.environ.update(old)
        return adapter, rec

    def test_claude_launches_the_configured_model_identity(self):
        _, rec = self.launch("claude", "claude-opus-5")
        self.assertEqual(os.path.basename(rec.argv[0]), "claude")
        self.assertIn("--model", rec.argv)
        self.assertEqual(rec.argv[rec.argv.index("--model") + 1], "claude-opus-5")

    def test_codex_launches_the_configured_model_identity(self):
        _, rec = self.launch("codex", "gpt-5-codex")
        self.assertEqual(os.path.basename(rec.argv[0]), "codex")
        self.assertIn("--model", rec.argv)
        self.assertEqual(rec.argv[rec.argv.index("--model") + 1], "gpt-5-codex")

    def test_no_adapter_resumes_a_previous_session(self):
        # A fresh process means the prompt is the whole of the context: no
        # resume/continue/fork flag may be spelled by either adapter.
        for provider, model in (("claude", "claude-opus-5"), ("codex", "gpt-5-codex")):
            _, rec = self.launch(provider, model)
            joined = " ".join(rec.argv)
            for flag in ("--resume", "--continue", "-c ", "fork", "--last"):
                self.assertNotIn(flag, joined, "%s: %s" % (provider, joined))

    def test_inherited_session_state_is_stripped_from_the_environment(self):
        # A tick launched from inside a provider session must not leak that
        # session into the child (ai-utils dogfoods its own loop).
        _, rec = self.launch("claude", "claude-opus-5", CLAUDECODE="1",
                             CLAUDE_CODE_ENTRYPOINT="cli",
                             CLAUDE_SESSION_ID="abc",
                             ANTHROPIC_API_KEY="keep-me")
        self.assertNotIn("CLAUDECODE", rec.env)
        self.assertNotIn("CLAUDE_CODE_ENTRYPOINT", rec.env)
        self.assertNotIn("CLAUDE_SESSION_ID", rec.env)
        self.assertEqual(rec.env.get("ANTHROPIC_API_KEY"), "keep-me")

    def test_codex_session_state_is_stripped_from_the_environment(self):
        _, rec = self.launch("codex", "gpt-5-codex", CODEX_SESSION_ID="abc",
                             CODEX_THREAD_ID="def", CODEX_HOME="/keep")
        self.assertNotIn("CODEX_SESSION_ID", rec.env)
        self.assertNotIn("CODEX_THREAD_ID", rec.env)
        self.assertEqual(rec.env.get("CODEX_HOME"), "/keep")

    def test_the_prompt_is_handed_over_on_stdin(self):
        for provider, model in (("claude", "claude-opus-5"), ("codex", "gpt-5-codex")):
            adapter = ralph_agent.PROVIDERS[provider](model)
            rec = Recorder()
            adapter.launch("the whole story", run=rec)
            self.assertEqual(rec.calls[-1]["prompt"], "the whole story")

    def test_the_binary_is_overridable_per_provider(self):
        _, rec = self.launch("claude", "claude-opus-5", RALPH_CLAUDE="/opt/claude")
        self.assertEqual(rec.argv[0], "/opt/claude")
        _, rec = self.launch("codex", "gpt-5-codex", RALPH_CODEX="/opt/codex")
        self.assertEqual(rec.argv[0], "/opt/codex")


class AdaptersClassifyTheOutcome(unittest.TestCase):
    """AC: each adapter classifies normal completion, session exhaustion, and
    infrastructure failure distinctly."""

    def outcome(self, provider, rc=0, output="", **env):
        adapter = ralph_agent.PROVIDERS[provider]("a-model")
        old = dict(os.environ)
        os.environ.update(env)
        try:
            return adapter.launch("prompt", run=Recorder(rc=rc, output=output))
        finally:
            os.environ.clear()
            os.environ.update(old)

    def test_the_three_kinds_are_distinct(self):
        kinds = {ralph_agent.NORMAL, ralph_agent.SESSION_EXHAUSTED,
                 ralph_agent.INFRASTRUCTURE_FAILURE}
        self.assertEqual(len(kinds), 3)
        exits = {ralph_agent.EXIT_NORMAL, ralph_agent.EXIT_SESSION_EXHAUSTED,
                 ralph_agent.EXIT_INFRASTRUCTURE_FAILURE}
        self.assertEqual(len(exits), 3)

    def test_a_clean_exit_is_normal_completion(self):
        for provider in ralph_agent.PROVIDERS:
            out = self.outcome(provider, rc=0, output="done")
            self.assertEqual(out.kind, ralph_agent.NORMAL, provider)
            self.assertEqual(out.cli_exit, ralph_agent.EXIT_NORMAL)
            self.assertEqual(out.output, "done")

    def test_the_session_limit_marker_is_session_exhaustion(self):
        for provider in ralph_agent.PROVIDERS:
            out = self.outcome(provider, rc=1,
                               output="Claude usage limit reached; try later")
            self.assertEqual(out.kind, ralph_agent.SESSION_EXHAUSTED, provider)
            self.assertEqual(out.cli_exit, ralph_agent.EXIT_SESSION_EXHAUSTED)

    def test_the_session_limit_exit_code_is_session_exhaustion(self):
        out = self.outcome("claude", rc=91)
        self.assertEqual(out.kind, ralph_agent.SESSION_EXHAUSTED)

    def test_the_session_limit_signals_are_overridable(self):
        out = self.outcome("codex", rc=42, RALPH_SESSION_LIMIT_EXIT="42")
        self.assertEqual(out.kind, ralph_agent.SESSION_EXHAUSTED)
        out = self.outcome("codex", rc=1, output="out of quota",
                           RALPH_SESSION_LIMIT_MARKER="out of quota")
        self.assertEqual(out.kind, ralph_agent.SESSION_EXHAUSTED)

    def test_any_other_failure_is_an_infrastructure_failure(self):
        for provider in ralph_agent.PROVIDERS:
            out = self.outcome(provider, rc=1, output="connection reset by peer")
            self.assertEqual(out.kind, ralph_agent.INFRASTRUCTURE_FAILURE, provider)
            self.assertEqual(out.cli_exit, ralph_agent.EXIT_INFRASTRUCTURE_FAILURE)
            self.assertEqual(out.exit_code, 1)

    def test_a_missing_provider_binary_is_an_infrastructure_failure(self):
        def explode(argv, prompt, env):
            raise OSError(2, "No such file or directory")

        adapter = ralph_agent.PROVIDERS["codex"]("gpt-5-codex")
        out = adapter.launch("prompt", run=explode)
        self.assertEqual(out.kind, ralph_agent.INFRASTRUCTURE_FAILURE)
        self.assertIn("codex", out.output)

    def test_session_exhaustion_outranks_infrastructure_failure(self):
        # A truncated run exits non-zero; it is exhaustion (checkpoint), never a
        # crash, so the story is resumed rather than counted as an Attempt.
        out = self.outcome("claude", rc=91, output="usage limit reached")
        self.assertEqual(out.kind, ralph_agent.SESSION_EXHAUSTED)


class TheExitCodeContractIsSharedWithTheTick(unittest.TestCase):
    """`bin/ralph.sh` cannot import Python: the two spellings of the adapter's
    exit-code contract must not drift."""

    def bash_constant(self, name):
        with open(RALPH_SH) as fh:
            match = re.search(r"^%s=(\d+)" % name, fh.read(), re.MULTILINE)
        self.assertIsNotNone(match, "bin/ralph.sh must define %s" % name)
        return int(match.group(1))

    def test_session_exhaustion_code_matches(self):
        self.assertEqual(self.bash_constant("RC_SESSION_LIMIT"),
                         ralph_agent.EXIT_SESSION_EXHAUSTED)

    def test_infrastructure_failure_code_matches(self):
        self.assertEqual(self.bash_constant("RC_INFRA_FAILURE"),
                         ralph_agent.EXIT_INFRASTRUCTURE_FAILURE)


if __name__ == "__main__":
    unittest.main()
