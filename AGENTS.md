# ai-utils — Ralph Loop tooling (module notes)

The tool being built here is the **issue/label-driven Ralph Loop** shipped from
ai-utils. (The snarktank-style loop in `ralph/` is only the *build harness* that
drives construction — don't confuse the two.) Honor `CONTEXT.md` terminology (HIL,
not HITL) and `docs/adr/0001–0005`.

## Layout
- `bin/ralph` — bash CLI entrypoint; dispatches subcommands, delegates logic to `lib/`.
- `lib/*.py` — pure logic (Python 3, stdlib + `jsonschema` + `PyYAML`). No network, no side effects.
- `schema/*.json` — shipped JSON-schemas (e.g. `ralph.schema.json` for `.ralph.yml`).
- `.ralph.yml.sample` — documented sample config that MUST validate (a test asserts it).
- `test/run.sh` — the green gate. `test/unit/` = Python `unittest` (fixtures under `test/fixtures/`); `test/bats/` = bats orchestration (auto-skipped if bats absent).

## Conventions / gotchas
- No `pytest`/`bats` installed here; unit tests use stdlib `unittest`, run via `test/run.sh`.
- Python logic returns a result object (`ok`, `errors`, resolved data) rather than
  exiting; only the CLI wrapper prints and sets exit codes. Keeps logic unit-testable.
- Error strings name the offending field path (e.g. `branching/afk_merge: ...`) so
  `--check-config` failures are actionable.
- Config validation is JSON-schema Draft-7 with `additionalProperties: false`, which is
  how the mandated label scheme stays non-overridable (unknown keys like `labels:` fail).
- Schema `default`s are applied by `lib/ralph_config.py` after validation (jsonschema
  does not fill defaults itself).
