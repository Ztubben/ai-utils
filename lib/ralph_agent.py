"""Provider-neutral agent adapters (#45, PRD #42).

One adapter contract launches either role, so the orchestration that drives the
Implementation Agent and the Review Agent never names a provider. An adapter
knows three things and nothing else:

  * how to spell its provider's launch command for the configured model
    identity (the exact identifier from the target repository's Model Profile
    catalog, #44);
  * how to hand that command a **fresh process** carrying no inherited session
    state -- no resume/continue flag, and the provider's own session variables
    stripped from the child environment, so a tick launched from inside a
    provider session cannot leak that session into the agent it starts;
  * how to classify what came back as normal completion, session exhaustion, or
    infrastructure failure.

`launch_role` is the whole public surface the loop needs: name a role, get an
outcome. Role resolution stays in `ralph_models` (including #44's independence
invariant), so this module never re-decides which model a role runs.

`bin/ralph.sh` cannot import Python, so the three outcomes are also a small
exit-code contract (`EXIT_*`) that `ralph --launch-agent` returns and the tick
reads. Session exhaustion outranks infrastructure failure: a truncated run is
checkpointed and resumed, never counted as a crash.
"""
import atexit
import json
import os
import shutil
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ralph_config  # noqa: E402
import ralph_ledger  # noqa: E402
import ralph_models  # noqa: E402
import ralph_session  # noqa: E402
import ralph_usage  # noqa: E402

# The three outcomes an adapter distinguishes.
NORMAL = "normal"
SESSION_EXHAUSTED = "session-exhausted"
INFRASTRUCTURE_FAILURE = "infrastructure-failure"

# The same three, as the exit-code contract `bin/ralph.sh` reads. 11 is the
# tick's own RC_STORY_COMPLETE (the done-signal is a loop protocol, not a
# provider outcome), so infrastructure failure takes 12.
EXIT_NORMAL = 0
EXIT_SESSION_EXHAUSTED = 10
EXIT_INFRASTRUCTURE_FAILURE = 12

_CLI_EXIT = {NORMAL: EXIT_NORMAL,
             SESSION_EXHAUSTED: EXIT_SESSION_EXHAUSTED,
             INFRASTRUCTURE_FAILURE: EXIT_INFRASTRUCTURE_FAILURE}

ROLES = ("implementation", "review")

# Compatibility default for a target repository that has not declared a
# `models:` catalog (it stays optional, #44): keep ticking on the provider Ralph
# shipped with, at whatever model that CLI defaults to.
DEFAULT_PROVIDER = "claude"


# `gh` finds its credential in two places: the token environment variables, and
# a config file -- `$GH_CONFIG_DIR/hosts.yml`, else `$XDG_CONFIG_HOME/gh`, else
# `~/.config/gh`. Unsetting the variables leaves a logged-in operator's file
# behind, so the review role points `GH_CONFIG_DIR` (which overrides the whole
# lookup) at an empty directory instead (#64). That is what makes "the reviewer
# holds no GitHub credential" true of the environment rather than only of the
# read-only tool policy it is also held to.
_CREDENTIAL_FREE_CONFIG_DIR = None


def credential_free_config_dir():
    """An empty directory to point a credential-free `gh` at.

    Created once per process and removed at exit: it holds nothing worth
    keeping, so every review launch can share the one directory.
    """
    global _CREDENTIAL_FREE_CONFIG_DIR
    if _CREDENTIAL_FREE_CONFIG_DIR is None:
        path = tempfile.mkdtemp(prefix="ralph-review-gh-")
        atexit.register(shutil.rmtree, path, True)
        _CREDENTIAL_FREE_CONFIG_DIR = path
    return _CREDENTIAL_FREE_CONFIG_DIR


def _run_process(argv, prompt, env):
    """Launch the provider CLI, hand it the prompt on stdin, and capture the
    combined output. Returns (exit code, output)."""
    proc = subprocess.run(argv, input=prompt, env=env, stdout=subprocess.PIPE,
                          stderr=subprocess.STDOUT, text=True)
    return proc.returncode, proc.stdout


