# Runtime Composition / Capability Wiring Audit

Date: 2026-08-18

Scope: Phase 0 reconnaissance plus the Phase 1-6 implementation record.
The Phase 0 sections below preserve the before-state as inspected. The
implementation-result sections record later production changes separately;
they do not reinterpret Phase 0 facts as if they had always described the
post-refactor system. `CURRENT FACT` statements are grounded in the code and
tests available during the relevant phase.

## Evidence and terminology

The current composition root is
[`applications/bootstrap.py`](../../applications/bootstrap.py), not the
`runtime/bootstrap.py` path mentioned by some older architecture documents.
CLI, Telegram, Feishu, Web, and scripts call this `build_runtime()` path.

* **Constructor dependency**: an object is passed to `__init__` or a factory at
  construction time.
* **Runtime dependency**: an object is looked up or invoked only while a turn,
  tool call, or background action runs.
* **Late-bound dependency**: a dependency whose binding occurs after consumer
  construction.
* **Process-global**: module state shared by every `build_runtime()` call in
  the process. This does not imply that it is invalid.
* **AppRuntime-instance**: an object freshly constructed by one
  `build_runtime()` invocation and retained by that returned runtime.
* **Session-scoped**, **task-scoped**, and **turn-scoped**: state belonging to
  one conversation/session, coding/subagent task, or inbound execution,
  respectively.
* **Durable data**: filesystem or database data which outlives a runtime
  instance regardless of which object currently accesses it.

## Phase 0 dependency graph (before implementation)

### Process composition graph

```text
environment (.env, config, proxy)
  -> cached ModelPool -> routed providers/models
  -> MessageBus
  -> ArtifactStore -> LongContentDetector -> SessionManager
  -> TraceStore
  -> CancellationRegistry
  -> lead ToolRegistry (TEAM + handler maps + schema lists)
  -> optional history vector index
  -> optional semantic-memory repository/index
       -> command service, retrieval service, index synchronizer
  -> optional security embedding provider
       -> security retrieval router/classifier, knowledge index
  -> ContextBudgeter + EventCompactor
       -> ContextBuilder
  -> ModelTaskRunner
  -> PluginManager (registers plugin ToolSpecs and hooks into ToolRegistry)
  -> ToolExecutor (base hooks + plugin hooks)
  -> optional ReflectionAgent
  -> Runtime (ToolRegistry, provider/model/pool, ContextBuilder,
              ToolExecutor, reflection, policies)
  -> TEAM.configure(Runtime-adjacent model/executor state)
  -> CodingApplication (sessions, Runtime, optional memory command service,
                         artifacts, long-content detector)
  -> TaskSubagentRunner (Runtime)
  -> TurnCoordinator (bus, sessions, Runtime, router, plugins, coding app,
                      subagent runner, trace, cancellation, long-content)
  -> RuntimeServices (selected process-level services)
  -> AppRuntime (bus, coordinator, services)
```

`build_runtime()` is therefore a DAG-shaped composition root with two delayed
edges described below, not a strict infrastructure-to-application layer stack.
Optional memory/retrieval vertices are enabled by environment flags. Security
RAG construction catches all exceptions and substitutes `None`, while history
and semantic-vector builders can substitute null indexes depending on their
strict flags.

### Main execution graph

```text
inbound message
  -> TurnCoordinator
  -> SessionManager / TraceStore / CancellationRegistry
  -> AgentRouter -> AgentSpec
  -> CodingApplication (coding mode) OR Runtime.run (other modes)
  -> AgentRunner -> ContextBuilder + ToolRegistry + ToolExecutor + ModelPool
  -> provider/model call, tool policy/handler execution, trace events
  -> MessageBus outbound delivery
```

`ContextBuilder` is the context-composition owner. Its providers receive the
builder and use its memory, retrieval, budget, and coding-context collaborators;
they do not independently own those services. This is an intentional current
pattern, not evidence of a provider registry requirement.

### Subagent graph and circular wiring relationship

```text
ToolRegistry
  -> ToolSpec("task" | "parallel_tasks")
  -> handler function in tools.handlers
  -> module-global SUBAGENT_RUNNER              [late-bound]
  -> TaskSubagentRunner
  -> base Runtime
  -> base Runtime.agent_runner.tools (ToolRegistry)
```

