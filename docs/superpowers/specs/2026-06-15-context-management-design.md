# Context Management Improvement Design

**Date**: 2026-06-15
**Status**: Approved
**Scope**: Compaction fix, budget system repair, token management, context frame injection, reflection optimization, conversation history quality

---

## 1. Problem Summary

15 issues identified across 6 context-related files. Organized into 6 modules.

### 1.1 Files Analysed

| File | Lines | Role |
|------|-------|------|
| `runtime/context.py` | 758 | ContextBuilder: assembles system prompt + memory + history + RAG + instructions → ContextBundle |
| `runtime/context_budget.py` | 234 | Section-level budget enforcement (off by default) |
| `runtime/context_history.py` | 510 | Conversation history truncation + active turn compaction |
| `runtime/context_sections.py` | 85 | Report dataclasses |
| `runtime/compact.py` | 81 | micro_compact (mutate-in-place) + auto_compact (destructive LLM summary) |
| `runtime/reflection.py` | 150 | ReflectionAgent for stuck/risky reasoning states |

### 1.2 Current Context Assembly Flow

```
Pipeline.run()
├── _before_turn()
│   ├── micro_compact(session.messages)     ← mutate-in-place: hide old tool results
│   └── auto_compact(session.messages)      ← if >50K chars: DESTROY ALL, replace with LLM summary
│
├── _before_reasoning()
│   ├── micro_compact(session.messages)     ← AGAIN
│   ├── auto_compact(session.messages)      ← AGAIN
│   └── ContextBuilder.build()
│       ├── _build_instruction_block()       ← read .agent/coding.md from disk (every time)
│       ├── _build_system_prompt()           ← profile + instructions + runtime_guidance
│       ├── _build_memory_block()            ← <memory>...</memory>
│       ├── _build_retrieved_history_block() ← vector search on history
│       ├── _build_security_knowledge_block()← router → RAG search (once per turn)
│       ├── _build_task_runtime_events()     ← inbox + background results
│       ├── budgeter.apply() on each section  ← DEFAULT OFF
│       └── _build_context_frame()           ← all blocks joined as one user message
│
└── ReasoningLoop
    ├── ReflectionAgent.should_reflect() → every step after step 6
    └── No pre-call token budget check
```

### 1.3 Issue Summary

| # | Issue | Severity |
|---|-------|----------|
| 1 | Two compaction systems run independently, masked if both truncate | 🔴 High |
| 2 | Budget system defaults to off — elaborate section logic is dead code | 🟡 Medium |
| 3 | `_build_report()` takes 30 named parameters | 🟡 Medium |
| 4 | `auto_compact()` is destructive, expensive, and silently truncates input | 🔴 High |
| 5 | `estimate_tokens()` too coarse (JSON chars ÷ 4) | 🟡 Medium |
| 6 | No pre-model-call token budget check | 🟡 Medium |
| 7 | Context frame injected as single user message | 🟡 Medium |
| 8 | Task events dumped as raw JSON wasting tokens | 🟢 Low |
| 9 | Security RAG once-per-turn state fragile on router exception | 🟢 Low |
| 10 | Security RAG hit rendering doesn't differentiate score tiers | 🟢 Low |
| 11 | Reflection LLM called after every step ≥ 6 | 🔴 High |
| 12 | `_summary_message()` destroys tool-call semantics | 🟡 Medium |
| 13 | `KEEP_RECENT=3` hides bash results too aggressively | 🟢 Low |
| 14 | Instruction files read from disk every turn, no cache | 🟢 Low |
| 15 | `_runtime_guidance()` hardcoded, not extensible by plugins | 🟢 Low |

---

## 2. Module 1: Unified Compaction Pipeline

### 2.1 Problem

`micro_compact()` mutates `session.messages` in-place BEFORE ContextBuilder runs, then `auto_compact()` may also fire. Budget system has its own truncation logic that runs even later. Three layers of downsizing with no coordination.

### 2.2 Solution

**Merge compaction into ContextBuilder as the single source of truth.** Remove mutation from Pipeline — compaction becomes a budget stage inside `build()`, not a pre-processing step.

```python
class ContextBuilder:
    def build(self, *, session, profile, ...) -> ContextBundle:
        # OLD (in Pipeline._before_turn and _before_reasoning):
        #   micro_compact(session.messages)  ← mutate session
        #   auto_compact(session.messages)   ← replace all
        #
        # NEW: Session messages are READ-ONLY during context build.
        # All compaction happens on copies within budget_* functions.

        session_messages = list(session.messages)  # snapshot
        ...
```