class Outcome:
    """What one agent launch produced."""

    def __init__(self, kind, provider, model, exit_code, output, usage=None):
        self.kind = kind
        self.provider = provider
        self.model = model
        self.exit_code = exit_code
        self.output = output
        # What the provider said this cost, in the neutral categories (#62).
        # Never None: a launch that reported nothing reports it as unavailable.
        self.usage = usage if usage is not None else ralph_usage.normalize(
            provider, None)

    @property
    def cli_exit(self):
        """The exit code `ralph --launch-agent` returns for this outcome."""
        return _CLI_EXIT[self.kind]

    def __repr__(self):  # pragma: no cover - debugging aid
        return "Outcome(%r, %r, %r, %r)" % (self.kind, self.provider,
                                            self.model, self.exit_code)


class AgentAdapter:
    """The one contract both roles are launched through."""

    provider = None
    binary_env = None      # env var overriding the provider CLI's path
    default_binary = None
    session_env = ()       # inherited session state stripped from the child

    def __init__(self, model=None, role="implementation"):
        self.model = model
        self.role = role

    def binary(self):
        return os.environ.get(self.binary_env) or self.default_binary

    def argv(self):
        """The launch command for this adapter's configured model identity."""
        raise NotImplementedError

    def environment(self):
        """The child environment: this one, minus the provider's session state.

        Credentials and provider *configuration* are deliberately kept -- only
        the variables that would attach the child to an existing session go.
        """
        env = dict(os.environ)
        for name in self.session_env:
            env.pop(name, None)
        if self.role == "review":
            # Review is evidence-only.  Even if a provider somehow escapes its
            # local read-only tool policy, it receives no GitHub credential:
            # neither the token variables, nor the config file `gh` would
            # otherwise fall back to (#64).
            for name in ("GH_TOKEN", "GITHUB_TOKEN", "GH_ENTERPRISE_TOKEN"):
                env.pop(name, None)
            env["GH_CONFIG_DIR"] = credential_free_config_dir()
        return env

    def unwrap(self, output):
        """Split the provider's raw output into (the agent's words, its usage).

        A provider CLI can be asked for a machine-readable transcript that
        carries the token counts (#62); this is where that shape is understood,
        so nothing downstream has to know a provider ever spoke JSON.  The
        default is a provider that offers no such thing.
        """
        return output, None

    def _unwrap(self, output):
        """`unwrap`, but incapable of failing a run.

        Accounting is telemetry: a provider that changed its transcript format,
        truncated it, or wrote an error before it starts must cost the run
        nothing at all.  The raw output is then passed through whole -- losing
        the counts, never the agent's words.
        """
        try:
            text, raw = self.unwrap(output)
        except Exception:                       # noqa: BLE001 - see docstring
            return output, None
        return (output if text is None else text), raw

    def classify(self, exit_code, output):
        """The three-way verdict for one launch.

        The session-limit half is deliberately not decided here:
        `lib/ralph_session.py` owns that detection (#65) so the tick and every
        provider adapter read one answer, layered across an exit-code set, a
        family of wording patterns, and an operator marker -- and anchored to
        the tail of the output, so an agent *writing about* limits is not
        mistaken for a limit notice. This adds only the distinction that module
        leaves to its caller: a clean exit finished, a dirty one is
        infrastructure.
        """
        if ralph_session.classify(exit_code, output) == ralph_session.SESSION_EXHAUSTED:
            return SESSION_EXHAUSTED
        return NORMAL if exit_code == 0 else INFRASTRUCTURE_FAILURE

    def launch(self, prompt, run=None):
        """Run one fresh agent process and classify what came back."""
        run = run or _run_process
        try:
            exit_code, output = run(self.argv(), prompt, self.environment())
        except OSError as exc:
            # An unavailable provider CLI is infrastructure, not a failed story.
            return Outcome(INFRASTRUCTURE_FAILURE, self.provider, self.model,
                           None, "%s: %s" % (self.binary(), exc))
        # Unwrapped first, so every later reader -- the session-limit verdict
        # included -- sees the agent's own words rather than the transport
        # around them.
        text, raw_usage = self._unwrap(output)
        return Outcome(self.classify(exit_code, text), self.provider,
                       self.model, exit_code, text,
                       usage=ralph_usage.normalize(self.provider, raw_usage))


