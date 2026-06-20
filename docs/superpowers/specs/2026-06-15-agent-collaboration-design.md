# Agent Loop & Multi-Agent Collaboration Improvement Design

**Date**: 2026-06-15
**Status**: Approved
**Scope**: AgentLoop refactoring, short-lived subagent system (Task tool), structured inter-agent communication protocol, teammate protocol integration, fan-out parallelism

---

## 1. Problem Summary

### 1.1 Current Architecture

```
AgentLoop.run_once()  (170 lines, monolithic)
├── PluginManager.before_turn()
├── ModeRouter.route()
│   ├── IntentClassifier (keyword-based)
│   ├── ExecutionPlanner
│   └── HybridModeClassifier (LLM gate)
├── coding → TaskSessionRunner.run_coding_task()
│   └── Pipeline.fork() → ReasoningLoop (single agent, max 24 steps)
│       ├── tool_call → tool_result cycle
│       └── post: ConclusionExtractor + MemoryPromoter
├── bot/chat → Pipeline.run() → ReasoningLoop (same)
│
└── Available tools (lead agent):
    ├── spawn_teammate: persistent thread, file-inbox comms
    ├── broadcast / send_message / read_inbox
    ├── task_create / claim_task: file-based task board
    ├── plan_approval: fire-and-forget
    └── task: short-lived subagent — DISABLED ("not wired")
```

### 1.2 12 Identified Issues

| # | Issue | Location | Impact |
|---|-------|----------|--------|
| 1 | `AgentLoop.run_once()` is 170-line monolith | `runtime/agent_loop.py:31-172` | Untestable, routing + execution + error handling all mixed |
| 2 | `TaskSessionRunner` is single-agent-only | `agents/coding/runner.py:55-200` | Coding task runs one ReasoningLoop, cannot fan-out subagents |
| 3 | `task` short-lived subagent tool is disabled | `tools/handlers.py:771-773` | Lead agent cannot spawn context-isolated short-lived subagents |
| 4 | No wait/join mechanism | Global | Lead spawns teammate, cannot wait for completion with structured result |
| 5 | No fan-out pattern | Global | Cannot "3 explore agents parallel → collect → 2 code agents parallel" |
| 6 | No result aggregation protocol | Global | No standardized subagent output format; relies on NL text parsing |
| 7 | Teammate errors don't propagate to lead | `coding_runtime/teammate.py:161` | `except Exception as e: print(...)` — crash is invisible to lead |
| 8 | BUS is fire-and-forget | `bus/team_bus.py` | No request-response correlation, no timeout, no delivery guarantee |
| 9 | No structured message schema | `tools/schema.py:418-451` | Messages are freeform `{sender, content, type}` dicts |
| 10 | Plan approval is one-way | `coding_runtime/protocols.py:14-22` | Lead sends approval to teammate inbox; teammate only sees it on idle poll |
| 11 | Task board has no notification | `coding_runtime/task.py` | `claim_task()` is a file lock; no notification that "task is done" |
| 12 | `TeammateManager` uses global singleton | `coding_runtime/teammate.py:311` | `TEAM = TeammateManager(...)` references `MODEL_POOL`, `BUS`, `TASKS` globals; untestable |

---

## 2. Target Architecture

```
AgentLoop.run_once()
├── _receive()          ← inbound consume + session resolve
├── _preprocess()       ← plugin before_turn (may abort)
├── _route()            ← ModeRouter.route()
├── _handle_switch()    ← mode switch reply (may return early)
├── _record()           ← append user message to session
├── _execute()          ← dispatch:
│   ├── task_session → TaskSessionRunner
│   │   └── Pipeline → ReasoningLoop
│   │       └── task(explore/code/plan) → TaskSubagentRunner  ← NEW
│   │           ├── isolated Session
│   │           ├── filtered tools per agent_type
│   │           ├── ReasoningLoop
│   │           └── structured SubagentResult
│   └── bot/chat → Pipeline → ReasoningLoop (unchanged)
├── _postprocess()      ← plugin after_turn + memory lifecycle
└── _deliver()          ← outbound publish + emit text
```

---

## 3. Module 1: AgentLoop Refactoring

### 3.1 Goal

