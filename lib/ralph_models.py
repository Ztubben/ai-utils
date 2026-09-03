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

`assign_plan` makes that resolution *durable* (#46): the first time a Story is
started it records the two exact model identities as Story labels
(`model:impl:<id>` / `model:review:<id>`, created on demand), and from then on
`roles_for_story` reads the roles off the Story instead of the config. A Story
that already carries an assignment keeps it -- later default changes and CLI
overrides never rewrite it -- so a retry, a resume, or an audit reproduces the
run that was actually made rather than the configuration that happens to be
current. Independence is decided once, when the pair is chosen; re-litigating it
on every resume would strand an in-flight Story on an unrelated config change.

Alternation (#47) is the third layer: the two profiles are a *pair*, and a Story
that carries no assignment at all takes its turn in swapping which one implements
and which one reviews. `assign_plan` stays pure -- it takes the phase and reports
whether it swapped and whether it consumed the phase -- while `ralph_alternation`
owns the ordering and the loop-local phase state, and the CLI advances that state
only once the assignment has actually been recorded.

Catalog *well-formedness* (unknown adapter, duplicate key, a default naming an
absent profile) belongs to `ralph_config`, so `ralph --check-config` is the one
place a broken catalog is reported. This module trusts a validated config.
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ralph_alternation  # noqa: E402
import ralph_config  # noqa: E402
import ralph_init  # noqa: E402
import ralph_story  # noqa: E402

# Assignment labels are created on demand (one per model identity), so they are
# not part of the fixed vocabulary `ralph --init` seeds -- but they use the same
# idempotent `gh label create --force` spelling.
ASSIGNMENT_LABEL_COLOR = "bfd4f2"


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
                 same_model=False, newly_assigned=None):
        self.ok = ok
        self.errors = errors
        self.implementation = implementation
        self.review = review
        self.same_model = same_model
        # The roles a *fresh* choice was just made for -- both, unless the Story
        # already carried an assignment for one of them (#46).
        self.newly_assigned = (list(ralph_story.MODEL_ROLES)
                               if newly_assigned is None else newly_assigned)


def profile_for_model(catalog, model):
    """The catalog entry whose exact model identifier is `model`, or None.

    Assignment labels record the identity, not the profile key, so this is how a
    persisted role gets back its provider adapter. Two keys naming one model are
    one identity (`same_identity`), so either entry answers equally.
    """
    for key in sorted(catalog):
        if catalog[key].model.strip() == model.strip():
            return catalog[key]
    return None


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


def roles_for_story(config, story, implementation=None, review=None,
                    allow_same_model=False):
    """Resolve both roles for one Story, its own labels winning over config.

    A role already recorded on the Story (`model:impl:` / `model:review:`) is
    read back from the label, so a resume or a retry runs the same model the
    Story was assigned -- config defaults and CLI overrides are ignored for it.
    A role with no label is resolved from the catalog exactly as `resolve_roles`
    does. `resolution.newly_assigned` names the roles that had no label, i.e.
    the ones a fresh choice was just made for.

    The independence invariant guards *fresh* choices only: a pair persisted on
    the Story is honored as recorded, because that decision (including an
    explicit same-model acknowledgement) was already made when the Story
    started.
    """
    catalog = profiles(config)
    if not catalog:
        return RoleResolution(False, [
            "models: no model-profile catalog is configured; declare "
            "models.profiles and models.defaults"])

    assignment, errors = ralph_story.model_assignment(story)
    if errors:
        return RoleResolution(False, errors)

    defaults = (config.get("models") or {}).get("defaults") or {}
    overrides = {"implementation": implementation, "review": review}
    resolved, newly_assigned = {}, []
    for role in ralph_story.MODEL_ROLES:
        model = assignment.get(role)
        if model:
            profile = profile_for_model(catalog, model)
            if profile is None:
                errors.append(
                    "%s: story is assigned model %r, which is not in the "
                    "catalog (%s); restore the profile or reassign the story"
                    % (ralph_story.MODEL_LABEL_PREFIXES[role], model,
                       ", ".join(sorted(catalog))))
            resolved[role] = profile
            continue
        newly_assigned.append(role)
        profile, err = _pick(catalog, role, overrides[role], defaults.get(role))
        if err:
            errors.append(err)
        resolved[role] = profile
    if errors:
        return RoleResolution(False, errors)

    impl, rev = resolved["implementation"], resolved["review"]
    collapsed = same_identity(impl, rev)
    if collapsed and newly_assigned and not allow_same_model:
        return RoleResolution(False, [
            "models: implementation %r and review %r resolve to the same model "
            "identity %r; pass --allow-same-model to accept it"
            % (impl.key, rev.key, impl.model)])

    return RoleResolution(True, [], implementation=impl, review=rev,
                          same_model=collapsed, newly_assigned=newly_assigned)


class AssignPlan:
    def __init__(self, ok, errors, commands, implementation=None, review=None,
                 newly_assigned=None, same_model=False, swapped=False,
                 advances_alternation=False):
        self.ok = ok
        self.errors = errors
        self.commands = commands
        self.implementation = implementation
        self.review = review
        self.newly_assigned = newly_assigned or []
        self.same_model = same_model
        # Alternation (#47): whether this plan runs the pair the other way
        # round, and whether it consumed a phase the next Story must not reuse.
        self.swapped = swapped
        self.advances_alternation = advances_alternation


def assign_plan(story, config, implementation=None, review=None,
                allow_same_model=False, phase=0, fixed_roles=False):
    """Build the plan that records this Story's model assignment durably.

    Pure: computes commands, runs nothing. For each role with no assignment
    label yet, creates that identity's label on demand and adds it to the issue
    in one edit. A Story whose roles are both already recorded yields an empty
    plan -- re-running is a no-op, and a crash between the two label writes
    heals forward by recording only the missing role. With no catalog configured
    the plan is empty and `implementation`/`review` are None: there is no exact
    identity to persist.

    `phase` is the alternation phase (#47): a Story that carries **no**
    assignment is a fresh pair, so an odd phase records the resolved pair the
    other way round and `plan.advances_alternation` tells the caller to advance
    the phase once the plan has actually been applied. Every other Story --
    assigned, or half-assigned and healing forward -- keeps what it carries and
    leaves the phase alone. `fixed_roles` disables the swap for this invocation,
    as the committed `models.alternate: false` does for every one.
    """
    if not profiles(config):
        # The catalog stays optional (#44): a target repository that declares no
        # profiles runs the provider Ralph shipped with, and there is no exact
        # identity to persist. Nothing to do rather than a refusal, so the tick
        # keeps working stories.
        return AssignPlan(True, [], [], newly_assigned=[])

    resolution = roles_for_story(config, story, implementation=implementation,
                                 review=review,
                                 allow_same_model=allow_same_model)
    if not resolution.ok:
        return AssignPlan(False, resolution.errors, [])

    newly_assigned = resolution.newly_assigned
    chosen = {"implementation": resolution.implementation,
              "review": resolution.review}

    # Alternation applies to a *fresh pair* only: a Story that already carries
    # either role keeps what it was assigned, so a resume, a retry, or a further
    # review round can never swap models midway (#47).
    fresh_pair = len(newly_assigned) == len(ralph_story.MODEL_ROLES)
    alternating = (fresh_pair and not fixed_roles
                   and ralph_alternation.enabled(config))
    if alternating:
        chosen["implementation"], chosen["review"] = ralph_alternation.order_for(
            phase, resolution.implementation, resolution.review)

    labels = [(role, ralph_story.model_label(role, chosen[role].model))
              for role in newly_assigned]

    commands = [
        ralph_init.label_command(
            label, ASSIGNMENT_LABEL_COLOR,
            "%s Agent: %s" % (role.capitalize(), chosen[role].model))
        for role, label in labels
    ]
    if labels:
        edit = ["gh", "issue", "edit", str(story["number"])]
        for _, label in labels:
            edit += ["--add-label", label]
        commands.append(edit)

    return AssignPlan(True, [], commands,
                      implementation=chosen["implementation"],
                      review=chosen["review"],
                      newly_assigned=newly_assigned,
                      same_model=resolution.same_model,
                      swapped=alternating and ralph_alternation.swaps(phase),
                      advances_alternation=alternating)


# Replacing a Story's assigned model is a *human* action (#61). Ralph never
# substitutes one on its own: a provider outage is retried and resumed, and a
# reviewer that keeps failing is a person's decision to make. The record lives
# on the Story like every other durable loop fact, so an audit reads why the
# run that was made differs from the one the assignment first chose.
REASSIGNMENT_MARKER = "<!-- ralph-model-reassignment:v1 -->"


def reassignment_record(payload):
    """The Story comment recording a role's replacement, and the reason for it."""
    return "\n".join([
        REASSIGNMENT_MARKER,
        "",
        "**Model reassignment — %s agent**, by hand." % payload["role"],
        "",
        "- was: `%s`" % payload["from"],
        "- now: `%s` (profile `%s`, provider `%s`)"
        % (payload["to"], payload["profile"], payload["provider"]),
        "- reason: %s" % payload["reason"],
        "",
        "```json",
        json.dumps(payload, indent=2, sort_keys=True),
        "```",
    ])


class ReassignPlan:
    def __init__(self, ok, errors, commands, role=None, previous=None,
                 replacement=None):
        self.ok = ok
        self.errors = errors
        self.commands = commands
        self.role = role
        self.previous = previous
        self.replacement = replacement


def reassign_plan(story, config, role, profile_key, reason=None,
                  allow_same_model=False):
    """Build the plan replacing one role's durable assignment on this Story.

    Pure: computes commands, runs nothing. Unlike `assign_plan`, which only ever
    *fills* an empty role, this rewrites one that is already recorded -- which is
    why nothing automated may call it. It refuses everything that would make the
    record misleading: a role with no assignment to replace (that is
    `assign_plan`'s job), a profile outside the allowlist, a replacement that is
    already the assigned identity, a reason left blank, and a swap that would
    quietly collapse both roles onto one model identity.
    """
    if role not in ralph_story.MODEL_ROLES:
        return ReassignPlan(False, ["role: unknown role %r (roles: %s)"
                                    % (role, ", ".join(ralph_story.MODEL_ROLES))],
                            [])
    catalog = profiles(config)
    if not catalog:
        return ReassignPlan(False, [
            "models: no model-profile catalog is configured; there is no "
            "allowlisted identity to reassign to"], [])
    if not (reason or "").strip():
        # An audit record without a reason records only that someone did it,
        # which is the half that is already obvious from the labels.
        return ReassignPlan(False, [
            "reason: a reassignment must say why; it is the audit record"], [])

    assignment, errors = ralph_story.model_assignment(story)
    if errors:
        return ReassignPlan(False, errors, [])
    previous = assignment.get(role)
    if not previous:
        return ReassignPlan(False, [
            "%s: story #%s has no %s assignment to replace; starting it records "
            "one (--assign-models)"
            % (ralph_story.MODEL_LABEL_PREFIXES[role], story.get("number", "?"),
               role)], [])

    replacement = catalog.get(profile_key)
    if replacement is None:
        return ReassignPlan(False, [
            "profile: unknown profile key %r (catalog: %s)"
            % (profile_key, ", ".join(sorted(catalog)))], [])
    if replacement.model.strip() == previous.strip():
        return ReassignPlan(False, [
            "%s: story #%s is already assigned %r; nothing to replace"
            % (ralph_story.MODEL_LABEL_PREFIXES[role], story.get("number", "?"),
               previous)], [])

    other = [r for r in ralph_story.MODEL_ROLES if r != role][0]
    counterpart = assignment.get(other)
    if (counterpart and replacement.model.strip() == counterpart.strip()
            and not allow_same_model):
        return ReassignPlan(False, [
            "models: reassigning %s to %r would leave both roles on one model "
            "identity; pass --allow-same-model to accept it"
            % (role, replacement.model)], [])

    new_label = ralph_story.model_label(role, replacement.model)
    old_label = ralph_story.model_label(role, previous)
    number = str(story["number"])
    commands = [
        ralph_init.label_command(
            new_label, ASSIGNMENT_LABEL_COLOR,
            "%s Agent: %s" % (role.capitalize(), replacement.model)),
        ["gh", "issue", "edit", number,
         "--add-label", new_label, "--remove-label", old_label],
        # The record comes last, so a crash leaves the assignment correct and
        # the record missing. A record of a swap that never happened is the
        # worse of the two failures: it is the thing an audit trusts.
        ["gh", "issue", "comment", number, "--body", reassignment_record({
            "story": story["number"], "role": role, "from": previous,
            "to": replacement.model, "profile": replacement.key,
            "provider": replacement.provider, "reason": reason.strip()})],
    ]
    return ReassignPlan(True, [], commands, role=role, previous=previous,
                        replacement=replacement)


class _RoleOptions:
    """The role flags `--resolve-models` and `--assign-models` share."""

    def __init__(self, positional, implementation, review, allow_same,
                 fixed_roles=False):
        self.positional = positional
        self.implementation = implementation
        self.review = review
        self.allow_same = allow_same
        self.fixed_roles = fixed_roles


def _parse_role_options(rest, allow_fixed_roles=False):
    """Return (_RoleOptions, None) or (None, exit code) on a bad flag.

    `--fixed-roles` is an assignment-time choice (there is no alternation to
    disable when only previewing the configured resolution), so `--resolve-models`
    rejects it as an unknown option rather than accepting it and ignoring it.
    """
    implementation, review, allow_same, fixed_roles = None, None, False, False
    positional = []
    i = 0
    while i < len(rest):
        arg = rest[i]
        if arg == "--allow-same-model":
            allow_same = True
        elif arg == "--fixed-roles" and allow_fixed_roles:
            fixed_roles = True
        elif arg in ("--implementation", "--review"):
            if i + 1 >= len(rest):
                sys.stderr.write("ralph: %s requires a profile key\n" % arg)
                return None, 2
            i += 1
            if arg == "--implementation":
                implementation = rest[i]
            else:
                review = rest[i]
        elif arg.startswith("--"):
            sys.stderr.write("ralph: unknown option: %s\n" % arg)
            return None, 2
        elif arg:
            positional.append(arg)
        i += 1
    return _RoleOptions(positional, implementation, review, allow_same,
                        fixed_roles), None


def _load_story(path):
    if path == "-":
        return json.load(sys.stdin)
    with open(path) as fh:
        return json.load(fh)


def _cmd_assign(rest):
    """`ralph --assign-models STORY [CONFIG] [...]`: record the Story's exact
    implementation and review model identities as durable labels (#46), in the
    order this tick's alternation phase calls for (#47)."""
    opts, rc = _parse_role_options(rest, allow_fixed_roles=True)
    if opts is None:
        return rc
    if not opts.positional:
        sys.stderr.write("ralph: --assign-models requires a STORY "
                         "(gh --json path, or - for stdin)\n")
        return 2
    if len(opts.positional) > 2:
        sys.stderr.write("ralph: --assign-models takes STORY and at most one CONFIG\n")
        return 2
    story_path = opts.positional[0]
    config_path = opts.positional[1] if len(opts.positional) > 1 else ".ralph.yml"

    try:
        story = _load_story(story_path)
    except (OSError, ValueError) as exc:
        sys.stderr.write("ralph: could not read story: %s\n" % exc)
        return 2

    validated = ralph_config.load_and_validate(config_path)
    if not validated.ok:
        sys.stderr.write("INVALID CONFIG: %s\n" % config_path)
        for err in validated.errors:
            sys.stderr.write("  - %s\n" % err)
        return 2

    state = ralph_alternation.state_path(os.getcwd())
    phase = ralph_alternation.read_phase(state)

    plan = assign_plan(story, validated.config,
                       implementation=opts.implementation,
                       review=opts.review,
                       allow_same_model=opts.allow_same,
                       phase=phase, fixed_roles=opts.fixed_roles)
    if not plan.ok:
        sys.stderr.write("REFUSED: model assignment\n")
        for err in plan.errors:
            sys.stderr.write("  - %s\n" % err)
        return 2

    run = ralph_init.run_plan(plan.commands, cwd=os.getcwd())
    if not run.ok:
        sys.stderr.write("FAILED: assign-models (exit %d): %s\n"
                         % (run.failed.returncode, " ".join(run.failed.args)))
        if run.failed.output.strip():
            sys.stderr.write(run.failed.output.rstrip() + "\n")
        return 1

    # Advance only now: the phase tracks assignments that were actually
    # recorded, so a gh outage cannot silently burn a swap (#47).
    if plan.advances_alternation:
        if not ralph_alternation.write_phase(state,
                                             ralph_alternation.advanced(phase)):
            sys.stderr.write("note: could not record the alternation phase; the "
                             "next story may repeat this role order\n")

    if plan.implementation is None:
        print("assigned: no model-profile catalog is configured; nothing to record")
        return 0

    print("implementation: %s" % plan.implementation.describe())
    print("review: %s" % plan.review.describe())
    if plan.newly_assigned:
        print("assigned: %s" % ", ".join(plan.newly_assigned))
        # Only a fresh pair has a role *order* to report; a half-assigned story
        # healing forward is filling one slot, not choosing a pair.
        if len(plan.newly_assigned) == len(ralph_story.MODEL_ROLES):
            print("role order: %s" % ("swapped" if plan.swapped else "resolved"))
    else:
        print("assigned: already recorded on the story; unchanged")
    if plan.same_model:
        sys.stderr.write("note: both roles run the same model identity\n")
    return 0


def _cmd_reassign(rest):
    """`ralph --reassign-model STORY ROLE PROFILE [CONFIG] --reason TEXT`.

    Human-only (#61): nothing in the unattended tick calls this. Ralph retries
    an outage and resumes; replacing the model a Story was assigned is a
    person's decision, and it leaves a record saying so.
    """
    reason, allow_same, positional = None, False, []
    i = 0
    while i < len(rest):
        arg = rest[i]
        if arg == "--allow-same-model":
            allow_same = True
        elif arg == "--reason":
            if i + 1 >= len(rest):
                sys.stderr.write("ralph: --reason requires TEXT\n")
                return 2
            i += 1
            reason = rest[i]
        elif arg.startswith("--"):
            sys.stderr.write("ralph: unknown option: %s\n" % arg)
            return 2
        elif arg:
            positional.append(arg)
        i += 1
    if not 3 <= len(positional) <= 4:
        sys.stderr.write("usage: ralph --reassign-model STORY ROLE PROFILE "
                         "[CONFIG] --reason TEXT [--allow-same-model]\n")
        return 2
    story_path, role, profile_key = positional[:3]
    config_path = positional[3] if len(positional) > 3 else ".ralph.yml"

    try:
        story = _load_story(story_path)
    except (OSError, ValueError) as exc:
        sys.stderr.write("ralph: could not read story: %s\n" % exc)
        return 2

    validated = ralph_config.load_and_validate(config_path)
    if not validated.ok:
        sys.stderr.write("INVALID CONFIG: %s\n" % config_path)
        for err in validated.errors:
            sys.stderr.write("  - %s\n" % err)
        return 2

    plan = reassign_plan(story, validated.config, role, profile_key,
                         reason=reason, allow_same_model=allow_same)
    if not plan.ok:
        sys.stderr.write("REFUSED: model reassignment\n")
        for err in plan.errors:
            sys.stderr.write("  - %s\n" % err)
        return 2

    run = ralph_init.run_plan(plan.commands, cwd=os.getcwd())
    if not run.ok:
        sys.stderr.write("FAILED: reassign-model (exit %d): %s\n"
                         % (run.failed.returncode, " ".join(run.failed.args)))
        if run.failed.output.strip():
            sys.stderr.write(run.failed.output.rstrip() + "\n")
        return 1

    print("reassigned: %s agent on #%s" % (plan.role, story["number"]))
    print("  was: %s" % plan.previous)
    print("  now: %s" % plan.replacement.describe())
    return 0


def _cmd_resolve(rest):
    opts, rc = _parse_role_options(rest)
    if opts is None:
        return rc
    if len(opts.positional) > 1:
        sys.stderr.write("ralph: --resolve-models takes at most one CONFIG\n")
        return 2
    config_path = opts.positional[0] if opts.positional else ".ralph.yml"
    implementation, review, allow_same = (opts.implementation, opts.review,
                                          opts.allow_same)

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
        sys.stderr.write("usage: ralph_models.py resolve [CONFIG] ...\n"
                         "       ralph_models.py assign STORY [CONFIG] ...\n"
                         "       [--implementation KEY] [--review KEY] "
                         "[--allow-same-model]\n")
        return 2
    mode, rest = argv[0], argv[1:]
    if mode == "resolve":
        return _cmd_resolve(rest)
    if mode == "assign":
        return _cmd_assign(rest)
    if mode == "reassign":
        return _cmd_reassign(rest)
    sys.stderr.write("ralph_models.py: unknown mode: %s\n" % mode)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