### 2.3 Specific Changes

**A. Move micro_compact into budget layer**

Replace `micro_compact()`'s mutate-in-place approach with a budget strategy:

```python
# context_budget.py — new strategy
class SectionBudgetRule:
    strategy: str  # add "replace_old_tool_results"
    preserve_tools: tuple[str, ...]   # tool names to always keep
    keep_recent_results: int           # how many recent tool results to preserve
```

**B. Make auto_compact non-destructive**

Instead of replacing ALL messages with an LLM summary:

```python
def auto_compact(messages: list, max_chars: int) -> list:
    """Return a REDUCED COPY of messages. Never mutate session in place."""
    if estimate_chars(messages) <= max_chars:
        return list(messages)

    # Strategy: keep head turns + tail turns, summarize middle, preserve tool chains
    groups = _group_turns(messages)
    head = groups[:3]       # first 3 turns
    tail = groups[-6:]      # last 6 turns
    middle = groups[3:-6]

    if not middle:
        return _copy_messages(messages)

    summary = _summarize_middle_turns(middle)  # LLM call, but only when needed
    return _flatten(head) + [summary] + _flatten(tail)
```

**C. Remove compaction from Pipeline.** These lines go away:

```python
# pipeline.py — REMOVE from _before_turn and _before_reasoning:
micro_compact(session.messages)
auto_compact(session.messages)
session.mark_compacted()
```

Instead, the budgeter handles it:

```python
# In ContextBuilder.build():
budgeted_history = budget_conversation_history(
    history_messages,
    enabled=self.budgeter.enabled,
    rule=self.budgeter.rules.get("conversation_history"),
)
# budget_conversation_history already does head-tail preservation + middle summary
# Micro-compaction of old tool results happens inside budget_active_turn
```

### 2.4 Files

| Action | File |
|--------|------|
| Remove | `compact.py` — micro_compact + auto_compact |
| Move logic | `context_history.py` — absorb micro_compact into `_reduce_active_turn` |
| Modify | `runtime/pipeline.py` — remove compaction calls from `_before_turn` and `_before_reasoning` |
| Modify | `config.py` — remove `KEEP_RECENT`, `PRESERVE_RESULT_TOOLS`, `THRESHOLD`, `TRANSCRIPT_DIR` (moved to budget rules) |
| Modify | `runtime/context_budget.py` — add `preserve_tools` and `keep_recent_results` to SectionBudgetRule |

---

## 3. Module 2: Budget System Activation

### 3.1 Problem

`CONTEXT_ENABLE_SECTION_BUDGET` defaults to `False`. The 9 `SectionBudgetRule`s and 4 truncation strategies (`head`, `tail`, `head_tail`, `summary_middle`, `latest_tool_call`) are dead code by default.

### 3.2 Solution

**Enable by default with conservative budgets that match actual model context windows.**

```python
# context_budget.py
DEFAULT_TOTAL_BUDGET_CHARS = 24000  # unchanged

@classmethod
def from_env(cls) -> "ContextBudgeter":
    enabled = _env_bool("CONTEXT_ENABLE_SECTION_BUDGET", default=True)  # was False
    ...
```

Budget values re-tuned for real-world usage:

| Section | Old default | New default | Rationale |
|---------|-------------|-------------|-----------|
| `mode_instructions` | 3000 | 3000 | OK |
| `project_instructions` | 3000 | 3000 | OK |
| `memory` | 2500 | 2000 | Memory should be terse |
| `retrieved_history` | 3000 | 2500 | Fewer hits, higher quality |
| `security_knowledge` | 4000 | 3000 | Reranker will reduce needed hits |
| `task_runtime_events` | 2000 | 1500 | Aggregated format uses fewer chars (Module 6) |
| `conversation_history` | 12000 | 10000 | Head-tail preservation already keeps essential context |
| `active_turn` | 8000 | 8000 | OK |
| **Total** | — | **24000** | Fits in ~6K tokens safely + system prompt overhead |

### 3.3 Fallback: Hard budget gate

Add a pre-call token check (Module 4) as the final safety net for when section budgets don't catch it.

### 3.4 Files

| Action | File |
|--------|------|
| Modify | `runtime/context_budget.py` — default `enabled=True`, re-tuned budgets |
| Modify | `.env.example` — document new defaults |

---

## 4. Module 3: Pre-Call Token Guard

### 4.1 Problem

After ContextBuilder assembles messages, ReasoningLoop sends them directly to the model. No check that the total fits within the model's context window.