Decompose `AgentLoop.run_once()` (170 lines) into 7 phase methods, each ≤ 30 lines, independently testable.

### 3.2 Phase Methods

```python
class AgentLoop:
    def __init__(self, bus, sessions, pipeline, router, plugin_manager,
                 task_session_runner, subagent_runner, trace_store):
        ...

    async def run_once(self, on_text=None):
        inbound = await self.bus.consume_inbound()
        session = self.sessions.get_or_create(inbound.session_key)

        # Phase 1: Identity & trace
        self._receive(session, inbound)

        # Phase 2: Pre-turn plugins (may abort)
        if self._preprocess(session, inbound, on_text):
            return

        # Phase 3: Route
        route = self._route(session, inbound.content)
        if route.switched:
            self._handle_switch(session, inbound, route, on_text)
            return

        # Phase 4: Record user message
        self._record(session, inbound)

        # Phase 5: Execute
        reply = self._execute(session, inbound, route, on_text)

        # Phase 6: Post-turn
        self._postprocess(session, inbound, reply)

        # Phase 7: Deliver
        self._deliver(session, inbound, reply, route, on_text)
```

### 3.3 Method Signatures

```python
def _receive(self, session, inbound) -> None:
    """Validate inbound identity, start run_state + trace."""
    ...

def _preprocess(self, session, inbound, on_text) -> bool:
    """Run plugin_manager.before_turn(). Return True if aborted."""
    ...

def _route(self, session, user_text: str) -> RouteResult:
    """ModeRouter.route()."""
    ...

def _handle_switch(self, session, inbound, route, on_text) -> None:
    """Handle mode switch: record messages, emit + publish reply."""
    ...

def _record(self, session, inbound) -> None:
    """Append user message to session.messages."""
    ...

def _execute(self, session, inbound, route, on_text) -> str:
    """Dispatch: task_session or pipeline."""
    ...

def _postprocess(self, session, inbound, reply) -> None:
    """Plugin after_turn + memory lifecycle + after_run plugins."""
    ...

def _deliver(self, session, inbound, reply, route, on_text) -> None:
    """Finish run, save session, publish outbound, emit text."""
    ...
```

### 3.4 Files

| Action | File |
|--------|------|
| Modify | `runtime/agent_loop.py` |
| Create | `tests/test_agent_loop_phases.py` |

---

## 4. Module 2: Short-Lived Subagent System

### 4.1 Goal

Wire the existing `task` tool schema so the lead agent can spawn context-isolated, short-lived subagents synchronously.

### 4.2 Agent Types & Tool Whitelists

```python
SUBTASK_TOOL_WHITELIST: dict[str, set[str]] = {
    "explore": {
        "bash", "list_files", "read_file",
        "git_status", "git_diff", "git_log",
        "storage_list_files", "storage_read_file",
        "tool_search",
    },
    "plan": {
        "bash", "list_files", "read_file",
        "git_status", "git_diff", "git_log",
        "tool_search",
    },
    "code": {
        "bash", "list_files", "read_file", "write_file", "edit_file",
        "git_status", "git_diff", "git_log", "git_branch", "git_add", "git_commit",
        "background_run", "check_background",
        "load_skill", "tool_search",
        "memorize", "recall_memory",
        # NOTE: NO task, NO spawn_teammate — prevents infinite recursion
    },
}
```

### 4.3 TaskSubagentRunner

