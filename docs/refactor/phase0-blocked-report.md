# Phase 0 Gate Recovery Report

> Date: 2026-07-23
> Branch: `refactor/agent-runtime-phase0-7`
> Start: `bb4e845571428069a78a05526e71da8e41ddbcb7`

## Outcome

The initial complete regression exposed four failures. They were fixed without
skipping tests or weakening security assertions, and the complete gate now passes.

## Passing gates

```text
Phase 0 targeted: 20 passed
Tool/workspace/identity security: 23 passed, 1 skipped
pip check: No broken requirements
git diff --check: passed
```

Behavior snapshots and performance artifacts are present. No snapshot was
automatically updated during this gate audit.

## Initial failed test gate

Command:

```text
TRACE_INDEX_ENABLED=0 .venv/bin/python -m pytest -q
```

Result:

```text
4 failed, 400 passed, 39 skipped
```

Failures:

1. `test_bot_sandbox_tools.py::test_task_session_uses_readable_task_id_scope`
   compares an uncanonicalized `/var/...` expected path with the production
   canonical `/private/var/...` path on macOS.
2. `test_swebench_adapter.py::test_official_eval_command_includes_instance_filter`
   has the same `/tmp` versus `/private/tmp` canonicalization mismatch.
3. `test_coding_agent_matrix.py::test_matrix_script_runs_one_scripted_cell_and_writes_reports`
   starts a subprocess that constructs the PostgreSQL SessionManager.
4. `test_run_system_evals_script.py::test_compatibility_entrypoint_runs_one_scripted_task`
   starts the same evaluation path outside pytest's in-process test adapter.

## Recovery

- Scripted evaluation now uses an explicit ephemeral SessionManager and disables
  only its optional PostgreSQL Trace Index. Real evaluation remains PostgreSQL.
- macOS path assertions compare canonical paths while retaining containment.
- Telegram integration setup checks PostgreSQL before changing process cwd.

Final complete regression:

```text
404 passed, 39 skipped
```

## Unsupported repository checks

The repository contains no configured lint, formatter-check or static
type-check command. This is recorded as unsupported, not reported as passed.

## Git state

The independent branch was created. Phase 0–3 work remains reviewable and
uncommitted. `.DS_Store` remains untouched. No push, reset, clean, rebase or
history rewrite was performed.

## Next action

Create the local Phase 0 commit and stop for the required human confirmation.