`TaskSubagentRunner` needs an already-built base `Runtime` in order to fork a
filtered tool registry and derive a context builder. The current assembly has
a circular *wiring relationship*: task ToolSpecs are registered before the
runner is available, while the runner needs the Runtime that holds that
registry. Bootstrap resolves the current ordering by building the registry
first, creating the runner after `Runtime`, then calling
`configure_subagent_runner(runner)`.

This is not yet evidence that the relationship is an unavoidable
architectural construction cycle. `Runtime` passes the supplied registry by
reference into `AgentRunner`; `AgentRunner` passes that same reference into a
fresh `ReasoningLoop` for each run. `ReasoningLoop` calls
`ToolRegistry.schemas_for_turn()` at every reasoning step and invokes
`ToolRegistry.execute()` for a tool call. Neither path snapshots or freezes
registered ToolSpecs at Runtime or AgentRunner construction.

Phase 2 must evaluate, but not preselect, both of these designs:

| Candidate | Assembly | Benefits | Risks / tests needed |
| --- | --- | --- | --- |
| A. Explicit typed late-bound reference | Build task handlers with a typed unbound runner port; bind it after Runtime and TaskSubagentRunner exist. | Preserves the present registration order while replacing module-global state with explicit ownership, bind-once semantics, and fail-fast behavior. | Adds a small indirection and still represents delayed binding. Test unbound invocation, double bind, normal invocation, and two-runtime isolation. |
| B. Two-stage registry assembly | Register all non-task specs, construct Runtime and TaskSubagentRunner, then construct runner-bound task handlers and register their ToolSpecs into the same registry before exposing AppRuntime. | Removes the task runner global and uses direct constructor binding; current by-reference registry behavior makes pre-turn late registration safe. | Registration must finish before any runtime is exposed or run. Test registry completeness, schema visibility/discovery/execution after second-stage registration, plugins/order interactions, and two-runtime isolation. |

Both candidates retain the same registry object and preserve existing external
tool behavior. The Phase-2 choice depends on the smallest complete wiring path
after implementation-level tests; neither candidate is selected by this audit.

## Dependency classification and ownership

| Object/capability | Constructed by / held by | Constructor-time dependencies | Runtime consumers | Scope |
| --- | --- | --- | --- | --- |
| ModelPool | `get_model_pool()` module cache | environment/model config | Runtime, router classifier, reflection, TEAM, model tasks | process-global cached resource |
| MessageBus | `build_runtime()` / AppRuntime, coordinator | none | inbound/outbound dispatch, gateways | AppRuntime-instance; messages are turn-scoped |
| ArtifactStore and LongContentDetector | bootstrap / sessions, coding app, coordinator | artifact root; store | input/tool artifact handling | AppRuntime-instance accessors over durable filesystem data |
| SessionManager | bootstrap / coordinator, coding app, plugins | long-content detector, SessionStore | every turn and coding task | AppRuntime-instance; Session objects are session-scoped; rows/events are durable data |
| TraceStore | bootstrap / coordinator, `RuntimeServices` | filesystem root; optional index store | turn, tool, RAG, subagent traces | AppRuntime-instance; RunState and events are turn-scoped; trace files are durable data |
| CancellationRegistry | bootstrap / coordinator, `RuntimeServices` | none | per-turn cancellation scopes | AppRuntime-instance; cancellation tokens/scopes are turn-scoped |
| ToolRegistry | bootstrap / Runtime, plugins | schemas, handler maps, TEAM, ArtifactStore | model schemas, policy, discovery, execution | AppRuntime-instance lead registry; each subagent creates a task-scoped filtered view |
| ToolExecutor | bootstrap / Runtime and TEAM | hook list, plugin hooks | each tool call | AppRuntime-instance, except the same executor is also assigned to process-global TEAM; hook/request state is session/turn scoped |
| ContextBuilder | bootstrap / Runtime | budgeter, assets, memory, retrieval, coding view, providers | each model step | AppRuntime-instance; built context is turn-scoped |
| Semantic memory services | bootstrap / `RuntimeServices`, CodingApplication | Postgres repo, semantic index | context retrieval, memory tool, promotion | AppRuntime-instance accessors over durable memory data |
| Runtime | bootstrap / coordinator, CodingApplication, subagent runner | tools, provider/model/pool, context, executor, optional reflection | normal agent and coding runs | AppRuntime-instance |
| TaskSubagentRunner | bootstrap / coordinator and handler global | base Runtime | `task`, `parallel_tasks` handlers | AppRuntime-instance under current wiring; subagent Session, ChildRun, and result are task/turn scoped |
| CodingApplication | bootstrap / coordinator | sessions, Runtime, optional memory command service, artifacts | coding-mode turn | AppRuntime-instance; coding task session/workspace is task-scoped and durable on disk |
| TurnCoordinator | bootstrap / AppRuntime | all application collaborators above | every inbound turn | AppRuntime-instance |
| AppRuntime | bootstrap / entrypoints | bus, coordinator, `RuntimeServices` | start/stop/run APIs | AppRuntime-instance |