class ClaudeAdapter(AgentAdapter):
    provider = "claude"
    binary_env = "RALPH_CLAUDE"
    default_binary = "claude"
    session_env = ("CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT",
                   "CLAUDE_CODE_SSE_PORT", "CLAUDE_SESSION_ID")

    def argv(self):
        if self.role == "review":
            argv = [self.binary(), "--print", "--safe-mode",
                    "--permission-mode", "plan", "--no-session-persistence"]
        else:
            argv = [self.binary(), "--dangerously-skip-permissions", "--print"]
        # The envelope is the only place this CLI states what a run cost (#62).
        # It wraps the same text `--print` prints on its own, and `unwrap` puts
        # that text back, so asking for it changes nothing a caller sees.
        argv += ["--output-format", "json"]
        if self.model:
            argv += ["--model", self.model]
        return argv

    def unwrap(self, output):
        envelope = json.loads((output or "").strip())
        if not isinstance(envelope, dict) or not isinstance(
                envelope.get("result"), str):
            return output, None
        return envelope["result"], envelope.get("usage")


class CodexAdapter(AgentAdapter):
    provider = "codex"
    binary_env = "RALPH_CODEX"
    default_binary = "codex"
    session_env = ("CODEX_SESSION_ID", "CODEX_THREAD_ID")

    def argv(self):
        if self.role == "review":
            argv = [self.binary(), "exec", "--sandbox", "read-only",
                    "--ephemeral", "--ignore-user-config"]
        else:
            argv = [self.binary(), "exec",
                    "--dangerously-bypass-approvals-and-sandbox"]
        # This CLI states what a run cost on its event stream and nowhere else
        # (#62); `unwrap` turns the stream back into the agent's messages.
        argv.append("--json")
        if self.model:
            argv += ["--model", self.model]
        argv.append("-")            # read the prompt from stdin
        return argv

    def unwrap(self, output):
        """Recover the agent's messages and the turn's usage from the stream."""
        messages, usage, events = [], None, 0
        for line in (output or "").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except ValueError:
                # A line this does not understand -- a warning printed ahead of
                # the stream, say -- is skipped rather than taken as proof the
                # output is not a stream at all.
                continue
            if not isinstance(event, dict):
                continue
            events += 1
            item = event.get("item") or {}
            if isinstance(item, dict) and item.get("type") == "agent_message":
                messages.append(item.get("text") or "")
            if event.get("type") == "turn.completed":
                usage = event.get("usage")
        if not events:
            return output, None
        # Usage without messages still counts: the run happened. Passing the
        # raw stream through then is better than reporting the agent said
        # nothing at all.
        return ("\n".join(messages) if messages else output), usage


# Every provider the schema allows in a Model Profile has an adapter here, so a
# config that validates can always be launched (guarded by a test).
PROVIDERS = {ClaudeAdapter.provider: ClaudeAdapter,
             CodexAdapter.provider: CodexAdapter}


def adapter_for_role(config, role, implementation=None, review=None,
                     allow_same_model=False, story=None):
    """The adapter that runs `role`, per the target repository's catalog.

    With a `story`, the Story's own model assignment labels win (#46): a resume
    or a retry launches the model the Story was assigned, and the committed
    defaults and any CLI override are ignored for a role already recorded.

    Returns (adapter, errors); the adapter is None when the role is unknown or
    role resolution refused (an unknown profile key, an assigned identity that
    has left the catalog, or two roles collapsing to one model identity without
    the explicit acknowledgement).
    """
    if role not in ROLES:
        return None, ["role: unknown role %r (roles: %s)"
                      % (role, ", ".join(ROLES))]
    if not ralph_models.profiles(config):
        return PROVIDERS[DEFAULT_PROVIDER](role=role), []

    if story is None:
        resolved = ralph_models.resolve_roles(
            config, implementation=implementation, review=review,
            allow_same_model=allow_same_model)
    else:
        resolved = ralph_models.roles_for_story(
            config, story, implementation=implementation, review=review,
            allow_same_model=allow_same_model)
    if not resolved.ok:
        return None, resolved.errors
    profile = getattr(resolved, role)
    return PROVIDERS[profile.provider](profile.model, role=role), []


