"""Unit tests for the model-profile catalog and role resolution (#44, PRD #42).

Covers the four seams the story names: config defaults, config validation
(unknown provider adapter, duplicate profile key, dangling default), profile
resolution for the two roles, and same-model identity comparison.
"""
import os
import subprocess
import sys
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
LIB_DIR = os.path.join(REPO_ROOT, "lib")
FIXTURES = os.path.join(REPO_ROOT, "test", "fixtures", "config")
SAMPLE = os.path.join(REPO_ROOT, ".ralph.yml.sample")
RALPH = os.path.join(REPO_ROOT, "bin", "ralph")

sys.path.insert(0, LIB_DIR)
import ralph_config  # noqa: E402
import ralph_models  # noqa: E402


def valid(name):
    return os.path.join(FIXTURES, "valid", name)


def invalid(name):
    return os.path.join(FIXTURES, "invalid", name)


def config_of(path):
    result = ralph_config.load_and_validate(path)
    assert result.ok, result.errors
    return result.config


class CatalogSchemaTests(unittest.TestCase):
    """AC: the schema accepts a catalog (key -> adapter + exact model id) plus
    committed default implementation/review profile keys."""

    def test_catalog_config_is_valid(self):
        result = ralph_config.load_and_validate(valid("models.yml"))
        self.assertTrue(result.ok, result.errors)

    def test_catalog_survives_validation_intact(self):
        config = config_of(valid("models.yml"))
        profiles = ralph_models.profiles(config)
        self.assertEqual(sorted(profiles), ["claude-impl", "codex-review"])
        self.assertEqual(profiles["claude-impl"].provider, "claude")
        self.assertEqual(profiles["claude-impl"].model, "claude-opus-5")
        self.assertEqual(profiles["codex-review"].provider, "codex")
        self.assertEqual(profiles["codex-review"].model, "gpt-5-codex")

    def test_committed_defaults_name_profile_keys(self):
        config = config_of(valid("models.yml"))
        self.assertEqual(config["models"]["defaults"]["implementation"], "claude-impl")
        self.assertEqual(config["models"]["defaults"]["review"], "codex-review")

    def test_a_config_without_a_catalog_still_validates(self):
        # The catalog is optional in the shipped schema; each target repository
        # opts in. An absent catalog is an empty catalog, not a broken config.
        result = ralph_config.load_and_validate(valid("minimal.yml"))
        self.assertTrue(result.ok, result.errors)
        self.assertEqual(ralph_models.profiles(result.config), {})

    def test_shipped_sample_config_declares_a_catalog(self):
        result = ralph_config.load_and_validate(SAMPLE)
        self.assertTrue(result.ok, result.errors)
        profiles = ralph_models.profiles(result.config)
        self.assertTrue(profiles, "the sample config must document a model catalog")
        defaults = result.config["models"]["defaults"]
        self.assertIn(defaults["implementation"], profiles)
        self.assertIn(defaults["review"], profiles)

    def test_summary_reports_the_catalog_and_role_defaults(self):
        summary = ralph_config.load_and_validate(valid("models.yml")).summary()
        self.assertIn("claude-impl", summary)
        self.assertIn("claude-opus-5", summary)
        self.assertIn("codex-review", summary)


class CatalogValidationTests(unittest.TestCase):
    """AC: --check-config rejects an unknown adapter, a duplicate profile key,
    and a default naming an absent profile, naming the offending field."""

    def _errors(self, fixture):
        result = ralph_config.load_and_validate(invalid(fixture))
        self.assertFalse(result.ok, "expected %s to be rejected" % fixture)
        self.assertTrue(result.errors)
        return "\n".join(result.errors)

    def test_unknown_provider_adapter_is_rejected(self):
        joined = self._errors("bad-model-provider.yml")
        self.assertIn("models/profiles/0/provider", joined)
        self.assertIn("acme", joined)

    def test_duplicate_profile_key_is_rejected(self):
        joined = self._errors("duplicate-model-profile.yml")
        self.assertIn("models/profiles/1/key", joined)
        self.assertIn("claude-impl", joined)
        self.assertIn("duplicate", joined.lower())

    def test_default_naming_an_absent_profile_is_rejected(self):
        joined = self._errors("unknown-default-profile.yml")
        self.assertIn("models/defaults/review", joined)
        self.assertIn("codex-review", joined)

    def test_check_config_rejects_a_duplicate_profile_key(self):
        proc = subprocess.run([RALPH, "--check-config",
                               invalid("duplicate-model-profile.yml")],
                              cwd=REPO_ROOT, stdout=subprocess.PIPE,
                              stderr=subprocess.STDOUT)
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn(b"models/profiles/1/key", proc.stdout)

    def test_check_config_rejects_an_unknown_provider_adapter(self):
        proc = subprocess.run([RALPH, "--check-config",
                               invalid("bad-model-provider.yml")],
                              cwd=REPO_ROOT, stdout=subprocess.PIPE,
                              stderr=subprocess.STDOUT)
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn(b"models/profiles/0/provider", proc.stdout)


