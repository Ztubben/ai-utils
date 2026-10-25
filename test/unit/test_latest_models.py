"""Moving model selectors follow provider catalogs without pinning a release."""
import json
import os
import subprocess
import sys
import unittest
from unittest.mock import patch

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, "lib"))
import ralph_agent
import ralph_config
import ralph_models


def catalog(*slugs):
    return {"models": [{"slug": slug, "visibility": "list"} for slug in slugs]}


class LatestSolSelection(unittest.TestCase):
    def test_numeric_order_across_major_and_minor_versions(self):
        self.assertEqual(ralph_agent.latest_sol(catalog(
            "gpt-5.6-sol", "gpt-6.9-sol", "gpt-6.10-sol", "gpt-6-sol")),
            "gpt-6.10-sol")
        self.assertEqual(ralph_agent.latest_sol(catalog(
            "gpt-6.10-sol", "gpt-7-sol")), "gpt-7-sol")

    def test_hidden_preview_other_families_and_malformed_entries_are_excluded(self):
        data = catalog("gpt-6-sol", "gpt-7-sol-preview", "gpt-9-astra")
        data["models"] += [{"slug": "gpt-8-sol", "visibility": "hide"},
                           {"slug": "gpt-9-sol"}, {"slug": 123}, None]
        self.assertEqual(ralph_agent.latest_sol(data), "gpt-6-sol")

    def test_unusable_catalogs_fail_instead_of_guessing_a_model(self):
        for data in (None, [], {}, {"models": {}}, catalog("gpt-6-astra")):
            with self.subTest(data=data), self.assertRaises(ValueError):
                ralph_agent.latest_sol(data)


class ExistingAssignments(unittest.TestCase):
    def setUp(self):
        self.catalog = {
            "claude": ralph_models.ModelProfile("claude", "claude", "opus"),
            "codex": ralph_models.ModelProfile("codex", "codex", "sol")}

    def test_existing_assignments_follow_an_explicitly_allowlisted_family(self):
        for model, selector in (("claude-opus-5", "opus"), ("gpt-6.1-sol", "sol")):
            self.assertEqual(ralph_models.profile_for_model(self.catalog, model).model,
                             selector)

    def test_explicit_versioned_entries_still_pin_existing_assignments(self):
        pinned = ralph_models.ModelProfile("pinned", "codex", "gpt-6-sol")
        self.catalog["pinned"] = pinned
        self.assertIs(ralph_models.profile_for_model(self.catalog, "gpt-6-sol"), pinned)

    def test_selectors_do_not_allow_other_families_or_preview_ids(self):
        for model in ("claude-sonnet-5", "gpt-6-astra", "gpt-7-sol-preview"):
            self.assertIsNone(ralph_models.profile_for_model(self.catalog, model))


class MovingSelectorLaunch(unittest.TestCase):
    def launch(self, adapter, payload, returncode=0):
        calls = []

        def run(argv, prompt, env):
            calls.append((argv, prompt, env))
            return 0, "finished"

        result = subprocess.CompletedProcess([], returncode, payload, "")
        with patch.object(ralph_agent.subprocess, "run", return_value=result) as lookup:
            outcome = adapter.launch("the prompt", run=run)
        return outcome, calls, lookup

    def test_sol_resolves_for_both_roles_and_records_the_concrete_version(self):
        for role in ralph_agent.ROLES:
            with self.subTest(role=role), patch.dict(os.environ, {"RALPH_CODEX": "/fake/codex"}):
                adapter = ralph_agent.CodexAdapter("sol", role=role)
                outcome, calls, lookup = self.launch(
                    adapter, json.dumps(catalog("gpt-6-sol", "gpt-6.1-sol")))
                self.assertEqual(outcome.model, "gpt-6.1-sol")
                self.assertEqual(outcome.kind, ralph_agent.NORMAL)
                self.assertEqual(adapter.model, "sol")
                self.assertEqual(lookup.call_args.args[0], ["/fake/codex", "debug", "models"])
                self.assertEqual(lookup.call_args.kwargs["timeout"], 30)
                argv, prompt, env = calls[0]
                self.assertEqual(argv[argv.index("--model") + 1], "gpt-6.1-sol")
                self.assertEqual(prompt, "the prompt")
                if role == "review":
                    self.assertEqual(argv[argv.index("--sandbox") + 1], "read-only")
                    self.assertNotIn("GH_TOKEN", env)

    def test_reusing_an_adapter_follows_a_new_release(self):
        adapter = ralph_agent.CodexAdapter("sol")
        for model in ("gpt-6.1-sol", "gpt-7-sol"):
            outcome, _, _ = self.launch(adapter, json.dumps(catalog(model)))
            self.assertEqual(outcome.model, model)

    def test_invalid_catalog_never_launches_an_agent(self):
        for payload in ("not json", json.dumps(catalog("gpt-6-astra"))):
            outcome, calls, _ = self.launch(ralph_agent.CodexAdapter("sol"), payload)
            self.assertEqual(outcome.kind, ralph_agent.INFRASTRUCTURE_FAILURE)
            self.assertIn("models/sol", outcome.output)
            self.assertEqual(calls, [])

    def test_catalog_process_failures_never_launch_an_agent(self):
        for error in (OSError("missing binary"), subprocess.TimeoutExpired("codex", 30),
                      subprocess.CalledProcessError(1, "codex")):
            with self.subTest(error=error), patch.object(
                    ralph_agent.subprocess, "run", side_effect=error):
                outcome = ralph_agent.CodexAdapter("sol").launch(
                    "prompt", run=lambda *args: self.fail("agent must not launch"))
                self.assertEqual(outcome.kind, ralph_agent.INFRASTRUCTURE_FAILURE)

    def test_pinned_codex_models_and_claude_alias_need_no_catalog_lookup(self):
        for adapter in (ralph_agent.CodexAdapter("gpt-6-sol"),
                        ralph_agent.ClaudeAdapter("opus")):
            outcome, calls, lookup = self.launch(adapter, "unused")
            lookup.assert_not_called()
            self.assertEqual(outcome.model, adapter.model)
            self.assertEqual(calls[0][0][calls[0][0].index("--model") + 1], adapter.model)

    def test_committed_config_and_sample_use_the_moving_families(self):
        for name in (".ralph.yml", ".ralph.yml.sample"):
            result = ralph_config.load_and_validate(os.path.join(ROOT, name))
            self.assertTrue(result.ok, result.errors)
            profiles = {p["provider"]: p["model"]
                        for p in result.config["models"]["profiles"]}
            self.assertEqual(profiles, {"claude": "opus", "codex": "sol"})