### 4.2 Solution

Add a token estimator and guard in ReasoningLoop before the model call.

**A. Better token estimator**

Replace `len(str(messages)) // 4` with a provider-aware approach:

```python
def estimate_tokens(messages: list[dict], provider=None) -> int:
    """Estimate token count using provider's tokenizer if available."""
    # Prefer provider's native token counter
    if provider is not None and hasattr(provider, "count_tokens"):
        return provider.count_tokens(messages)

    # Fallback: sum content chars only (not JSON overhead)
    total = 0
    for msg in messages or []:
        content = msg.get("content", "")
        if isinstance(content, str):
            total += len(content)
        elif isinstance(content, list):
            total += sum(len(block.get("text", "")) for block in content if isinstance(block, dict))
        # tool_calls contribute ~20% overhead on top of content
        if msg.get("tool_calls"):
            total += len(str(msg["tool_calls"])) // 3
    return total // 3  # ~3 chars per token for English/mixed content
```

**B. Pre-call guard in ReasoningLoop**

```python
# reasoning_loop.py — _reasoning_step
def _reasoning_step(self, ..., context, ...):
    messages = context.messages
    estimated_tokens = estimate_tokens(messages, provider=provider)

    context_limit = getattr(provider, "context_limit", 128000)
    safe_limit = int(context_limit * 0.85)  # 85% for response space

    if estimated_tokens > safe_limit:
        # Emergency: trim from the middle, preserving head+tail
        messages = _emergency_trim(messages, safe_limit)
        self._trace(trace_store, run_state, "context_emergency_trim", {
            "before_tokens": estimated_tokens,
            "after_tokens": estimate_tokens(messages),
            "safe_limit": safe_limit,
        })

    # Continue with (possibly trimmed) messages
    response = method(model=model, messages=messages, ...)
```

**C. Emergency trim strategy**

```python
def _emergency_trim(messages: list[dict], max_tokens: int) -> list[dict]:
    """Last-resort trim: keep system + head turns + tail turns, cut middle."""
    system_msg = messages[0] if messages and messages[0].get("role") == "system" else None
    body = messages[1:] if system_msg else messages

    groups = _group_turns(body)
    if len(groups) <= 3:
        # Can't trim — return as-is and hope
        return messages

    head = groups[:2]
    tail = groups[-3:]
    trimmed = []
    if system_msg:
        trimmed.append(system_msg)
    trimmed.extend(_flatten(head))
    trimmed.append({
        "role": "user",
        "content": "[Emergency context trim: middle turns omitted to fit model context window.]",
    })
    trimmed.extend(_flatten(tail))
    return trimmed
```

### 4.3 Files

| Action | File |
|--------|------|
| Create | `runtime/token_estimator.py` — `estimate_tokens()` |
| Modify | `runtime/reasoning_loop.py` — pre-call guard in `_reasoning_step()` |
| Modify | `runtime/compact.py` — remove old `estimate_tokens()` (replace with import) |

---

## 5. Module 4: Reflection Optimization

### 5.1 Problem

`ReflectionAgent.should_reflect()` returns `True` for every step after step 6, causing excessive LLM calls. A 20-step task ~14 reflection calls.

### 5.2 Solution

**A. Rate-limit reflection calls**

```python
class ReflectionAgent:
    def __init__(self, ..., reflection_interval: int = 5):
        self.reflection_interval = max(2, int(reflection_interval))

    def should_reflect(self, *, session, profile, response, execution,
                       reasoning_steps: int) -> bool:
        # Immediate triggers (always check)
        if execution.loop_guard_denied:
            return True
        if execution.unavailable_tools:
            return True
        if any(item.get("status") != "success" for item in execution.tool_results):
            return True

        # Periodic check only
        if reasoning_steps < self.min_reasoning_steps:
            return False
        return reasoning_steps % self.reflection_interval == 0
```

This cuts calls from ~14 to ~3 per 20-step task.

**B. Increase min_reasoning_steps**

Default from 6 → 10. Reflection is most useful mid-to-late in a task, not at the beginning.

```python
# config.py
REFLECTION_MIN_REASONING_STEPS = int(os.getenv("REFLECTION_MIN_REASONING_STEPS", "10"))
```

**C. Make "revise" actionable**

When reflection returns `action="revise"`, instead of just injecting a `<reflection-instruction>` message that the model can ignore:

