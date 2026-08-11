# Product Capability Matrix

Phase 2 preserves product capabilities, not historical implementation shapes.

| Capability | Before | Current owner / implementation | Contract test |
|---|---|---|---|
| Simple chat | Large default tool set and layered mode/profile prompts | Chat `AgentSpec`, `Runtime.run`, one default discovery tool | `test_runtime_phase0_closure_baseline.py`, `test_context_instructions.py` |
| Conversation continuity | Session messages plus overlapping working/history memory | `Session` / `SessionStore` history | `test_session_store_incremental.py`, `test_context_instructions.py` |
| Coding inspect | Multiple convenience readers | `read_file`, `list_files`, `repo_map` primitives | `test_git_tools.py`, `test_repo_map.py` |
| Coding search | Overlapping grep/search wrappers | `rg` and deferred specialized tools | `test_grep_nl_tools.py`, `test_tool_spec.py` |
| Coding edit | Multiple write wrappers | `apply_patch` and workspace-scoped execution | `test_git_tools.py`, `test_tool_safety.py` |
| Coding shell | Shell plus duplicated script wrappers | workspace-scoped `bash` | `test_tool_safety.py`, `test_workspace_boundary.py` |
| Tool safety | Visibility checks mixed with runtime branches | `ToolPolicy`, `ToolExecutor`, hooks | `test_tool_safety.py`, `test_artifact_access_state.py` |
| Workspace isolation | Mode-specific path checks | `WorkspaceResolver` plus scope hooks | `test_workspace_resolver.py`, `test_bot_sandbox_tools.py` |
| Cancellation | Loop-specific stop prose and task mutation | generic `StopDecision`; optional TaskState observer | `test_reasoning_loop_token_gate.py`, `test_shared_runtime_task_state.py` |
| Streaming | Pipeline callback forwarding | `RunContext.on_text` through Runtime/Runner/model invocation | `test_web_streaming.py` |
| Context compaction | CodingContextState duplicated progress | event compaction and `ContextSnapshot`, TaskState read-only projection | `test_context_snapshots.py`, `test_context_pressure.py` |
| Optional long task state | Core loop imported and wrote TaskState | `TaskStateRunObserver` extension and TaskState service | `test_task_state_core.py`, `test_shared_runtime_task_state.py` |
| Optional durable memory | Markdown/lifecycle/candidate stack | repository, command service, retrieval, derived index | `test_memory_command_service.py`, `test_memory_scope.py` |
| Optional subagent | Large inherited main-agent prompt | isolated child Runtime and constrained AgentSpec/tool view | `test_subagent_runner.py`, `test_subagent_tools.py` |
| Optional skills | Eager catalog in every context | deferred deterministic discovery and selected skill load | `test_skill_runtime.py`, `test_context_instructions.py` |
| Artifact offload | ReasoningLoop emitted application artifact events | `ToolResultStoreHook` stores and emits artifact facts | `test_reasoning_loop_artifact_events.py`, `test_context_budget.py` |

The matrix is intentionally implementation-neutral: an old test is updated or
deleted when it only protects a retired alias, schema, prompt layer, tool count,
or lifecycle pipeline.