## Late-bound and process-global state

### Confirmed late-bound state

1. `tools.handlers.SUBAGENT_RUNNER` starts as `None`; handlers are registered
   before a runner exists, then bootstrap mutates it. Invocation before binding
   returns a string error rather than raising. It can be rebound without a
   guard. Tests directly reset/configure it, demonstrating test coupling.
2. `SEMANTIC_MEMORY_COMMAND_SERVICE`, `SEMANTIC_MEMORY_RETRIEVAL_SERVICE`, and
   `SEMANTIC_MEMORY_INDEX_SYNCHRONIZER` in `tools.handlers` are configured
   after the lead registry and its handler functions are created. This is not
   currently demonstrated to break a construction cycle: the memory services
   are created independently of the registry/Runtime, and the memory handlers
   only consume them at invocation time.

### Other mutable process singletons

* Bootstrap keeps `_MODEL_POOL`, `_MODEL_HEALTHCHECK_RESULTS`, and
  `_ENV_INITIALIZED`. Repeated `build_runtime()` calls reuse the model pool and
  environment initialization, but create fresh MessageBus, SessionManager,
  ToolRegistry, ToolExecutor, ContextBuilder, Runtime, TraceStore,
  TurnCoordinator, and AppRuntime-instance.
* `TEAM` is instantiated at import time and reconfigured on every bootstrap;
  it owns a thread map and local `.team` state.
* `BG`, `TASKS`, team `BUS`, `RELIABLE_BUS`, and `PROTOCOLS` are import-time
  orchestration singletons used by handlers/teammates.

**Assumption confirmed by implementation, not a documented guarantee:** the
normal deployment model behaves as one process with one active AppRuntime.
Creating more than one runtime in the same process can overwrite the handler
globals and TEAM configuration while earlier instances remain live. Existing
tests chiefly construct isolated fakes/registries and directly reset handler
globals; they do not establish multi-runtime isolation.

## Tool declaration and exposure data flow

```text
tools/schema.py grouped schema lists
  + tools/handlers.py grouped handler maps
  + tools/tool_registry.py name-indexed metadata tables
  -> _builtin_spec(schema, handler, source)
  -> ToolSpec
  -> ToolRegistry
  -> PluginManager may register plugin ToolSpec directly
  -> ToolPolicy (mode, agent type, AgentSpec ToolSet/SpawnPolicy, exposure,
                 condition, unlock state)
  -> schemas_for_turn / tool_search / execute
  -> ToolExecutor hooks and handler
```

`ToolSpec` is the runtime authority after registration: policy, catalog,
discovery scoring, schema variants, execution injection, tracing, and
governance read the registered spec. Plugin tools already take the desired
direct-registration route.

However, builtin `ToolSpec` construction is still derived from parallel
name-indexed metadata:

* `_SESSION_SCOPED_TOOLS`
* `_NON_IDEMPOTENT_TOOLS`
* `_PRELOADED_TOOLS` (with implicit default-to-deferred behavior)
* `_DEFERRED_TOOLS` is stale/dead declaration metadata: it has no current
  Python runtime consumer. `_builtin_spec()` does not read it; it selects
  `CONDITIONAL` for `update_task_state`, `PRELOADED` for `_PRELOADED_TOOLS`
  (and teammate `send_message`), then defaults every remaining builtin to
  `DEFERRED`.