```python
def _apply_reflection(self, reflection_agent, ...):
    decision = reflection_agent.reflect(...)
    if decision.action == "revise":
        # Inject as system-level guidance, not just another user message
        instruction = (
            f"<reflection-instruction critical=\"true\">\n"
            f"REFLECTION: {decision.reason}\n"
            f"ACTION REQUIRED: {decision.instruction}\n"
            f"</reflection-instruction>"
        )
        session.messages.append({"role": "user", "content": instruction})
        # Also: log for observability
        self._trace(trace_store, run_state, "reflection_revise", {
            "reason": decision.reason,
            "instruction": decision.instruction,
        })
```

### 5.3 Files

| Action | File |
|--------|------|
| Modify | `runtime/reflection.py` — rate-limiting + critical tag |
| Modify | `config.py` — `REFLECTION_MIN_REASONING_STEPS` default 10, add `REFLECTION_INTERVAL` |

---

## 6. Module 5: Context Frame Restructuring

### 6.1 Problem

Memory, Retrieved History, Security Knowledge, and Task Events are joined as `\n\n` separated XML blocks inside a single `role: "user"` message. No priority signal to the model.

### 6.2 Solution

Keep the single user message format (compatible with all providers) but restructure into priority-ordered sections with explicit guidance:

```python
def _build_context_frame(self, *, memory_block, retrieved_history_block,
                         security_knowledge_block, task_runtime_events_block):
    sections = []

    # Priority 1: Security knowledge (time-sensitive, use once)
    if security_knowledge_block:
        sections.append(
            "<!-- PRIORITY: Use security knowledge as evidence for security questions. "
            "Cite source paths. -->\n"
            + security_knowledge_block
        )

    # Priority 2: Current task state (inbox, background)
    if task_runtime_events_block:
        sections.append(
            "<!-- Current runtime events. Check for new teammate messages or "
            "background task completions. -->\n"
            + task_runtime_events_block
        )

    # Priority 3: Retrieved history (reference only)
    if retrieved_history_block:
        sections.append(
            "<!-- Past conversation context. Reference when user asks about "
            "prior discussions. -->\n"
            + retrieved_history_block
        )

    # Priority 4: Memory (persistent preferences)
    if memory_block:
        sections.append(memory_block)

    return "\n\n".join(sections)
```

### 6.3 Task Events: Structured → Natural Language

Replace raw JSON dump:

```python
def _build_task_runtime_events_block(self, *, inbox, background_results):
    parts = []
    if inbox:
        for msg in inbox:
            sender = msg.get("sender", "unknown")
            msg_type = msg.get("type", "message")
            content = msg.get("content", "")
            # Render as natural language, not JSON
            parts.append(f"[inbox] {sender} ({msg_type}): {content}")

    if background_results:
        for n in background_results:
            parts.append(f"[bg:{n['task_id']}] {n['status']}: {n['result']}")

    if not parts:
        return ""

    return (
        "<task_runtime_events>\n"
        + "\n".join(parts)
        + "\n</task_runtime_events>"
    )
```

### 6.4 Security RAG: Score Tier Markers

```python
# In _build_security_knowledge_block:
for index, hit in enumerate(hits, start=1):
    tier = "HIGH" if hit.score >= 0.80 else "MEDIUM" if hit.score >= 0.60 else "LOW"
    lines.append(
        f"[{index}] score={hit.score:.4f} [{tier}] source={hit.source_relpath} title={hit.title}\n"
        f"{hit.text.strip()}"
    )
```

### 6.5 Security RAG: Fix Once-Per-Turn State

Move the state flag set to AFTER successful search, not at context build start:

```python
# pipeline.py
def _before_reasoning(self, ...):
    include_security_knowledge = not bool(
        session.metadata.get(SECURITY_RAG_AUTO_CONTEXT_USED_KEY)
    )
    context = self.agent_runner.context_builder.build(
        ...,
        include_security_knowledge=include_security_knowledge,
    )
    # MOVE: only mark as used if knowledge was actually injected
    if context.report:
        sec_section = context.report.section("security_knowledge")
        if sec_section and sec_section.rendered_chars > 0:
            session.metadata[SECURITY_RAG_AUTO_CONTEXT_USED_KEY] = True
```

### 6.6 Files

| Action | File |
|--------|------|
| Modify | `runtime/context.py` — `_build_context_frame()` restructured |
| Modify | `runtime/context.py` — `_build_task_runtime_events_block()` natural language rendering |
| Modify | `runtime/context.py` — `_build_security_knowledge_block()` score tiers |
| Modify | `runtime/pipeline.py` — `_before_reasoning()` state flag placement |

---

