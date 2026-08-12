# Declaration to Runtime Consumer Matrix

Current implementation at task HEAD
`70df90fda22c75a0131ee0d20e198474f2646ce9` plus this working-tree repair.
`Actually Used` means a production consumer changes visibility, authorization,
context, execution, tracing, or termination.

## Tool Declarations

| Declaration | Defined In | Intended Meaning | Runtime Consumer | Actually Used? | Previous Conflict | Action |
|---|---|---|---|---|---|---|
| `ToolSpec.schema` / `schemas_by_mode` | `tools/spec.py` | Provider execution schema | `ToolRegistry.schemas_for_turn`, `_schema_for_mode` | Yes | None | Keep |
| `handler` | `tools/spec.py` | Tool implementation | `ToolRegistry.execute` | Yes | None | Keep |
| `allowed_modes` | `tools/spec.py` | Mode authorization | `ToolSpec.enabled_for` via `ToolPolicy` | Yes | Policy had a parallel visible list | Keep, enforce before discovery and execution |
| `allowed_agent_types` | `tools/spec.py` | Agent-type authorization | `enabled_for` using AgentSpec metadata | Yes | Missing | Add and test |
| `exposure` | `tools/spec.py` | Preloaded/deferred/conditional/internal | `ToolPolicy.visible_tools`, discovery filter | Yes | `injection` plus `DEFAULT_VISIBLE_TOOLS` | Replace old enum; delete parallel map |
| `condition` | `tools/spec.py` | Conditional visibility predicate | `ToolPolicy._condition_met` | Yes | Missing | Add; use for active TaskState |
| discovery fields | `tools/spec.py` | Deterministic capability matching | `ToolRegistry._tool_search`, `_discovery_score` | Yes | Name/description-oriented search | Add summary, capability, alias and keyword ownership |
| `risk` | `tools/spec.py` | Audit classification | catalog and `tool.governance.observed` | Metadata only | Fake approval implication | Keep as explicitly audit-only |
| `side_effect` / `idempotent` | `tools/spec.py` | Retry safety and audit signal | execution recovery and governance trace | Yes | Split semantics were unclear | Keep; not approval |
| `state_effect` / `policy_tag` | `tools/spec.py` | State/audit classification | governance trace/catalog | Metadata only | No enforcement was claimed consistently | Keep as audit metadata |
| `requires_audit` | `tools/spec.py` | Emit governance observation | `ToolRegistry.execute` | Yes | None | Auto-enable for side/state effects |
| `admin_only` | `tools/spec.py` | Role authorization | `ToolSpec.enabled_for` | Yes | Search could reveal before filter | Filter before discovery |
| `session_scoped` | `tools/spec.py` | Inject current session | `ToolRegistry.execute` | Yes | None | Keep |
| `runtime_parameters` | `tools/spec.py` | Inject selected Runtime services | `ToolRegistry.execute` | Yes | None | Keep |

## Agent Declarations

| Declaration | Defined In | Intended Meaning | Runtime Consumer | Actually Used? | Previous Conflict | Action |
|---|---|---|---|---|---|---|
| `ToolSet.mode` | `runtime/agent_spec.py` | Capability mode | AgentSpec `tool_mode`, Registry/Policy | Yes | None | Keep |
| `ToolSet.allow` | `runtime/agent_spec.py` | Strict requested capability boundary | `ToolPolicy._allowed_names` | Yes | Declared but ignored | Wire as intersection |
| `ToolSet.deny` | `runtime/agent_spec.py` | Explicit capability subtraction | `ToolPolicy._allowed_names` | Yes | Declared but ignored | Wire into view and execution |
| `ContextPolicy.include_history` | `runtime/agent_spec.py` | Include conversation history | `ContextBuilder` history provider | Yes | None | Keep |
| `include_memory` | `runtime/agent_spec.py` | Include memory context | `ContextBuilder` memory provider | Yes | None | Keep |
| `include_skills` | `runtime/agent_spec.py` | Permit skill discovery/loading | `ToolPolicy` and ToolRegistry skill search | Yes | Declared but ignored | Enforce by removing `load_skill` from scope |
| `AgentSpec.skills` | `runtime/agent_spec.py` | Restrict installed skill scope | Skill search and `get_content` | Yes | Declared but ignored | Empty means all mode-compatible; tuple is allowlist |
| `SpawnPolicy.enabled` | `runtime/agent_spec.py` | Permit subagent spawn | visibility filter and execution authorization | Yes | Tool presence was the only practical gate | Enforce again at execution |
| `allowed_agent_types` | `runtime/agent_spec.py` | Restrict spawn type | `ToolPolicy._spawn_policy_error` | Yes | Normalization owned a second list | Enforce Runtime request arguments |
| `RunLimits.max_tokens` | `runtime/agent_spec.py` | Model output cap | `AgentRunner` / `ReasoningLoop` | Yes | None | Keep |
| `max_reasoning_steps` | `runtime/agent_spec.py` | Reasoning hard limit | `AgentRunner` / loop policies | Yes | None | Keep |
| `max_tool_calls` | `runtime/agent_spec.py` | Tool-call hard limit | `ReasoningLoop` counter and stop reason | Yes | Declared but ignored | Wire; reject the batch that crosses the limit |
| `allow_empty_final` | `runtime/agent_spec.py` | Empty response may terminate successfully | `ReasoningLoop` empty-response branch | Yes | Declared but ignored | Wire and test |
| `model_policy` / model purpose | `runtime/agent_spec.py` | Provider/model route | `AgentRunner` | Yes | None | Keep |
| `thinking_enabled` | `runtime/agent_spec.py` | Provider reasoning flag | `ReasoningLoop` model invocation | Yes | None | Keep |
| `instructions` | `runtime/agent_spec.py` | Behavior prompt owner | `ContextBuilder` | Yes | None | Keep |
| `metadata.agent_type` | `runtime/agent_spec.py` | Typed policy identity | `ToolPolicy` | Yes | Missing consumer | Keep and consume |
| `hooks` | removed | Agent lifecycle hooks | None | No | API illusion | Delete |
| `output_schema` | removed | Structured output declaration | None | No | API illusion | Delete |
| `ContextPolicy.name` | removed | Policy label | None | No | API illusion | Delete |
| `TerminationPolicy.name` | removed | Termination label | None | No | API illusion | Delete |

## Ownership Result

```text
ToolSpec: tool-owned availability, discovery, schema and audit metadata
AgentSpec.ToolSet: requested agent boundary
ToolPolicy: mode/role/agent/session final authorization
ToolRegistry: preloaded view, discovery, unlock and execution
ReasoningLoop: run limits and termination
```

Subagent whitelists remain only as hard isolation boundaries. The Explore
whitelist cannot grant a tool that ToolSpec/AgentSpec/ToolPolicy disallow, and
it no longer determines preload versus deferred exposure.