* `_DISCOVERY_METADATA`
* `_modes_for_tool()` with three mode sets and special cases
* `_risk_for_tool()` with two risk sets and defaults
* `_builtin_spec()` name-specific branches for condition, policy tag,
  runtime parameters, and schema variants

These tables are bootstrap convenience only in the sense that they are
consumed once to construct specs. They are nevertheless competing *authoring*
sources for fields that `ToolSpec` claims to own. Adding or changing a builtin
tool may require schema list, handler map, one or more metadata tables, and
special-case code changes. A missing handler silently skips the schema (except
`tool_search`), which is an additional wiring failure mode.

`ToolPolicy` applies the final dynamic view consistently for discovery and
execution: allowed mode and agent type, `ToolSet.allow`/`deny`, skill policy,
spawn policy, exposure/condition, and session unlocks. Existing
`test_capability_wiring_consistency.py` exercises these invariants. No audited
current test demonstrates a known discovery/policy/runtime mismatch; the
principal risk is future drift in builtin declaration tables.

## AgentSpec data flow

`AgentSpec` is constructed by static agent definitions, the router, coding and
subagent paths. It normalizes legacy-compatible `model_purpose`, `max_tokens`,
and `max_reasoning_steps` into canonical `model_policy`, `limits`, and their
mirrored compatibility fields in `__post_init__`.

```text
AgentSpec input (legacy or canonical fields)
  -> __post_init__ normalization
  -> AgentRouter / CodingApplication / Runtime.run
  -> AgentRunner model route: model_policy.purpose (via model_purpose mirror)
  -> ToolPolicy: ToolSet, ContextPolicy, SpawnPolicy, metadata.agent_type
  -> ContextBuilder: ContextPolicy and tool mode
  -> ReasoningLoop: normalized limits
```

The canonical representation is `model_policy`, `tool_set`, `context_policy`,
`termination_policy`, `limits`, `skills`, and `spawn_policy`. Compatibility is
currently intentional and does not need removal. The residual clarity issue is
that some consumers read `model_purpose`/`max_tokens` aliases rather than the
canonical nested objects; normalization keeps them equal today. Phase 1 should
avoid an API break and should only tighten runtime consumption where a test can
prove alias/canonical divergence impossible.

## Resource inventory and shutdown

| Resource | Creation / ownership | Long-lived behavior | Current shutdown handling |
| --- | --- | --- | --- |
| OpenAI-compatible clients | process-global ModelPool cache | HTTP client/connection resources | no explicit close observed |
| Qdrant clients | AppRuntime-instance history, semantic-memory, and security indexes each construct one | network client; collection initialization | no explicit close observed |
| embedding providers | independently constructed by AppRuntime-instance history, semantic-memory, and security-RAG builders | configuration-dependent model/cache memory | no explicit close contract observed |
| Postgres memory repository | AppRuntime-instance when semantic flags are enabled | opens connections per operation; schema init | `closing()` is used per connection; no pooled owner detected |
| SessionStore | owned by AppRuntime-instance SessionManager | persistent DB connection | `SessionManager.close()` calls `SessionStore.close()`, which closes the connection; AppRuntime.stop does not call SessionManager.close() |
| TraceStore | AppRuntime-instance | filesystem writes, optional index store/subscribers | no close method observed |
| MessageBus dispatch task | AppRuntime-instance after `AppRuntime.start()` | one asyncio dispatcher task | AppRuntime.stop stops bus and cancels/awaits this task |
| BackgroundManager (`BG`) | process-global import-time singleton | daemon threads / subprocesses | no coordinated shutdown |
| TEAM / team bus | process-global import-time singletons | teammate threads and local files | no coordinated shutdown |
| ArtifactStore | AppRuntime-instance accessor | filesystem storage | no close needed identified |

The audit confirms an ownership gap: `AppRuntime.stop()` owns the user-bus
task but does not close `SessionManager`, even though SessionManager owns a
persistent SessionStore connection and exposes `close()`. The operational
severity depends on deployment lifecycle and runtime recreation behavior.
Phase 5 should establish explicit shutdown ownership before deciding whether a
simple close callback is sufficient or broader resource management is
justified.