```python
@dataclass
class SubagentResult:
    agent_type: str
    success: bool
    summary: str
    files_touched: list[str]
    tool_count: int
    error: str | None = None


class TaskSubagentRunner:
    """Execute short-lived, context-isolated subagent tasks synchronously."""

    def __init__(self, *, base_pipeline: Pipeline, sessions: SessionManager):
        self.base_pipeline = base_pipeline
        self.sessions = sessions

    def run(
        self,
        *,
        prompt: str,
        agent_type: str,
        parent_session,          # for workspace/user scope
        workspace_root=None,
    ) -> SubagentResult:
        # 1. Validate agent_type
        if agent_type not in SUBTASK_TOOL_WHITELIST:
            return SubagentResult(agent_type=agent_type, success=False,
                                  summary="", files_touched=[],
                                  error=f"Unknown agent_type: {agent_type}")

        # 2. Create isolated session
        sub_session = self._create_sub_session(parent_session, agent_type)
        sub_session.add_message("user", prompt)

        # 3. Build filtered tool registry
        tools = self._build_tools(agent_type)

        # 4. Build sub-pipeline with isolated context
        context_builder = ContextBuilder(memory_store=MemoryStore())
        sub_pipeline = self.base_pipeline.fork(context_builder=context_builder)

        # 5. Run reasoning loop (synchronous, blocking)
        profile = ModeProfile(name=f"subtask:{agent_type}", tool_mode="coding",
                              system_prompt=SUBTASK_SYSTEM_PROMPTS[agent_type])

        sub_pipeline.run(sub_session, profile)

        # 6. Extract structured result
        reply = get_last_assistant_text(sub_session.messages)
        return SubagentResult(
            agent_type=agent_type,
            success=True,
            summary=reply,
            files_touched=self._extract_files_touched(sub_session.messages),
            tool_count=self._count_tool_calls(sub_session.messages),
        )
```

### 4.4 System Prompts

```python
SUBTASK_SYSTEM_PROMPTS = {
    "explore": (
        "You are an exploration subagent. Search, read, and analyze files. "
        "NEVER modify files. Return a concise summary of what you found: "
        "list the relevant files, key code sections, and your conclusions."
    ),
    "plan": (
        "You are a planning subagent. Analyze the codebase and output a "
        "numbered implementation plan. Do NOT make any changes. "
        "Return the plan as structured markdown."
    ),
    "code": (
        "You are a coding subagent. Implement the requested changes. "
        "After completion, return a summary of what you changed, which files "
        "were modified, and any decisions worth noting."
    ),
}
```

### 4.5 Tool Handler Wiring

Replace the current stub in `tools/handlers.py:771-773`:

```python
# OLD
"task": lambda **kw: (
    "Error: The short-lived subagent task tool is not wired..."
),

# NEW
"task": lambda **kw: _run_subagent_task(
    prompt=kw["prompt"],
    agent_type=kw.get("agent_type", "explore"),
    description=kw.get("description", ""),
),
```

### 4.6 Files

| Action | File |
|--------|------|
| Create | `agents/subagent/__init__.py` |
| Create | `agents/subagent/runner.py` — TaskSubagentRunner |
| Create | `agents/subagent/tools.py` — tool whitelist + system prompts |
| Modify | `tools/handlers.py` — wire `task` handler |
| Modify | `runtime/bootstrap.py` — instantiate TaskSubagentRunner, inject into AgentLoop |
| Modify | `runtime/agent_loop.py` — inject `subagent_runner` |

---

## 5. Module 3: Structured Agent Communication Protocol (ACP)

### 5.1 Goal

Replace ad-hoc `{sender, content, type}` dicts with a typed message protocol that supports request-response correlation, timeout, and error propagation.

### 5.2 Message Types

```python
class MessageType(str, Enum):
    # Delegation (task assign + result)
    TASK_ASSIGN = "task_assign"
    TASK_RESULT = "task_result"
    TASK_PROGRESS = "task_progress"

    # Peer query
    QUERY = "query"
    RESPONSE = "response"

    # Plan review (two-phase handshake)
    PLAN_REQUEST = "plan_request"
    PLAN_RESPONSE = "plan_response"

    # Lifecycle
    SHUTDOWN_REQUEST = "shutdown_request"
    SHUTDOWN_RESPONSE = "shutdown_response"

    # Error
    ERROR = "error"

    # Broadcast
    BROADCAST = "broadcast"
```

### 5.3 AgentMessage Schema

