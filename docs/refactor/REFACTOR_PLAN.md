# TaleClaw Agent Runtime Refactor Plan

Baseline: `bb4e845571428069a78a05526e71da8e41ddbcb7`

The authoritative architecture is
`docs/agent-runtime-refactor-summary.md`. Work is executed on
`refactor/agent-runtime-phase0-7`, one independently committed phase at a time.

## Phase sequence and scope

| Phase | Scope | Required stop |
|---|---|---|
| 0 | Behavior, architecture, snapshot and performance baseline | Human review |
| 1 | Unified AgentSpec | No |
| 2 | Runtime.run compatibility facade for Chat/Coding/Subagent | Human review |
| 3 | Runner/loop lifecycle convergence and policy extraction | No |
| 4 | Explicit RunContext with compatibility double-write | Human review |
| 5 | Context policies/providers without prompt changes | No |
| 6 | Applications and optional-extension isolation | No |
| 7 | Remove Mode/Pipeline/legacy compatibility paths | Final review |
| 8 | Consolidate physical package and directory boundaries | Human review |
| 9 | Consolidate Runtime internals into context, execution and tooling subpackages | Human review before implementation and after completion |
| 10 | Fix the minimum Agent Kernel ports and executable dependency boundaries | Human review |
| 11 | Extract stateless execution collaborators from ReasoningLoop | No |
| 12 | Move Working Memory, Search and batching behind explicit optional policies | No |
| 13 | Make Context capabilities explicit and keep the default Context kernel dependency-light | No |
| 14 | Extract History Retrieval and Security RAG/Trace from ContextBuilder | No |
| 15 | Remove legacy Retrieval/Security construction parameters from ContextBuilder | No |
| 16 | Extract Prompt Assets and Memory rendering services from ContextBuilder | No |
| 17 | Remove Prompt Assets and Memory compatibility construction parameters | No |

## Gates applied to every phase

- Behavior: Phase 0 snapshots and targeted behavior tests remain stable.
- Tests: targeted, component-group and complete pytest runs are executed.
- Compatibility: public compatibility surface and stored metadata are checked.
- Performance: deterministic Runtime benchmark is compared with the prior phase.
- Security: Tool authorization, workspace containment and identity tests pass.
- Documentation: design, execution report and state are current.

The repository has no configured lint, formatter-check or static type-check command.
Their absence is recorded rather than replaced during the refactor.

## Existing environment baseline

PostgreSQL-backed tests skip or fail without `DATABASE_URL`. Trace-only suites are
also run with `TRACE_INDEX_ENABLED=0` to exercise local trace behavior without
silently changing production configuration. macOS may canonicalize `/var` as
`/private/var`; existing strict path-string tests are tracked separately from
Runtime behavior.

## Commit policy

Each completed phase is committed locally as:

```text
phaseN: <summary>
```

No remote push, merge, rebase, destructive reset or clean is performed.