## Phase-0 findings

1. **F1: builtin ToolSpec authoring is not SSOT.** Runtime reads one
   ToolSpec, but builtin fields originate in several parallel tables and
   special cases. Phase 1 should replace this with explicit builtin
   `ToolSpec` declarations (or a small adjacent declaration object) while
   retaining schema/handler grouping only where it does not repeat semantic
   metadata. `_DEFERRED_TOOLS` is concrete stale/dead evidence. Add a registry
   completeness assertion: every declared builtin must have a handler and
   exactly one spec.
2. **F2: subagent wiring is implicit mutable global state.** It resolves a
   current circular wiring relationship, but Phase 0 does not establish that
   a late-bound reference is necessary. Phase 2 must compare typed late
   binding and two-stage registry assembly before choosing an implementation.
3. **F3: semantic-memory binding is global even though no current
   construction cycle is evident.** Phase 4 should pass a small explicit
   handler dependency (or a narrowly scoped late-bound reference only if a
   cycle is proven) and update tests that mutate module globals.
4. **F4: bootstrap is readable as a composition root but contains
   several capability clusters in one 270-line function.** Phase 3 should use
   small builders for model/routing, memory/retrieval, context, tools/plugins,
   and application assembly only if signatures clarify dependencies. No DI
   container, layer manager, or service locator is warranted.
5. **F5: embedding/vector-client instantiation is redundant under some
   configurations.** History and semantic memory each call
   `build_embedding_provider_from_env()` and each index creates a Qdrant
   client. Security RAG separately builds an embedding provider/client with
   its own configuration. The duplication is factual; performance and memory
   impact are conditional: the default HashEmbeddingProvider is lightweight,
   while FastEmbed and BGE-M3 can create expensive model instances. Phase 6
   remains conditional and may share only equivalent configured resources
   while retaining distinct collections, schemas, retrieval policy, and
   fallback behavior.
6. **F6: process-global orchestration assumes a single active
   runtime.** Do not remove `TEAM`, `BG`, or model pooling in this round.
   Phase 5 should first determine whether hot reload, test isolation, or
   multi-runtime production deployment needs a change.
7. **F7: AppRuntime lifecycle ownership is incomplete.** SessionManager owns
   SessionStore, SessionStore holds a persistent database connection, and
   `SessionManager.close()` closes that connection. `AppRuntime.stop()` does
   not invoke `SessionManager.close()`. The ownership gap is confirmed; its
   operational severity depends on deployment lifecycle and runtime recreation
   behavior. Phase 5 should establish explicit shutdown ownership with the
   minimum required cleanup mechanism, without introducing a lifecycle
   framework preemptively.

## Proposed implementation priority

1. Phase 1: ToolSpec authoring SSOT and registry completeness (F1).
2. Phase 2: choose and implement explicit subagent wiring after targeted
   comparison tests (F2).
3. Phase 3: bootstrap cohesive builders (F4).
4. Phase 4: semantic-memory handler wiring (F3).
5. Phase 5: lifecycle ownership/cleanup only where tests establish a real
   lifecycle mechanism; begin with AppRuntime ownership of SessionManager
   shutdown (F7). F6 remains a separate single-active-runtime assumption.
6. Phase 6: conditional vector resource sharing (F5).

## Phase 1 Implementation Result

### Phase-0 final correction

F7 is now distinct from F6. F6 remains the process-global,
single-active-runtime assumption. F7 is the confirmed lifecycle ownership gap:
SessionManager owns SessionStore, SessionStore holds a persistent database
connection, and `SessionManager.close()` closes it, but `AppRuntime.stop()`
does not invoke SessionManager close. The operational severity depends on
deployment lifecycle; Phase 5 must establish explicit ownership using the
minimum required cleanup mechanism.

### Baseline

The repository standard test command is `PYTHONPATH=. pytest -q` from the
repository root (`pytest.ini` defines `tests` as the test path). Before Phase 1,
the detached HEAD baseline was run using the same local `.env` configuration:

| Run | Result |
| --- | --- |
| Phase-1 pre-change full suite | 650 passed, 2 warnings, 37.60s |
| Phase-1 pre-change focused suite | 34 passed |
| Phase-1 post-change focused suite | 38 passed, 0.30s |
| Phase-1 post-change full suite | 654 passed, 2 warnings, 38.12s |