class RoleResolutionTests(unittest.TestCase):
    """AC: role resolution returns the exact model identity per role from the
    committed defaults, and a CLI override by profile key wins over them."""

    def setUp(self):
        self.config = config_of(valid("models.yml"))

    def test_defaults_resolve_to_the_exact_model_identity(self):
        res = ralph_models.resolve_roles(self.config)
        self.assertTrue(res.ok, res.errors)
        self.assertEqual(res.implementation.key, "claude-impl")
        self.assertEqual(res.implementation.model, "claude-opus-5")
        self.assertEqual(res.review.key, "codex-review")
        self.assertEqual(res.review.model, "gpt-5-codex")

    def test_implementation_override_wins_over_the_committed_default(self):
        res = ralph_models.resolve_roles(self.config, implementation="codex-review",
                                         review="claude-impl")
        self.assertTrue(res.ok, res.errors)
        self.assertEqual(res.implementation.model, "gpt-5-codex")
        self.assertEqual(res.review.model, "claude-opus-5")

    def test_a_single_role_override_leaves_the_other_default(self):
        res = ralph_models.resolve_roles(self.config, review="claude-impl",
                                         allow_same_model=True)
        self.assertTrue(res.ok, res.errors)
        self.assertEqual(res.implementation.key, "claude-impl")
        self.assertEqual(res.review.key, "claude-impl")

    def test_an_override_naming_an_unknown_profile_refuses(self):
        res = ralph_models.resolve_roles(self.config, review="nope")
        self.assertFalse(res.ok)
        joined = "\n".join(res.errors)
        self.assertIn("nope", joined)
        self.assertIn("--review", joined)

    def test_resolution_refuses_without_a_catalog(self):
        res = ralph_models.resolve_roles(config_of(valid("minimal.yml")))
        self.assertFalse(res.ok)
        self.assertIn("models", "\n".join(res.errors))


class SameModelIdentityTests(unittest.TestCase):
    """AC: resolution refuses when both roles resolve to the same model
    identity, and proceeds with the explicit acknowledgement."""

    def setUp(self):
        self.config = config_of(valid("models-same-identity.yml"))

    def test_distinct_keys_with_one_model_identifier_are_the_same_identity(self):
        profiles = ralph_models.profiles(self.config)
        self.assertNotEqual(profiles["primary"].key, profiles["secondary"].key)
        self.assertTrue(ralph_models.same_identity(profiles["primary"],
                                                   profiles["secondary"]))

    def test_different_model_identifiers_are_different_identities(self):
        profiles = ralph_models.profiles(config_of(valid("models.yml")))
        self.assertFalse(ralph_models.same_identity(profiles["claude-impl"],
                                                    profiles["codex-review"]))

    def test_same_identity_refuses_by_default(self):
        res = ralph_models.resolve_roles(self.config)
        self.assertFalse(res.ok)
        joined = "\n".join(res.errors).lower()
        self.assertIn("claude-opus-5", joined)
        self.assertIn("same", joined)

    def test_same_identity_proceeds_with_the_acknowledgement(self):
        res = ralph_models.resolve_roles(self.config, allow_same_model=True)
        self.assertTrue(res.ok, res.errors)
        self.assertTrue(res.same_model)
        self.assertEqual(res.implementation.model, "claude-opus-5")
        self.assertEqual(res.review.model, "claude-opus-5")

    def test_an_override_that_collapses_the_pair_also_refuses(self):
        config = config_of(valid("models.yml"))
        res = ralph_models.resolve_roles(config, review="claude-impl")
        self.assertFalse(res.ok)
        self.assertIn("same", "\n".join(res.errors).lower())


class ResolveModelsCliTests(unittest.TestCase):
    """The operator-facing seam: `ralph --resolve-models`."""

    def _run(self, *args):
        return subprocess.run([RALPH, "--resolve-models"] + list(args),
                              cwd=REPO_ROOT, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE)

    def test_prints_the_resolved_identity_of_both_roles(self):
        proc = self._run(valid("models.yml"))
        self.assertEqual(proc.returncode, 0, proc.stderr.decode())
        out = proc.stdout.decode()
        self.assertIn("implementation: key=claude-impl provider=claude "
                      "model=claude-opus-5", out)
        self.assertIn("review: key=codex-review provider=codex "
                      "model=gpt-5-codex", out)

    def test_override_wins_over_the_committed_default(self):
        proc = self._run(valid("models.yml"), "--implementation", "codex-review",
                         "--review", "claude-impl")
        self.assertEqual(proc.returncode, 0, proc.stderr.decode())
        out = proc.stdout.decode()
        self.assertIn("implementation: key=codex-review", out)
        self.assertIn("review: key=claude-impl", out)

    def test_same_model_refusal_exits_two_and_names_the_acknowledgement(self):
        proc = self._run(valid("models-same-identity.yml"))
        self.assertEqual(proc.returncode, 2)
        self.assertIn(b"--allow-same-model", proc.stderr)

    def test_same_model_acknowledgement_proceeds(self):
        proc = self._run(valid("models-same-identity.yml"), "--allow-same-model")
        self.assertEqual(proc.returncode, 0, proc.stderr.decode())
        self.assertIn(b"model=claude-opus-5", proc.stdout)

    def test_an_invalid_config_exits_two(self):
        proc = self._run(invalid("duplicate-model-profile.yml"))
        self.assertEqual(proc.returncode, 2)
        self.assertIn(b"models/profiles/1/key", proc.stderr)

    def test_an_unknown_override_key_exits_two(self):
        proc = self._run(valid("models.yml"), "--review", "nope")
        self.assertEqual(proc.returncode, 2)
        self.assertIn(b"nope", proc.stderr)


if __name__ == "__main__":
    unittest.main()