```python
@dataclass
class AgentMessage:
    id: str                            # unique message ID (uuid hex:8)
    sender: str                        # agent name
    recipient: str                     # agent name, "lead", or "all"
    type: MessageType
    correlation_id: str | None = None  # links request ↔ response
    payload: dict = field(default_factory=dict)
    timestamp: float = field(default_factory=time.time)
    ttl_seconds: int = 300

    def to_json(self) -> str:
        return json.dumps({
            "id": self.id,
            "sender": self.sender,
            "recipient": self.recipient,
            "type": self.type.value,
            "correlation_id": self.correlation_id,
            "payload": self.payload,
            "timestamp": self.timestamp,
            "ttl_seconds": self.ttl_seconds,
        }, ensure_ascii=False)

    @classmethod
    def from_json(cls, data: str | dict) -> "AgentMessage":
        if isinstance(data, str):
            data = json.loads(data)
        return cls(
            id=data.get("id", ""),
            sender=data.get("sender", ""),
            recipient=data.get("recipient", ""),
            type=MessageType(data.get("type", "message")),
            correlation_id=data.get("correlation_id"),
            payload=data.get("payload", {}),
            timestamp=data.get("timestamp", time.time()),
            ttl_seconds=data.get("ttl_seconds", 300),
        )
```

### 5.4 Payload Schemas (per MessageType)

```python
# TASK_ASSIGN
{ "task_id": "task-abc123", "description": "...", "context": "...",
  "deadline": None, "priority": "normal" }

# TASK_RESULT
{ "task_id": "task-abc123", "status": "completed"|"failed",
  "summary": "...", "artifacts": [], "files_touched": [],
  "error": None }

# TASK_PROGRESS
{ "task_id": "task-abc123", "percent": 0.5,
  "message": "Analysed 3 of 6 modules..." }

# PLAN_REQUEST
{ "plan": "...", "rationale": "...", "affected_files": [] }

# PLAN_RESPONSE
{ "request_id": "...", "approved": true, "feedback": "...",
  "amendments": [] }

# ERROR
{ "task_id": "...", "error_type": "RuntimeError",
  "message": "...", "traceback": "..." }
```

### 5.5 ReliableMessageBus

```python
class ReliableMessageBus:
    """Adds request-response correlation on top of existing JSONL MessageBus."""

    def __init__(self, base_bus: MessageBus):
        self._bus = base_bus
        self._pending: dict[str, threading.Event] = {}
        self._responses: dict[str, AgentMessage] = {}
        self._lock = threading.Lock()

    def request(
        self, sender: str, to: str, msg_type: MessageType,
        payload: dict, *, timeout: float = 60,
    ) -> AgentMessage | None:
        """Send a request and block until response or timeout."""
        msg = AgentMessage(sender=sender, recipient=to,
                          type=msg_type, payload=payload)
        event = threading.Event()
        with self._lock:
            self._pending[msg.id] = event
        self._bus.send(sender, to, msg.to_json(), msg.type.value)
        if event.wait(timeout):
            with self._lock:
                return self._responses.pop(msg.id, None)
        return None  # timeout

    def respond(self, original: AgentMessage, msg_type: MessageType,
                payload: dict):
        """Respond to a request message."""
        response = AgentMessage(
            sender=original.recipient,
            recipient=original.sender,
            type=msg_type,
            correlation_id=original.id,
            payload=payload,
        )
        self._bus.send(response.sender, response.recipient,
                      response.to_json(), response.type.value)

    def notify_arrival(self, raw_message: dict):
        """Called by inbox reader when a response-type message arrives."""
        corr_id = raw_message.get("correlation_id")
        if corr_id:
            msg = AgentMessage.from_json(raw_message)
            with self._lock:
                self._responses[corr_id] = msg
                event = self._pending.pop(corr_id, None)
            if event:
                event.set()
```

### 5.6 Backward Compatibility

`AgentMessage.to_json()` produces valid JSON that can be written into the current JSONL inbox files. Old-format `{sender, content, type}` messages are still readable by `read_inbox()` as raw dicts. `AgentMessage.from_json()` gracefully degrades on old-format messages (treats `content` as a generic payload).

### 5.7 Files

| Action | File |
|--------|------|
| Create | `bus/protocol.py` — AgentMessage, MessageType, payload schemas |
| Create | `bus/reliable.py` — ReliableMessageBus |
| Modify | `bus/__init__.py` — exports |
| Create | `tests/test_bus_protocol.py` |

---

## 6. Module 4: Protocol Integration into Teammates

### 6.1 Goal

Replace ad-hoc handler code in `tools/handlers.py` (spawn_teammate, send_message, broadcast, plan_approval, etc.) with structured protocol calls via `AgentMessage`.

