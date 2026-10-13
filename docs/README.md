# TaleClaw Documentation

Current contracts:

- [Current architecture](architecture/SYSTEM_ARCHITECTURE.md)
- [Multi-session execution and managed Coding worktrees](architecture/PARALLEL_CODING.md)
- [Runtime design](architecture/runtime-system-design.md)
- [Memory architecture](architecture/MEMORY_ARCHITECTURE.md)
- [TaskState and ContextSnapshot architecture](architecture/TASK_STATE_CONTEXT_ARCHITECTURE.md)
- [Product capability matrix](refactor/product-capability-matrix.md)
- [Aggressive runtime pruning report](refactor/aggressive-runtime-pruning-summary.md)
- [TaskState legacy migration](migrations/TASK_STATE_CONTEXT_MIGRATION.md)
- [Long-term memory migration](migrations/long-term-memory-migration.md)

Operational guides under `docs/deployment`, `docs/runtime`, `docs/web`, and
`docs/gateways` remain topic-specific references. Files under `docs/workplan`,
`docs/reports`, `docs/specs`, `docs/superpowers`, `docs/interview`, dated
refactor reports, `docs/system-design`, and `runtime-phase*` are historical
snapshots. They may name removed Pipeline, Profile, WorkingMemory, prompt, tool,
or Memory lifecycle implementations and must not be read as current contracts.

Research proposals, not implemented contracts:

- [Coding multi-session execution and Git worktree design research](workplan/2026-10-03-parallel-coding-worktrees-research.md)
