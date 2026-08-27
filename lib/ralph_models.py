"""Model-profile catalog access and role resolution (#44, PRD #42).

Pure logic: no network, no side effects. The target repository commits an
allowlisted catalog of Model Profiles (stable profile key -> provider adapter +
exact configured model identifier) plus default profile keys for the
implementation and review roles, so a scheduled tick resolves both roles without
command-line arguments. `resolve_roles` applies an operator's per-role override
on top of those defaults and returns the exact model identity each role runs.

Independence is the invariant: a pair whose two roles resolve to the same model
identity is refused unless the operator passes the explicit same-model
acknowledgement. Identity is the exact configured model identifier — two
profiles that name one model are one model, whichever adapter runs it.

Catalog *well-formedness* (unknown adapter, duplicate key, a default naming an
absent profile) belongs to `ralph_config`, so `ralph --check-config` is the one
place a broken catalog is reported. This module trusts a validated config.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ralph_config  # noqa: E402


class ModelProfile:
    """One allowlisted catalog entry."""

    def __init__(self, key, provider, model):
        self.key = key
        self.provider = provider
        self.model = model

    def __repr__(self):  # pragma: no cover - debugging aid
        return "ModelProfile(%r, %r, %r)" % (self.key, self.provider, self.model)

    def describe(self):
        return "key=%s provider=%s model=%s" % (self.key, self.provider, self.model)


def profiles(config):
    """The catalog as {profile key: ModelProfile}; empty when none is declared."""
    entries = (config.get("models") or {}).get("profiles") or []
    return {e["key"]: ModelProfile(e["key"], e["provider"], e["model"])
            for e in entries}


def same_identity(a, b):
    """True when two profiles resolve to the same model identity.

    The exact configured model identifier is the identity; the provider adapter
    is an internal concern, so one model reached through two adapters still
    costs the independence the two roles exist to provide.
    """
    return a.model.strip() == b.model.strip()


class RoleResolution:
    def __init__(self, ok, errors, implementation=None, review=None,
                 same_model=False):
        self.ok = ok
        self.errors = errors
        self.implementation = implementation
        self.review = review
        self.same_model = same_model


def _pick(catalog, role, override, committed):
    """Resolve one role: an override by profile key wins over the committed
    default. Returns (profile, error)."""
    if override:
        profile = catalog.get(override)
        if profile is None:
            return None, ("--%s: unknown profile key %r (catalog: %s)"
                          % (role, override, ", ".join(sorted(catalog))))
        return profile, None
    if not committed:
        return None, ("models/defaults/%s: no default profile key is configured"
                      % role)
    profile = catalog.get(committed)
    if profile is None:
        return None, ("models/defaults/%s: unknown profile key %r (catalog: %s)"
                      % (role, committed, ", ".join(sorted(catalog))))
    return profile, None


def resolve_roles(config, implementation=None, review=None,
                  allow_same_model=False):
    """Resolve the implementation and review roles to exact model identities."""
    catalog = profiles(config)
    if not catalog:
        return RoleResolution(False, [
            "models: no model-profile catalog is configured; declare "
            "models.profiles and models.defaults"])

    defaults = (config.get("models") or {}).get("defaults") or {}
    errors = []
    impl, err = _pick(catalog, "implementation", implementation,
                      defaults.get("implementation"))
    if err:
        errors.append(err)
    rev, err = _pick(catalog, "review", review, defaults.get("review"))
    if err:
        errors.append(err)
    if errors:
        return RoleResolution(False, errors)

    collapsed = same_identity(impl, rev)
    if collapsed and not allow_same_model:
        return RoleResolution(False, [
            "models: implementation %r and review %r resolve to the same model "
            "identity %r; pass --allow-same-model to accept it"
            % (impl.key, rev.key, impl.model)])

    return RoleResolution(True, [], implementation=impl, review=rev,
                          same_model=collapsed)


def _cmd_resolve(rest):
    config_path, implementation, review, allow_same = ".ralph.yml", None, None, False
    positional = []
    i = 0
    while i < len(rest):
        arg = rest[i]
        if arg == "--allow-same-model":
            allow_same = True
        elif arg in ("--implementation", "--review"):
            if i + 1 >= len(rest):
                sys.stderr.write("ralph: %s requires a profile key\n" % arg)
                return 2
            i += 1
            if arg == "--implementation":
                implementation = rest[i]
            else:
                review = rest[i]
        elif arg.startswith("--"):
            sys.stderr.write("ralph: unknown option: %s\n" % arg)
            return 2
        elif arg:
            positional.append(arg)
        i += 1
    if len(positional) > 1:
        sys.stderr.write("ralph: --resolve-models takes at most one CONFIG\n")
        return 2
    if positional:
        config_path = positional[0]

    validated = ralph_config.load_and_validate(config_path)
    if not validated.ok:
        sys.stderr.write("INVALID CONFIG: %s\n" % config_path)
        for err in validated.errors:
            sys.stderr.write("  - %s\n" % err)
        return 2

    res = resolve_roles(validated.config, implementation=implementation,
                        review=review, allow_same_model=allow_same)
    if not res.ok:
        sys.stderr.write("REFUSED: model role resolution\n")
        for err in res.errors:
            sys.stderr.write("  - %s\n" % err)
        return 2

    print("implementation: %s" % res.implementation.describe())
    print("review: %s" % res.review.describe())
    if res.same_model:
        sys.stderr.write("note: both roles run the same model identity "
                         "(acknowledged via --allow-same-model)\n")
    return 0


def main(argv):
    if not argv:
        sys.stderr.write("usage: ralph_models.py resolve [CONFIG] "
                         "[--implementation KEY] [--review KEY] "
                         "[--allow-same-model]\n")
        return 2
    mode, rest = argv[0], argv[1:]
    if mode == "resolve":
        return _cmd_resolve(rest)
    sys.stderr.write("ralph_models.py: unknown mode: %s\n" % mode)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