There were no pre-existing test failures in the comparable baseline and no
Phase-1 regression. The four-test full-suite count increase is the new
builtin declaration completeness coverage. The two warnings are the existing
protobuf deprecation warnings from
`test_qdrant_filtered_search_builds_all_must_conditions`.

### Before

Builtin schemas were grouped in `tools/schema.py` and handlers in
`tools/handlers.py`, which remain appropriate separate concerns. Their
runtime semantic metadata was additionally authored through independent sets,
maps, and branching in `tools/tool_registry.py`: mode sets, risk sets,
preloaded/default exposure, non-idempotency, session scope, discovery maps,
and name-specific branches for conditions, runtime parameters, policy tags,
state effects, and schema variants. `_DEFERRED_TOOLS` had no runtime consumer.
The previous assembly silently omitted any schema whose handler was missing.

### After

`BuiltinToolDeclaration` in `tools/tool_registry.py` is the canonical builtin
semantic declaration. One declaration contains the ToolSpec semantic fields;
its `bind()` method only checks schema identity, selects the explicitly
declared teammate exposure where applicable, and builds the ToolSpec consumed
by runtime policy, discovery, schemas, governance, and execution. Schemas stay
in `tools/schema.py`; handlers stay in `tools/handlers.py`; lead and teammate
schema groups remain bootstrap grouping metadata rather than semantic
authorities.

`tool_search` remains an explicit Registry-owned execution exception: it has a
normal declaration and ToolSpec, but ToolRegistry intercepts execution. Its
marker handler is never used by the normal execution path.

### Removed competing sources

Removed from `tools/tool_registry.py`:

* `_SESSION_SCOPED_TOOLS`
* `_NON_IDEMPOTENT_TOOLS`
* `_PRELOADED_TOOLS`
* `_DEFERRED_TOOLS` (confirmed stale/dead)
* `_DISCOVERY_METADATA`
* `_modes_for_tool()`
* `_risk_for_tool()`
* `_builtin_spec()` and its name-specific semantic branches

No residual runtime consumer for these removed metadata symbols remains in the
current source tree.

### Registry completeness

`build_builtin_registry()` now validates builtin assembly before returning a
registry:

* a schema must declare `function.name` and names must be unique;
* every schema name must have a canonical builtin semantic declaration;
* every non-Registry-owned builtin must have a handler;
* each declaration verifies `schema.function.name == declaration.name` before
  building ToolSpec;
* handler names not represented by a registered schema fail fast;
* duplicate canonical declarations fail when indexed.

The registry's existing `register()` behavior remains unchanged so plugins may
continue to register a ToolSpec directly without being forced through builtin
assembly validation.

### Compatibility

Tool names, schemas, mode visibility, risks, idempotency, side effects,
conditions, policy tags, runtime parameter injection, schema variants,
discovery scoring terms, session unlock behavior, and `send_message`'s
lead-versus-teammate exposure remain unchanged. Plugin ToolSpec registration
remains compatible. Subagent and semantic-memory module-global wiring were not
modified, and bootstrap composition was not split.

### Tool addition workflow

Adding an ordinary builtin now requires:

1. add its model schema to the appropriate existing schema group;
2. add its callable to the appropriate existing handler map;
3. add one `BuiltinToolDeclaration` containing its ToolSpec semantic metadata.

`build_builtin_registry()` then validates schema-to-declaration-to-handler
completeness automatically. Developers do not separately update risk, mode,
exposure, idempotency, scope, or discovery metadata tables.

### Deferred

* Phase 2: subagent wiring design and implementation.
* Phase 3: bootstrap composition cleanup.
* Phase 4: semantic-memory wiring.
* Phase 5: AppRuntime lifecycle ownership (F7), separately from F6's
  single-active-runtime assumption.
* Phase 6: conditional vector-resource sharing.

## Phase 1 deferrals (historical)

* No production-code modification, API deletion, or behavior change.
* No DI container, generic capability model, provider registry, scope engine,
  lifecycle framework, or automatic graph resolver.
