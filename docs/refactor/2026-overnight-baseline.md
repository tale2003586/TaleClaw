# 2026 Runtime Slimming Baseline

Captured on 2026-08-12 before runtime edits.

## Repository

- Branch: `feat/memory-runtime-evolution`
- HEAD: `03a15802c4655df78d214ef70d5c80faade12b6c`
- Worktree: clean
- Python files: 312 tracked files, 84,948 LOC
- Tests: 124 test files, 20,997 LOC
- Test command: `PYTHONPATH=. .venv/bin/python -m pytest -q`
- Result: `694 passed in 28.41s`
- Note: `.venv/bin/pytest -q` produced 67 collection errors because the repository root was absent from its import path. The supported module invocation above is the valid baseline.

## Tracked Layout

```text
agents/          Agent definitions and subagent application
applications/    Bootstrap, turn coordination, coding application
memory/          Legacy file memory, semantic memory, lifecycle and experiments
runtime/         Context, execution, session, task-state, tooling and trace kernel
tools/           Tool schema, registry, policy, executor, hooks and handlers
tests/           Unit and deterministic integration tests
plugins/         Optional tool/context extensions
gateway/, web/   Product entrypoints
retrieval/       Optional security retrieval routing
knowledge/       Optional security knowledge implementation
```

Generated state directories (`.runs`, `.sessions`, `.coding_applications`, `.task_sessions`, Postgres and Qdrant data) are excluded from source metrics.

## LOC

| Area | Python LOC |
|---|---:|
| Total tracked | 84,948 |
| Runtime | 18,215 |
| Memory | 6,153 |
| Tools | 6,149 |
| Applications | 6,156 |
| Agents | 2,069 |
| Tests | 20,997 |
| Prompt-related (context builder/assets/providers + agent definitions + subagent prompting) | 2,409 |

There is no top-level `state/`; authoritative shared task state is in `runtime/task_state/` and coding compatibility/migration code is in `applications/coding/task_state.py`.

## Real Request Baseline

Requests were executed offline through `Runtime.run`, the real `ContextBuilder`, real lead registry and `ScriptedModel`. Token counts use TaleClaw's conservative `estimate_tokens`; tool tokens count compact JSON as one system-text payload. Network TTFT is not measurable with the deterministic provider.

| Scenario | Messages | System chars/tokens | Context tokens | Visible tools | Schema bytes/tokens | Context + tool tokens |
|---|---:|---:|---:|---:|---:|---:|
| Chat: `你好` | 3 | 2,198 / 913 | 1,541 | 14 | 8,221 / 2,285 | 3,826 |
| Coding read-only | 4 | 6,466 / 3,319 | 5,455 | 36 | 27,455 / 7,661 | 13,116 |
| Explore subagent | 4 | 8,192 / 3,787 | 4,702 | 15 | 15,300 / 4,311 | 9,013 |

Chat visible tools were `read_artifact`, `retrieve_tool_result`, `load_skill`, `update_task_state`, `memorize`, `recall_memory`, seven storage/sandbox/artifact tools, and `tool_search`. Coding exposed 36 of 49 registered coding-mode tools. The request includes a generated tool catalog in addition to the provider tool schemas, duplicating visible tool names and descriptions.

## Behavior Baseline

- Chat/final response: covered by runtime facade and phase closure tests.
- Deterministic tool call: covered by runtime phase baseline and tool execution tests.
- Memory recall/write: covered by `test_memory_recall`, `test_memory_scope`, `test_memory_command_service`, and semantic retrieval tests.
- Long task: `TaskState`, `StatePatch`, compaction, checkpoint recovery and context rebuild are covered by task-state, context-state and migration tests.
- Subagent: real child runtime construction and minimal run are covered by subagent tests and the request capture above.

## Timing Baseline

`benchmarks/runtime_phase0.py --iterations 20`:

| Scenario | Median ms | p95 ms |
|---|---:|---:|
| Chat no tool | 1.242 | 1.511 |
| Chat context build | 0.385 | 0.558 |
| Coding context build | 2.670 | 3.010 |
| Chat one tool | 2.247 | 2.539 |
| Explore subagent | 5.896 | 6.003 |

The benchmark is deterministic and offline; it measures runtime overhead, not provider TTFT.