### 6.2 Teammate Inbox Processing (improved)

Current: `TeammateContextBuilder.build()` reads raw dicts, appends as-is.

New:

```python
class TeammateContextBuilder:
    def build(self, *, session, profile):
        raw = BUS.read_inbox(self.name)
        for item in raw:
            try:
                msg = AgentMessage.from_json(item)
                rendered = self._render_message(msg)
                session.messages.append({
                    "role": "user",
                    "content": rendered,
                    "metadata": {"msg_id": msg.id, "msg_type": msg.type.value},
                })
            except Exception:
                # Old-format message — pass through as-is
                session.messages.append({
                    "role": "user",
                    "content": json.dumps(item, ensure_ascii=False),
                })
        ...

    def _render_message(self, msg: AgentMessage) -> str:
        """Render an AgentMessage as human-readable context for the LLM."""
        type_labels = {
            MessageType.TASK_ASSIGN: "📋 New task assigned",
            MessageType.PLAN_RESPONSE: "📝 Plan review result",
            MessageType.QUERY: "❓ Query from teammate",
            MessageType.ERROR: "⚠️ Error report",
            MessageType.SHUTDOWN_REQUEST: "🛑 Shutdown request",
            ...
        }
        header = type_labels.get(msg.type, f"📨 Message ({msg.type.value})")
        return (
            f"<inbox-message sender=\"{msg.sender}\" type=\"{msg.type.value}\" "
            f"id=\"{msg.id}\" correlation_id=\"{msg.correlation_id}\">\n"
            f"{header} from {msg.sender}:\n"
            f"{json.dumps(msg.payload, ensure_ascii=False, indent=2)}\n"
            f"</inbox-message>"
        )
```

### 6.3 Error Propagation

Current: `TeammateManager._run_member()` catches all, `print()` only.

New:

```python
def _run_member(self, name, role, prompt):
    ...
    try:
        state = self._run_reasoning_cycle(...)
    except Exception as e:
        RELIABLE_BUS.send(
            name, "lead",
            AgentMessage(
                sender=name, recipient="lead",
                type=MessageType.ERROR,
                payload={
                    "error_type": type(e).__name__,
                    "message": str(e),
                    "traceback": traceback.format_exc(),
                },
            ).to_json(),
            "error",
        )
        self._set_status(name, "error")
        return
```

### 6.4 Plan Approval (two-phase → proper handshake)

Current: `plan_approval_request` → BUS.send → lead inbox → lead calls `plan_approval` → BUS.send → teammate inbox → teammate reads on next idle poll.

New: Teammate calls `plan_approval_request`, then calls `read_inbox` with a short blocking wait for `PLAN_RESPONSE`:

```python
# Teammate handler
"plan_approval_request": lambda **kw: _request_plan_approval(
    sender=name,
    plan=kw["plan"],
    timeout=60,
),
```

### 6.5 Files

| Action | File |
|--------|------|
| Modify | `tools/handlers.py` — update `send_message`, `spawn_teammate`, `plan_approval` handlers to use AgentMessage |
| Modify | `coding_runtime/teammate.py` — error propagation, structured inbox rendering |
| Modify | `coding_runtime/protocols.py` — add ReliableMessageBus-based plan + shutdown flow |
| Create | `tests/test_teammate_protocol.py` |

---

## 7. Module 5: Fan-out Enhancement (Phase 2)

### 7.1 Goal

Add a `parallel_tasks` tool that runs multiple subagent tasks concurrently in threads, waits for all to complete, and returns aggregated results.

### 7.2 Tool Schema

```python
function_tool(
    "parallel_tasks",
    "Run multiple subagent tasks concurrently and return aggregated results.",
    {
        "tasks": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "agent_type": {"type": "string", "enum": ["explore", "code", "plan"]},
                    "prompt": {"type": "string"},
                    "description": {"type": "string"},
                },
                "required": ["agent_type", "prompt"],
            },
            "description": "List of subagent tasks to run in parallel.",
            "maxItems": 8,
        },
    },
    ["tasks"],
)
```

### 7.3 Implementation