* No ContextProvider breakup: providers remain independently testable enough
  and `ContextBuilder` is the intended composition owner.
* No vector-client/provider sharing until configuration equivalence, lifecycle,
  and failure-isolation requirements are proven.
* No process-singleton removal without a concrete multi-runtime or lifecycle
  failure.
* No conversion of suppressed optional security-RAG construction failures into
  startup failures; that is a product/reliability decision outside this audit.

## Phase-0 validation record (historical)

Read-only evidence reviewed: bootstrap, ToolSpec/ToolRegistry/ToolPolicy,
AgentSpec, handlers, subagent runner, context composition, resource factories,
AppRuntime shutdown path, and capability-wiring/subagent/memory tests. The
next required execution before any implementation is the focused capability
and lifecycle test set identified by the relevant phase.

## Phase 2 Implementation Result

**Implemented: two-stage registry assembly (candidate B).** Candidate A, a
typed late-bound runner reference, would remove the module global but would
add a one-purpose indirection and retain delayed mutation. Candidate B is
smaller: bootstrap builds the lead registry without `task` and
`parallel_tasks`, builds `Runtime`, builds `TaskSubagentRunner(runtime)`, and
then registers runner-bound ToolSpecs into that same registry before returning
`AppRuntime`.

This is safe in the current implementation: `Runtime` stores the supplied
registry reference through `AgentRunner`; each run constructs a
`ReasoningLoop` with that reference; the loop calls `schemas_for_turn()` at
each reasoning step and `execute()` for each tool call. No Runtime,
AgentRunner, ReasoningLoop, or PluginManager construction path freezes or
snapshots builtins. Plugin setup still runs before the second-stage task
registration, and the completed registry is never exposed by bootstrap.

`SUBAGENT_RUNNER` and `configure_subagent_runner` were removed. The new
`register_lead_subagent_tools()` rejects an absent runner, while an explicitly
incomplete registry simply has no task ToolSpecs and reports `Unknown tool:
task`. `make_subagent_handlers(runner)` binds each handler directly to the
owning runner, so independently assembled registries cannot overwrite one
another. Tool names/schemas, fanout guards, task state, traces, filtered
subagent tools, cancellation, and result rendering remain on their existing
paths.

Focused coverage includes incomplete assembly, completed registry/reference
identity, execution through an AgentRunner created before registration,
parallel-task behavior, dispatch validation, and two-registry isolation.

## Phase 3 Implementation Result

**No change justified.** `build_runtime()` remains a hand-written composition
root with contiguous environment/model, storage/session, memory/retrieval,
context, tools/plugins/execution, Runtime/subagent, and application assembly
blocks. Its cross-dependencies are real DAG edges. Extracting the remaining
blocks would require broad return tuples or a service bundle, making the
dependency path less explicit. The one small Phase 6 helper below has a
narrow resource-sharing contract; it is not a bootstrap layer or container.

## Phase 4 Implementation Result

**Implemented: explicit memory handler factories.**
`make_memory_handlers(command_service=..., retrieval_service=...,
index_synchronizer=...)` creates the two closures passed into the lead
registry. `make_lead_handlers()` receives that mapping explicitly. The
`SEMANTIC_MEMORY_*` module globals, `configure_semantic_memory_services`, and
the shared `MEMORY_HANDLERS` map were removed.

Each AppRuntime therefore retains the memory command/retrieval/index objects
that its own registry handlers invoke. Disabled write/read paths keep their
previous user-facing responses; a missing synchronizer still permits a write,
and synchronization failures remain isolated. Existing `MemoryContext`, owner
scope, repository, trace queueing, retrieval, and context-memory paths are
unchanged. Focused tests cover disabled memory, write/read with an index,
handler isolation across two registries, and the existing memory scope suite.

## Phase 5 Implementation Result

**Implemented: direct SessionManager shutdown ownership.** The post-change
resource inventory is:

