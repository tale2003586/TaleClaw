# Capability Wiring Repair Summary

## Current HEAD

- Branch: `feat/memory-runtime-evolution`
- Starting/current committed HEAD:
  `70df90fda22c75a0131ee0d20e198474f2646ce9`
- The repair is intentionally uncommitted in the working tree.
- Existing untracked `os` was not modified.

## Outcome

The small Phase 2 model surface is retained, but hidden capabilities now have a
deterministic discovery path. ToolSpec owns exposure and discovery metadata;
AgentSpec boundaries flow through both the model view and execution checks;
Skill discovery, conditional TaskState tools, spawn policy, tool-call limits,
and empty-final termination have real consumers.

## Problems Found and Repaired

- `ToolSpec.injection` did not own visibility because
  `DEFAULT_VISIBLE_TOOLS` selected the actual defaults. Four-state
  `ToolExposure` now drives `ToolPolicy.visible_tools`.
- Deferred tools depended on `select:<tool>`. `tool_search` now scores intent
  against name, aliases, capabilities, keywords and summary, returns at most
  three close matches, and unlocks them immediately for the current turn.
- Search now applies mode, AgentSpec, role/admin, agent type and exposure filters
  before candidates can be returned. Internal tools are excluded.
- `catalog/tools/list` now returns six compact capability groups instead of an
  error-shaped discovery path.
- `capability.discovery` traces query, candidate count, matches, scores,
  unlocked names, filters and no-result reason.
- `read_files` and `code_outline` are restored as high-value Coding primitives;
  benchmark scripts no longer waste model steps discovering already preloaded
  primitives.
- `update_task_state` is `CONDITIONAL`. Context construction no longer creates
  TaskState. CodingApplication activates it only for complex, long, structured,
  or externalized requests.
- Explore's real filtered registry, whitelist, AgentSpec allowlist and prompt now
  agree on `repo_map`, `read_files`, and `code_outline`; Explore cannot edit.
- Existing SkillLoader metadata is now searchable. Agent skill scope and
  `include_skills` are enforced for both discovery and load.
- Spawn policy is checked at execution even if a spawn tool was accidentally
  unlocked. `max_tool_calls` produces `tool_call_limit_exceeded`, and
  `allow_empty_final` changes empty-response termination.
- Fake declarations were removed: `AgentSpec.hooks`, `AgentSpec.output_schema`,
  `ContextPolicy.name`, `TerminationPolicy.name`, and the always-false
  `requires_approval()` API.

## Tool Pipeline

```text
Tool Registration
      |
      v
ToolSpec (schema, exposure, discovery, governance metadata)
      |
      v
AgentSpec.ToolSet requested boundary
      |
      v
Runtime ToolPolicy (mode, role, agent type, allow/deny, spawn/skill policy)
      |
      +---------------------------+
      |                           |
      v                           v
Preloaded                    Discoverable
full schema                  metadata only
      |                           |
      |                      tool_search
      |                           |
      |                    score + unlock
      +-------------+-------------+
                    v
             Final Tool View
                    |
                    v
                  Model
                    |
                    v
      ToolPolicy execution check -> ToolExecutor
```

Unlock scope is the current user turn. `Runtime._before_turn` calls
`reset_turn_unlocks`; discovery state therefore does not permanently pollute a
session.

## Skill Pipeline

```text
Installed SKILL.md files
      |
      v
SkillLoader metadata (name, description, tags, triggers,
                      applies_to, requires_tools, priority)
      |
      v
AgentSpec.skills + mode + ContextPolicy.include_skills
      |
      v
tool_search Skill Discovery (top 3 metadata cards)
      |
      v
load_skill(name)
      |
      v
Full SKILL.md body
```

An empty `AgentSpec.skills` means all mode-compatible installed skills are in
scope; an explicit tuple restricts both discovery and loading. No full catalog
is eagerly inserted into every prompt.

## Before and After Metrics

Core-registry numbers use `build_lead_tool_registry()` on both sides. Explore is
measured through the real child registry. Schema tokens use compact JSON and
the repository token estimator.

| Metric | Before | After |
|---|---:|---:|
| Core registered tools | 52 | 52 |
| Preloaded / deferred / conditional / internal | ambiguous | 10 / 41 / 1 / 0 |
| Chat visible | 1 | 1 |
| Coding visible | 8 | 9 |
| Explore child registry / visible | 16 / 6 | 16 / 7 |
| Chat schema bytes / tokens | 418 / 122 | 436 / 127 |
| Coding schema bytes / tokens | 9,802 / 2,816 | 5,708 / 1,568 |
| Explore schema bytes / tokens | 8,881 / 2,558 | 4,787 / 1,310 |
| Installed skills | 4 | 4 |
| Chat / Coding static prompt tokens | 253 / 398 | 249 / 395 |