## 7. Module 6: History Summary Semantics

### 7.1 Problem

`_summary_message()` flattens tool calls into a plain text list, destroying the tool-call → tool-result association that the model relies on for reasoning continuity.

### 7.2 Solution

Preserve tool-chain structure in summaries by grouping:

```python
def _summary_message(messages, max_chars):
    """Summarize while preserving tool call → result pairing."""
    groups = _group_tool_chains(messages)  # NEW: group assistant+tool messages together

    lines = [
        "[Conversation history summary: middle turns were compressed by ContextBuilder.]",
        f"Compressed turns: {len(_group_turns(messages))}.",
    ]
    for turn_idx, group in enumerate(groups, 1):
        # Each "turn" is [user_msg, assistant_msg?, tool_result1?, tool_result2?, ...]
        user_msg = group[0] if group else {}
        user_text = _message_text(user_msg).strip().replace("\n", " ")
        if len(user_text) > 200:
            user_text = user_text[:197] + "..."

        lines.append(f"- Turn {turn_idx}: user asked: {user_text}")

        for msg in group[1:]:
            role = msg.get("role", "")
            if role == "assistant":
                if msg.get("tool_calls"):
                    tools = _tool_call_names(msg.get("tool_calls", []))
                    lines.append(f"    → called tools: {tools}")
                content = _message_text(msg).strip()
                if content:
                    if len(content) > 150:
                        content = content[:147] + "..."
                    lines.append(f"    → assistant said: {content}")
            elif role == "tool":
                tool_id = msg.get("tool_call_id", "")
                status = msg.get("status", "")
                content = _message_text(msg).strip()
                if len(content) > 120:
                    content = content[:117] + "..."
                lines.append(f"    ↳ [{tool_id}] {status}: {content}")

        if len("\n".join(lines)) >= max_chars:
            lines.append("...[summary truncated]")
            break

    return {"role": "user", "content": "\n".join(lines)}
```

### 7.3 KEEP_RECENT Tuning

Move from hardcoded `KEEP_RECENT=3` to configurable:

```python
# context_budget.py — in the active_turn SectionBudgetRule
keep_recent_results: int = 5  # was 3
preserve_tools: tuple[str, ...] = ("read_file", "git_diff", "git_status", "git_log")
```

### 7.4 Files

| Action | File |
|--------|------|
| Modify | `runtime/context_history.py` — `_summary_message()` tool-chain grouping |
| Modify | `runtime/context_budget.py` — `SectionBudgetRule` add `preserve_tools`, `keep_recent_results` |
| Modify | `config.py` — remove standalone constants |

---

## 8. Module 7: Instruction Caching

### 8.1 Problem

`.agent/coding.md` and `AGENTS.md` read from disk on every context build call — up to 24 times per turn.

### 8.2 Solution

```python
class ContextBuilder:
    _instruction_cache: dict[Path, tuple[float, str]] = {}  # path → (mtime, content)
    _cache_lock = threading.Lock()

    def _read_instruction_file(self, path: Path) -> tuple[str, str, bool]:
        if not path.is_file():
            return "", "", False

        with self._cache_lock:
            cached = self._instruction_cache.get(path)
            current_mtime = path.stat().st_mtime
            if cached is not None and cached[0] == current_mtime:
                cached_text = cached[1]
                if len(cached_text) <= self.instruction_limit:
                    return cached_text, cached_text, False
                return (
                    cached_text[:self.instruction_limit].rstrip() + "\n\n...[truncated]",
                    cached_text,
                    True,
                )

        # Read and cache
        try:
            text = path.read_text(encoding="utf-8", errors="replace").strip()
        except OSError:
            return "", "", False

        with self._cache_lock:
            self._instruction_cache[path] = (current_mtime, text)

        if len(text) <= self.instruction_limit:
            return text, text, False
        return (
            text[:self.instruction_limit].rstrip() + "\n\n...[truncated]",
            text,
            True,
        )
```

### 8.3 Plugin Guidance Registry

Make `_runtime_guidance()` extensible:

```python
class ContextBuilder:
    _guidance_registry: list[str] = [
        "Use recall_memory when the user asks about prior preferences or project conventions.",
        "Use memorize when the user states a durable preference or important fact.",
        "Some tools are deferred. Use tool_search to find or unlock tools that are not currently visible.",
        "Automatic security RAG may be injected only once at the start of a user turn; use security_rag_search if more local security knowledge is needed later.",
    ]

    @classmethod
    def register_guidance(cls, text: str) -> None:
        """Plugins call this to add runtime guidance for the model."""
        if text not in cls._guidance_registry:
            cls._guidance_registry.append(text)

    def _runtime_guidance(self) -> str:
        return "\n".join(self._guidance_registry)
```