| Resource | Scope | Explicit close from AppRuntime |
| --- | --- | --- |
| ModelPool, TEAM, BG, task/protocol/team buses | process-global | no; retained single-active-runtime assumption |
| MessageBus dispatcher | AppRuntime-instance | yes, stopped and awaited by `AppRuntime.stop()` |
| SessionManager -> SessionStore -> DB connection | AppRuntime-instance / durable data | yes, synchronous `SessionManager.close()` once |
| ToolRegistry, ToolExecutor, ContextBuilder, Runtime, coordinator, trace store | AppRuntime-instance | no close contract observed |
| history/semantic/security indexes and embedding providers | AppRuntime-instance | no safe shared close contract observed |
| Session, ChildRun/coding workspace, RunState | session/task/turn-scoped | lifecycle remains with existing owners; durable rows/files outlive runtime |

`AppRuntime.stop()` cancels and awaits the outbound dispatcher, then closes
the `RuntimeServices.session_manager` once and marks the runtime closed. A
SessionManager close exception is logged rather than preventing dispatcher
termination or idempotent shutdown. No `AsyncExitStack` or lifecycle framework
was justified. `TEAM`, `BG`, and the ModelPool cache remain process-global;
they still encode the documented single-active-runtime assumption, but no
longer determine subagent or semantic-memory handler binding.

## Phase 6 Implementation Result

**Implemented: conditional history/semantic embedding-provider sharing only.**
History and semantic-memory indexes both use the same base embedding
environment: `EMBEDDING_PROVIDER`, `EMBEDDING_MODEL`, `QDRANT_VECTOR_SIZE`,
and, for BGE-M3, `EMBEDDING_USE_FP16`, `EMBEDDING_MAX_LENGTH`, and
`EMBEDDING_DEVICE`. Their Qdrant URL/API key/distance settings are also the
same, but their collections intentionally differ. When both capabilities are
enabled, bootstrap creates one provider and injects it into both builders.

If common provider construction fails, bootstrap supplies no provider and each
index builder retries its original construction independently, retaining its
own strict flag and null-index fallback. A history index failure does not
disable semantic indexing. Security RAG is deliberately excluded: it has
separate `SECURITY_RAG_*` provider/model/vector-size/device/cache settings,
optional sparse embeddings, hybrid/re-ranking behavior, and a distinct
exception-to-disabled boundary. Qdrant clients are also not shared because
their ownership/close contract and failure isolation are not established.

This reduces redundant construction only when both equivalent history and
semantic-memory configurations are active. The default hash provider is cheap;
the meaningful benefit is conditional for FastEmbed/BGE-M3 model instances.
Focused tests verify identity injection, distinct collections, and independent
index failure behavior.

## Final Runtime Composition

```text
process-global: environment initialization, cached ModelPool, TEAM/BG/task/team globals

build_runtime()
  -> AppRuntime-instance storage/session/trace/cancellation services
  -> optional history + semantic indexes
       -> one shared embedding provider only when their base configuration matches
       -> distinct index/client/collection and fallback boundaries
  -> memory handler closures bound to that runtime's memory services
  -> lead ToolRegistry without task tools -> plugins/executor -> Runtime
  -> TaskSubagentRunner(Runtime)
  -> register task/parallel_tasks ToolSpecs bound directly to that runner
  -> CodingApplication + TurnCoordinator + RuntimeServices -> AppRuntime

turn: AgentRunner creates ReasoningLoop -> reads ToolRegistry dynamically
task: task handler -> owning TaskSubagentRunner -> filtered Runtime fork
shutdown: AppRuntime.stop -> MessageBus dispatcher -> SessionManager -> SessionStore -> DB connection
```

The result remains a DAG-shaped composition root, not a strict layer stack or
DI container. `ToolSpec` is the builtin semantic source of truth, while plugin
ToolSpecs continue to register directly.

## Final Validation

| Check | Result |
| --- | --- |
| Phase 2/4/5/6 focused suite | 48 passed |
| Final full suite: `PYTHONPATH=. pytest -q` | 663 passed, 2 existing protobuf deprecation warnings, 36.83s |
| Syntax: `PYTHONPATH=. python -m compileall -q applications tools memory runtime agents plugins` | passed |
| Formatting/lint/type configuration | no repository-configured command found in `pyproject.toml` or project config |

The final dead-code search found no runtime Python consumers of the removed
subagent or semantic-memory wiring globals, or of the Phase 1 builtin metadata
tables. Historical design documentation retains a `SUBAGENT_RUNNER` reference
as before-state material and was not rewritten as current code.
