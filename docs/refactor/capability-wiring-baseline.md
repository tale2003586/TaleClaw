# Capability Wiring Baseline

Captured on 2026-08-12 from the current task starting point, before the
capability-wiring edits were applied.

## Repository

- Branch: `feat/memory-runtime-evolution`
- HEAD: `70df90fda22c75a0131ee0d20e198474f2646ce9`
- Baseline test command: `PYTHONPATH=. .venv/bin/python -m pytest -q`
- Baseline result: `632 passed` (the Phase 2 report at this HEAD)
- Baseline metrics were reproduced from `git archive HEAD`, so the dirty working
  tree and the existing untracked `os` entry did not affect them.

## Baseline Measurements

The registry row uses `build_lead_tool_registry()` without optional plugin
tools. Schema tokens are the repository estimator applied to compact JSON as a
single system-text message. Explore uses the actual
`TaskSubagentRunner._filtered_tools("explore")` registry.

| Metric | Baseline |
|---|---:|
| Registered tools | 52 |
| Chat visible tools | 1 |
| Coding visible tools | 8 |
| Explore child-registry tools | 16 |
| Explore visible tools | 6 |
| Installed skills | 4 |
| Chat schema bytes / tokens | 418 / 122 |
| Coding schema bytes / tokens | 9,802 / 2,816 |
| Explore schema bytes / tokens | 8,881 / 2,558 |
| Chat static prompt tokens | 253 |
| Coding static prompt tokens | 398 |

Baseline visible sets:

```text
Chat:
tool_search

Coding:
bash, edit_file, list_files, read_file, rg, tool_search,
update_task_state, write_file

Explore:
bash, list_files, read_file, rg, tool_search, update_task_state
```

The four installed skills were `agent-builder`, `code-review`, `mcp-builder`,
and `pdf`. They were loadable by exact name but had no supported discovery path
after the eager catalog was removed.

## Baseline Declaration Gaps

- `ToolSpec.injection` coexisted with `DEFAULT_VISIBLE_TOOLS`; the latter
  actually selected the default model view.
- `AgentSpec.ToolSet.allow/deny` were declared but were not passed into
  `schemas_for_turn()` or execution authorization.
- Deferred discovery searched too little metadata and required the model-facing
  `select:<tool>` protocol for deterministic unlock.
- `tool_search("catalog")` did not provide a useful recovery catalog.
- Skill metadata existed in `SkillLoader`, but `AgentSpec.skills` and
  `ContextPolicy.include_skills` did not delimit discovery and loading.
- `update_task_state` was always visible in Coding and ContextBuilder could
  create TaskState merely while building context.
- Explore prompted for `repo_map`, `read_files`, and `code_outline` while its
  real child contract did not contain all of them.
- `SpawnPolicy`, `RunLimits.max_tool_calls`, and
  `TerminationPolicy.allow_empty_final` were declarative without complete
  execution enforcement.
- `AgentSpec.hooks`, `AgentSpec.output_schema`, `ContextPolicy.name`, and
  `TerminationPolicy.name` had no Runtime consumer.
- `ToolPolicy.requires_approval()` always returned false, although its API
  implied an implemented approval feature.

## Measurement Note

The Phase 2 report's token rows are the authoritative historical values for this
HEAD. The current report re-runs the same compact-schema estimator specifically
for capability wiring; it does not claim network latency or model task-success
measurements from an offline deterministic suite.