Default startup adds three deferred plugin tools (`runtime_status`,
`web_search`, `markdown_to_pdf`) for 55 registered tools. Enabling Security RAG
adds `security_rag_search` for 56. These plugins do not change the initial Chat,
Coding, or Explore full-schema counts.

Current default views:

```text
Chat (1):
tool_search

Coding (9):
bash, code_outline, edit_file, list_files, read_file,
read_files, rg, tool_search, write_file

Explore (7 visible from a 16-tool hard-isolated registry):
bash, code_outline, list_files, read_file, read_files, rg, tool_search
```

Coding keeps these nine because they cover command execution, bounded
discovery/search, single and batch reads, large-file structure, creation,
focused edits, and optional capability discovery without extra model turns.

## Capability Discovery Evaluation

The contract suite exercises real intent rather than exact tool names:

| Intent | Expected result |
|---|---|
| `帮我联网查一下 OpenAI 最新文档` | `web_search` |
| `记住我以后 Python 测试默认使用 pytest` | `memorize` |
| `我之前说过测试框架偏好吗？` | `recall_memory` |
| `用适合这个任务的 skill 来处理` | matching Skill + `load_skill` |
| `开几个子 agent 并行分析` | `parallel_tasks` |
| `catalog` | compact recovery catalog |

The suite also covers auto-unlock, current-turn visibility, internal exclusion,
trace observability, allow/deny, skill scope, spawn denial, TaskState condition,
prompt/registry references and tool-description dependencies.

## Governance Truth

`risk`, `side_effect`, `state_effect`, `requires_audit`, and `policy_tag` are
classification and observability metadata. Side-effect/idempotence also informs
retry recovery. There is no human approval workflow in this Runtime repair.
The fake `requires_approval() -> False` method and decision field were removed;
the report therefore does not claim risk-based approval enforcement.

## Verification and Benchmark

- Capability contract suite: 15 tests after final declaration cleanup.
- Targeted benchmark/capability run: `22 passed in 20.03s` before the final
  declaration-only field cleanup; the affected focused suite then passed
  `17 passed in 0.22s`.
- Final full suite: `647 passed in 31.45s`.
- `git diff --check`: passed.
- `compileall` across tools/runtime/plugins/skills/agents/applications/evaluation/tests:
  passed before the final documentation-only step.
- Coding benchmark scripts removed 38 obsolete search/select steps for tools
  that are now preloaded; the benchmark and matrix tests pass.

This deterministic offline evaluation proves policy and discovery contracts and
measures static schema cost. It does not claim provider latency, average live
model search calls, or statistical task-success rate without a live model run.

## Acceptance Answers

1. Yes. `ToolSpec.exposure` now selects preloaded, deferred, conditional and
   internal behavior; `injection` no longer exists.
2. No. `DEFAULT_VISIBLE_TOOLS` was deleted.
3. Yes. `ToolSet.allow` intersects and `deny` subtracts from both schema view and
   execution authorization.
4. Yes for all deferred tools: every deferred ToolSpec has discovery summary,
   capability, alias and keyword metadata, with deterministic non-name scoring.
5. Yes. Tests cover Chinese web, memory write/recall, skills and subagents.
6. No. Search automatically unlocks the top relevant matches; `select:` is not
   a model-facing Runtime protocol.
7. Yes. `catalog/tools/list` returns a small capability-group catalog.
8. Coding defaults are the nine tools listed above, selected for frequent,
   information-dense coding operations and one progressive-disclosure entry.
9. Yes. `read_files` and `code_outline` are preloaded in Coding and Explore.
10. Yes. `update_task_state` appears only when TaskState is already active.
11. Yes. Explore prompt references are registered and inside its hard allowlist;
    the final view makes key read/discovery primitives immediately usable.
12. SkillLoader metadata is searched through `tool_search`; `load_skill` loads
    only the selected full body.
13. Yes. Explicit `AgentSpec.skills` restricts discovery and loading; empty means
    all mode-compatible skills.
14. Yes. `include_skills=False` removes `load_skill` from policy scope, so there
    is neither skill discovery nor loading.
15. Yes. `SpawnPolicy.enabled=False` denies execution even if a spawn tool is
    present or manually unlocked; agent types are checked again at execution.
16. Yes. `max_tool_calls` is a Runtime hard limit with an explicit StopReason.
17. No known declaration-only field remains in the audited ToolSpec/AgentSpec
    capability chain. Four unused declarative fields and fake hooks were removed.
18. Risk/side-effect data is metadata and retry/audit input, not approval. Human
    approval is not implemented.

## Closure Classification

Active Python, schemas, benchmark inputs and snapshots contain no old
`DEFAULT_VISIBLE_TOOLS`, `ToolInjection`, `.injection`, `requires_approval`, or
model-facing `select:` protocol. Remaining `select:` and `requires_approval`
references under older design, workplan, roadmap, interview and archived report
documents describe historical behavior/findings; they are retained as history,
not current Runtime instructions.