### 8.4 Files

| Action | File |
|--------|------|
| Modify | `runtime/context.py` — `_read_instruction_file()` add mtime cache |
| Modify | `runtime/context.py` — `_runtime_guidance()` use registry |
| Modify | `runtime/context.py` — add `register_guidance()` classmethod |

---

## 9. Module 8: _build_report Signature Simplify

### 9.1 Problem

`_build_report()` has 30 named parameters. Adding a new section requires touching 3 places.

### 9.2 Solution

Use a `BuildState` dataclass:

```python
@dataclass
class _BuildState:
    # Messages
    messages: list[dict]
    session_messages: list[dict]
    history_messages: list[dict]
    active_turn_messages: list[dict]
    active_turn_start_index: int | None
    current_request: str

    # Profile & instructions
    profile_prompt: str
    instruction_sections: list[ContextSection]
    runtime_guidance: str
    system_prompt: str

    # Budgeted sections
    budgeted_history: BudgetedMessages
    budgeted_active_turn: BudgetedMessages
    budgeted_memory: BudgetedText
    budgeted_retrieved_history: BudgetedText
    budgeted_security_knowledge: BudgetedText
    budgeted_task_runtime_events: BudgetedText

    # Raw blocks (for report metadata)
    memory_block: str
    raw_memory_block: str
    retrieved_history_block: str
    raw_retrieved_history_block: str
    retrieved_hits: list
    security_knowledge_block: str
    raw_security_knowledge_block: str
    security_decision: Any | None
    security_hits: list
    task_runtime_events: str
    raw_task_runtime_events: str
    context_frame: str

    # Other
    inbox: list
    background_results: list
    reductions: list[dict[str, Any]]


def _build_report(self, state: _BuildState) -> ContextBuildReport:
    """Build report from state — single parameter, easy to extend."""
    sections = [
        ContextSection.from_text("system_profile", state.profile_prompt),
        *state.instruction_sections,
        ...
    ]
```

### 9.3 Files

| Action | File |
|--------|------|
| Create | `runtime/context_build_state.py` — `_BuildState` dataclass |
| Modify | `runtime/context.py` — `build()` populates `_BuildState`, `_build_report()` takes single param |

---

## 10. File Structure (Post-Implementation)

```
runtime/
├── context.py                  # MODIFIED: unified flow, instruction cache, guidance registry, score tiers
├── context_budget.py           # MODIFIED: enabled by default, re-tuned budgets, preserve_tools
├── context_history.py          # MODIFIED: absorbed micro_compact, tool-chain summary, keep_recent configurable
├── context_sections.py         # unchanged
├── context_build_state.py      # NEW: _BuildState dataclass
├── token_estimator.py          # NEW: provider-aware token estimation
├── compact.py                  # REMOVED (logic absorbed elsewhere)
├── reasoning_loop.py           # MODIFIED: pre-call token guard
├── reflection.py               # MODIFIED: rate-limiting, critical tag on revise
├── pipeline.py                 # MODIFIED: remove compaction calls, fix security RAG flag placement
└── bootstrap.py                # MODIFIED: remove compact-related config

config.py                       # MODIFIED: remove KEEP_RECENT, PRESERVE_RESULT_TOOLS, THRESHOLD, TRANSCRIPT_DIR

tests/
├── test_context_budget.py      # NEW: budget enabled by default, section limits
├── test_token_estimator.py     # NEW: estimation accuracy
├── test_context_instruction_cache.py  # NEW: mtime-based cache
└── test_reflection_rate_limit.py      # NEW: interval-based reflection
```

---

## 11. Risks

| Risk | Mitigation |
|------|------------|
| Removing `micro_compact()` from Pipeline may change context sizes | `budget_active_turn` with `keep_recent_results=5` achieves same effect but via budget layer not mutation |
| Budget enabled by default may over-truncate | Conservative defaults (re-tuned from real-world); override via env vars |
| Pre-call emergency trim may cut important middle context | Only fires when >85% of model context window; preserves head+tail turns; logged for observability |
| Tool-chain summary loses precision vs full messages | Summary only applies to middle (compressed) turns; head 3 + tail 6 are preserved intact |
| Reflection rate-limiting may miss real problems | Immediate triggers (loop_guard, tool errors) still check every step; only periodic check is rate-limited |