```python
def run_parallel_tasks(tasks: list[dict]) -> str:
    """Run multiple subagent tasks concurrently, collect results."""
    results: list[SubagentResult | None] = [None] * len(tasks)
    threads = []

    def worker(index, task):
        try:
            results[index] = SUBAGENT_RUNNER.run(
                prompt=task["prompt"],
                agent_type=task.get("agent_type", "explore"),
                ...
            )
        except Exception as e:
            results[index] = SubagentResult(
                agent_type=task.get("agent_type", "explore"),
                success=False, summary="", files_touched=[],
                error=f"{type(e).__name__}: {e}",
            )

    for i, task in enumerate(tasks):
        t = threading.Thread(target=worker, args=(i, task), daemon=True)
        threads.append(t)
        t.start()

    for t in threads:
        t.join(timeout=300)  # 5-minute per-task cap

    # Aggregate
    return json.dumps([r.__dict__ for r in results], ensure_ascii=False, indent=2)
```

### 7.4 Files

| Action | File |
|--------|------|
| Modify | `tools/schema.py` — add `parallel_tasks` to LEAD_ONLY_TOOLS |
| Modify | `tools/handlers.py` — wire `parallel_tasks` handler |
| Create | `agents/subagent/parallel.py` — `run_parallel_tasks` |

---

## 8. Glue: Bootstrap Changes

```python
# runtime/bootstrap.py

from agents.subagent.runner import TaskSubagentRunner
from bus.protocol import ReliableMessageBus

def build_runtime() -> AppRuntime:
    ...
    # NEW: Reliable message bus wrapping existing BUS
    reliable_bus = ReliableMessageBus(BUS)

    # NEW: Subagent runner
    subagent_runner = TaskSubagentRunner(
        base_pipeline=pipeline,
        sessions=sessions,
        workspace_resolver=workspace_resolver,
    )

    # MODIFIED: AgentLoop gets subagent_runner
    loop = AgentLoop(
        bus,
        sessions,
        pipeline,
        router,
        plugin_manager,
        task_session_runner,
        subagent_runner,     # NEW
        trace_store,
    )
    ...
```

---

## 9. File Structure (Post-Implementation)

```
agents/
├── coding/                    # existing TaskSessionRunner
│   ├── runner.py
│   ├── session.py
│   └── ...
├── subagent/                  # NEW
│   ├── __init__.py
│   ├── runner.py              # TaskSubagentRunner
│   ├── tools.py               # tool whitelist + system prompts
│   ├── parallel.py            # run_parallel_tasks (Phase 2)
│   └── __pycache__/

bus/
├── __init__.py
├── team_bus.py                # existing MessageBus (unchanged)
├── user_bus.py                # existing (unchanged)
├── protocol.py                # NEW: AgentMessage, MessageType, payload schemas
└── reliable.py                # NEW: ReliableMessageBus

runtime/
├── agent_loop.py              # MODIFIED: decomposed into 7 phase methods
├── agent_runner.py            # unchanged
├── agent_spec.py              # unchanged
├── reasoning_loop.py          # unchanged
├── pipeline.py                # unchanged
├── bootstrap.py               # MODIFIED: inject subagent_runner + reliable_bus
└── ...

tests/
├── test_agent_loop_phases.py     # NEW
├── test_bus_protocol.py          # NEW
├── test_subagent_runner.py       # NEW
├── test_teammate_protocol.py     # NEW
└── test_parallel_tasks.py        # NEW
```

---

## 10. Risks

| Risk | Mitigation |
|------|------------|
| `AgentLoop` refactoring breaks existing behavior | Pure decomposition — no logic changes. Each phase method copy-pasted from original, then trimmed. All existing tests must pass. |
| Subagent tool recursion | Tool whitelist explicitly excludes `task` and `spawn_teammate` for all subagent types |
| ReliableMessageBus threading complexity | Uses simple `threading.Event` per request. Timeout prevents deadlock. Single `_lock` covers shared dicts. |
| Old-format BUS messages break | `AgentMessage.from_json()` falls back gracefully. Old `read_inbox()` still works for legacy consumers. |
| Memory growth from subagent sessions | Subagent sessions are not persisted (no call to `sessions.save()`). Created in-memory only, garbage collected after `run()` returns. |