def launch_role(config, role, prompt, implementation=None, review=None,
                allow_same_model=False, run=None, story=None):
    """Launch `role` in a fresh process. Returns (Outcome, errors)."""
    adapter, errors = adapter_for_role(config, role,
                                       implementation=implementation,
                                       review=review,
                                       allow_same_model=allow_same_model,
                                       story=story)
    if adapter is None:
        return None, errors
    return adapter.launch(prompt, run=run), []


def _cmd_launch(rest):
    """`ralph --launch-agent ROLE [CONFIG] [...]`: prompt on stdin, the agent's
    output on stdout, the outcome as the exit code. `--story PATH` pins the
    launch to that story's recorded model assignment (#46)."""
    role, config_path, implementation, review, allow_same = None, ".ralph.yml", None, None, False
    story_path, positional = None, []
    i = 0
    while i < len(rest):
        arg = rest[i]
        if arg == "--allow-same-model":
            allow_same = True
        elif arg == "--story":
            if i + 1 >= len(rest):
                sys.stderr.write("ralph: --story requires a PATH (or -)\n")
                return 2
            i += 1
            story_path = rest[i]
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
    if not positional:
        sys.stderr.write("ralph: --launch-agent requires a ROLE (%s)\n"
                         % ", ".join(ROLES))
        return 2
    if len(positional) > 2:
        sys.stderr.write("ralph: --launch-agent takes ROLE and at most one CONFIG\n")
        return 2
    role = positional[0]
    if len(positional) > 1:
        config_path = positional[1]

    validated = ralph_config.load_and_validate(config_path)
    if not validated.ok:
        sys.stderr.write("INVALID CONFIG: %s\n" % config_path)
        for err in validated.errors:
            sys.stderr.write("  - %s\n" % err)
        return 2

    # stdin carries the prompt, so the story is a file path only (#46). With it,
    # the story's own assignment labels decide which model this role launches.
    story = None
    if story_path:
        try:
            with open(story_path) as fh:
                story = json.load(fh)
        except (OSError, ValueError) as exc:
            sys.stderr.write("ralph: could not read story: %s\n" % exc)
            return 2

    outcome, errors = launch_role(validated.config, role, sys.stdin.read(),
                                  implementation=implementation, review=review,
                                  allow_same_model=allow_same, story=story)
    if outcome is None:
        sys.stderr.write("REFUSED: cannot launch the %s agent\n" % role)
        for err in errors:
            sys.stderr.write("  - %s\n" % err)
        return 2

    if outcome.output:
        sys.stdout.write(outcome.output)
        if not outcome.output.endswith("\n"):
            sys.stdout.write("\n")
    # The tick launches the Implementation Agent straight through here, so this
    # is where that invocation is accounted for (#62). On stderr, deliberately:
    # stdout is the agent's own words, and the tick reads its done-signal there.
    event = ralph_usage.invocation_event(
        outcome.usage, role=role,
        phase=(ralph_usage.REVIEW if role == "review"
               else ralph_usage.IMPLEMENTATION),
        model=outcome.model, provider=outcome.provider,
        story=(story or {}).get("number"))
    ralph_usage.emit(event)
    # And durably, on the Story's ledger (#63) -- best-effort, so a gh blip
    # costs a row and never the iteration that row described.
    ralph_ledger.record((story or {}).get("number"), event)
    return outcome.cli_exit


def main(argv):
    if not argv:
        sys.stderr.write("usage: ralph_agent.py launch ROLE [CONFIG] "
                         "[--story PATH] [--implementation KEY] "
                         "[--review KEY] [--allow-same-model]\n")
        return 2
    mode, rest = argv[0], argv[1:]
    if mode == "launch":
        return _cmd_launch(rest)
    sys.stderr.write("ralph_agent.py: unknown mode: %s\n" % mode)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
