"""Validate a superproject's .ralph.yml against the shipped JSON-schema.

Pure logic: no network, no side effects. `load_and_validate` returns a
ValidationResult carrying ok/errors and, when valid, the config with schema
defaults applied. `bin/ralph --check-config` is a thin wrapper over `main`.

A missing config file fails loud rather than assuming defaults (ADR-0001).
"""
import copy
import json
import os
import sys

try:
    import yaml
except ImportError:  # pragma: no cover - environment guard
    sys.stderr.write("ralph: PyYAML is required (pip install pyyaml)\n")
    raise

try:
    import jsonschema
except ImportError:  # pragma: no cover - environment guard
    sys.stderr.write("ralph: jsonschema is required (pip install jsonschema)\n")
    raise

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_SCHEMA = os.path.join(REPO_ROOT, "schema", "ralph.schema.json")


class ValidationResult:
    def __init__(self, ok, errors, config=None):
        self.ok = ok
        self.errors = errors
        self.config = config or {}

    def summary(self):
        """One-block human summary of the resolved (defaults-applied) config."""
        c = self.config
        lines = []
        lines.append("version: %s" % c.get("version"))
        branching = c.get("branching", {})
        lines.append("base branch: %s" % branching.get("base"))
        lines.append("branch pattern: %s" % branching.get("branch_pattern"))
        lines.append("feature pattern: %s" % branching.get("feature_pattern"))
        lines.append("rescue pattern: %s" % branching.get("rescue_pattern"))
        lines.append("afk merge: %s" % branching.get("afk_merge"))
        limits = c.get("limits", {})
        lines.append("max attempts: %s" % limits.get("max_attempts"))
        lines.append("circuit breaker: %s" % limits.get("circuit_breaker"))
        protected = (c.get("control_plane") or {}).get("protected") or []
        lines.append("protected control plane: %s"
                     % (", ".join(protected) if protected else "(none)"))
        lines.append("notify: @%s" % c.get("notify", {}).get("github"))
        steps = ", ".join(s.get("name", "?") for s in c.get("gating", []))
        lines.append("gating steps: %s" % steps)
        models = c.get("models") or {}
        entries = models.get("profiles") or []
        if entries:
            lines.append("model profiles: %s" % ", ".join(
                "%s=%s:%s" % (e.get("key"), e.get("provider"), e.get("model"))
                for e in entries))
            defaults = models.get("defaults") or {}
            lines.append("default implementation: %s" % defaults.get("implementation"))
            lines.append("default review: %s" % defaults.get("review"))
        else:
            lines.append("model profiles: (none configured)")
        return "\n".join(lines)


def _load_schema(schema_path):
    with open(schema_path) as fh:
        return json.load(fh)


def _apply_defaults(schema, instance):
    """Recursively fill object-property defaults from the schema into instance."""
    if not isinstance(instance, dict):
        return instance
    props = schema.get("properties", {})
    for key, subschema in props.items():
        if key not in instance and "default" in subschema:
            instance[key] = copy.deepcopy(subschema["default"])
        if key in instance and subschema.get("type") == "object":
            instance[key] = _apply_defaults(subschema, instance[key])
    # Ensure optional object sections exist so defaults within them resolve.
    for key, subschema in props.items():
        if subschema.get("type") == "object" and key not in instance:
            instance[key] = _apply_defaults(subschema, {})
    return instance


def format_error(err):
    """Render a jsonschema error as '<field path>: <message>'.

    Shared with the review-result validator so both name the offending field the
    same way; an actionable field path is the convention every Ralph validator
    reports failures in.
    """
    path = "/".join(str(p) for p in err.absolute_path)
    # For an unexpected/extra property (additionalProperties), surface its name.
    if not path and err.validator == "additionalProperties" and err.instance:
        extra = [k for k in err.instance if k not in err.schema.get("properties", {})]
        if extra:
            path = ", ".join(extra)
    return "%s: %s" % (path or "(root)", err.message)


