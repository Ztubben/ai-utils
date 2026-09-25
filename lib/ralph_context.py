"""The context-fill signal that tells an iteration when to hand off (ADR-0004).

`prompts/handoff.v1.md` asks an iteration to watch its remaining context and
write a Handoff before it runs out, but a `--print` agent is never *told* how
full its context is. Instructions it cannot act on are how a Story drifts into
a degraded, half-remembered context instead of a clean resume.

The Claude CLI runs a `PostToolUse` hook after every tool call and hands it the
session's `transcript_path`. Each assistant entry there records the request
that produced it, and that request's input -- uncached, cache-written and
cache-read -- plus its output is what the next request starts from. So:

  * `context_tokens(lines)` reads the latest request's size off the transcript;
  * `notice(tokens, threshold)` is the hook's answer, or None under threshold;
  * `hook_command(threshold)` / `hook_settings(threshold)` are what the Claude
    adapter passes as `--settings`.

The hook is advice, not a gate: it never stops the agent (a hard stop would
end the run *without* a Handoff, which is a failed Attempt, #95). It is also
best-effort by construction -- a hook that fails is logged by the CLI and the
run goes on, and this one swallows its own errors besides, so a transcript it
does not understand costs the iteration nothing. Codex has no hook seam, so
the signal is Claude's only.

Pure except for `main`, which is the hook entry point: hook JSON on stdin, a
`hookSpecificOutput` object on stdout when over threshold, always exit 0.
"""
import json
import os
import shlex
import sys

_USAGE_KEYS = ("input_tokens", "cache_creation_input_tokens",
               "cache_read_input_tokens", "output_tokens")


def context_tokens(lines):
    """Tokens in context after the latest request, or None if none is recorded.

    `lines` is the transcript, one JSON entry per line. Unparseable lines and
    entries without usage (user turns, tool results, attachments) are skipped.
    """
    latest = None
    for line in lines:
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if not isinstance(entry, dict) or entry.get("type") != "assistant":
            continue
        message = entry.get("message")
        usage = message.get("usage") if isinstance(message, dict) else None
        if not isinstance(usage, dict):
            continue
        latest = sum(usage.get(key) or 0 for key in _USAGE_KEYS)
    return latest


def notice(tokens, threshold):
    """The hook's output for a context of `tokens`, or None under threshold."""
    if tokens is None or tokens < threshold:
        return None
    text = ("Ralph context notice: this session's context is at ~{:,} tokens, "
            "past the hand-off threshold of {:,}. Finish the step you are on, "
            "then start no new work: if you are implementing a Story that is "
            "not yet green, write a Handoff (commit and push the WIP, then "
            "`ralph --checkpoint`) and end the iteration. A clean-context resume beats a degraded one."
            ).format(tokens, threshold)
    return {"hookSpecificOutput": {"hookEventName": "PostToolUse",
                                   "additionalContext": text}}


def hook_command(threshold):
    """The shell command the CLI runs after each tool call."""
    return "%s %s hook %d" % (shlex.quote(sys.executable),
                              shlex.quote(os.path.abspath(__file__)),
                              threshold)


def hook_settings(threshold):
    """The `--settings` JSON that installs the hook for one launch."""
    return json.dumps({"hooks": {"PostToolUse": [{
        "matcher": "*",
        "hooks": [{"type": "command", "command": hook_command(threshold)}],
    }]}})


def _hook(threshold, stdin):
    try:
        path = json.loads(stdin.read()).get("transcript_path")
        with open(path) as fh:
            answer = notice(context_tokens(fh), threshold)
    except Exception:                       # noqa: BLE001 - see module doc
        return None
    return answer


def main(argv, stdin=sys.stdin, stdout=sys.stdout):
    if len(argv) != 2 or argv[0] != "hook" or not argv[1].isdigit():
        sys.stderr.write("usage: ralph_context.py hook THRESHOLD\n")
        return 2
    answer = _hook(int(argv[1]), stdin)
    if answer:
        stdout.write(json.dumps(answer) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