def provider_enum(schema_path=DEFAULT_SCHEMA):
    """The provider adapters a Model Profile may name, per the shipped schema.

    The adapter registry is keyed off the same list, so a config that validates
    can always be launched.
    """
    schema = _load_schema(schema_path)
    return list(schema["properties"]["models"]["properties"]["profiles"]
                ["items"]["properties"]["provider"]["enum"])


def _model_catalog_errors(data):
    """Catalog rules the JSON-schema cannot express: profile keys are unique and
    every committed role default names a profile in the catalog."""
    models = data.get("models")
    if not isinstance(models, dict):
        return []
    entries = models.get("profiles")
    if not isinstance(entries, list):
        return []

    errors, seen = [], set()
    for index, entry in enumerate(entries):
        key = entry.get("key") if isinstance(entry, dict) else None
        if not isinstance(key, str):
            continue
        if key in seen:
            errors.append("models/profiles/%d/key: duplicate profile key %r"
                          % (index, key))
        seen.add(key)

    defaults = models.get("defaults")
    if isinstance(defaults, dict):
        for role in ("implementation", "review"):
            key = defaults.get(role)
            if isinstance(key, str) and key not in seen:
                errors.append(
                    "models/defaults/%s: unknown profile key %r (catalog: %s)"
                    % (role, key, ", ".join(sorted(seen)) or "empty"))
    return errors


def _control_plane_errors(data):
    """Protected patterns must stay inside the repository they protect.

    An absolute or parent-escaping pattern could never match a repository-
    relative changed path, so it would silently protect nothing -- which is the
    worst possible failure mode for a rule whose entire job is to stop the
    mechanism approving changes to itself.
    """
    patterns = ((data.get("control_plane") or {}).get("protected")
                if isinstance(data.get("control_plane"), dict) else None)
    if not isinstance(patterns, list):
        return []
    errors = []
    for index, pattern in enumerate(patterns):
        if not isinstance(pattern, str):
            continue
        if os.path.isabs(pattern) or ".." in pattern.split("/"):
            errors.append(
                "control_plane/protected/%d: %r must be a repository-relative "
                "path pattern" % (index, pattern))
    return errors


def load_and_validate(config_path, schema_path=DEFAULT_SCHEMA):
    if not os.path.isfile(config_path):
        return ValidationResult(False, ["config file not found: %s" % config_path])

    with open(config_path) as fh:
        try:
            data = yaml.safe_load(fh)
        except yaml.YAMLError as exc:
            return ValidationResult(False, ["%s: invalid YAML: %s" % (config_path, exc)])

    if data is None:
        return ValidationResult(False, ["%s: config is empty" % config_path])
    if not isinstance(data, dict):
        return ValidationResult(False, ["%s: top-level config must be a mapping" % config_path])

    schema = _load_schema(schema_path)
    validator = jsonschema.Draft7Validator(schema)
    errors = sorted(validator.iter_errors(data), key=lambda e: list(e.absolute_path))
    if errors:
        return ValidationResult(False, [format_error(e) for e in errors])

    cross_field = _model_catalog_errors(data) + _control_plane_errors(data)
    if cross_field:
        return ValidationResult(False, cross_field)

    resolved = _apply_defaults(schema, copy.deepcopy(data))
    return ValidationResult(True, [], resolved)


def main(argv):
    if not argv:
        sys.stderr.write("usage: ralph_config.py <config-path> [schema-path]\n")
        return 2
    config_path = argv[0]
    schema_path = argv[1] if len(argv) > 1 else DEFAULT_SCHEMA
    result = load_and_validate(config_path, schema_path)
    if result.ok:
        print("OK: %s" % config_path)
        print(result.summary())
        return 0
    sys.stderr.write("INVALID: %s\n" % config_path)
    for err in result.errors:
        sys.stderr.write("  - %s\n" % err)
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
