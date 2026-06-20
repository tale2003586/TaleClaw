# Cross-Cutting System Audit: Findings & Improvement Design

**Date**: 2026-06-16
**Status**: Audit complete
**Scope**: Memory, Session, Tool, Model/Provider, Routing, Plugin, Web, Configuration, Workspace, Error Handling, Test Coverage
**Previous audits**: [RAG](2026-06-15-rag-improvement-design.md), [Agent Collaboration](2026-06-15-agent-collaboration-design.md), [Context Management](2026-06-15-context-management-design.md)

---

## 1. Memory System

### 1.1 `MemoryStore.recall()` is a Stub

**Severity**: 🔴 High
**File**: `memory/store.py:176-181`

```python
def recall(self, query: str | None = None) -> str:
    text = self.read_all()
    if not query:
        return text
    # 第一版先不做检索，直接返回全文。
    return text
```

The `recall_memory` tool returns ALL memory content regardless of query. As memory grows (PENDING.md, MEMORY.md, SELF.md, NOW.md, HISTORY.md), this becomes a context-bloating full dump that the model must parse entirely. There is no vector search, no keyword filtering, no section selection.

**Fix**: Route `recall_memory` through `history_vector_index` with a `scope` derived from the session. Memory files should be embedded and indexed during `after_turn`, and `recall` should be a vector search, not a file read.

### 1.2 Memory Candidate Pipeline is Dead by Default

**Severity**: 🔴 High
**File**: `memory/processor.py:57-73`, `memory/vector_runtime.py:11-16`

`MemoryProcessingDevice.process_user_description()` depends on `history_vector_index`, which requires `HISTORY_VECTOR_ENABLED=true` (default `False`). When disabled, the entire candidate memory pipeline returns empty results. This means:
- No automatic candidate extraction from user messages
- No pattern detection across turns
- No promotion from candidate → confirmed memory
- `_candidate_memory_enabled()` returns `True` but the pipeline is a no-op

**Fix**: Either default `HISTORY_VECTOR_ENABLED` to `True`, or implement a lightweight fallback (keyword-based) when vector index is off.

### 1.3 `CandidateMemoryStore` is O(n) on Every Write

**Severity**: 🟡 Medium
**File**: `memory/candidates.py:54-122`

Every `upsert()` reads the full JSON file, deserializes all candidates, modifies one, serializes all, writes the full file. With hundreds of candidates, this becomes I/O-heavy. No incremental append, no index.

**Fix**: Use SQLite (like `MemoryArchiveStore` already does) instead of a JSON file for candidate storage. Or batch writes with a dirty flag.

### 1.4 `_render_messages_for_embedding()` Embeds JSON Noise

**Severity**: 🟡 Medium
**File**: `memory/lifecycle.py:228-240`

```python
def _render_messages_for_embedding(self, messages):
    for message in messages:
        parts.append(f"{role}: {_compact_json_or_text(content)}")
        for tool_call in message.get("tool_calls") or []:
            parts.append(f"{role}.tool_call: {_compact_json_or_text(tool_call)}")
```

When `content` is not a string, `_compact_json_or_text` calls `json.dumps()`, producing structured JSON in the embedding text. This dilutes the semantic signal with syntax characters.

**Fix**: Extract only the text fields from structured content before embedding.

### 1.5 Memory Dedup is Purely Lexical

**Severity**: 🟡 Medium
**File**: `memory/dedup.py`

`normalize_memory_text()` strips punctuation and lowercases, then exact-matches. "Use pytest for testing" and "Use pytest to run tests" will NOT be caught as duplicates (different token sets after normalization).

**Fix**: Use embedding cosine similarity with a high threshold (≥0.92) as a second-pass dedup check for near-duplicates.

### 1.6 `_MEMORY_KEYWORDS` Hardcoded

**Severity**: 🟢 Low
**File**: `memory/candidates.py:208-222`

The keyword set `{"pytest", "测试", "代码风格", ...}` is hardcoded and contains domain-specific terms ("同人文", "人物细节") that suggest they were copied from a specific use case. Not extensible.

**Fix**: Move to env-configurable list or load from a config file.

### 1.7 `MemoryArchiveStore` Shares DB with `SessionStore`

**Severity**: 🟢 Low
**File**: `memory/archive_store.py:34-37`

Both use `sessions.db` by default. Different concerns (session state vs. archived memory) sharing one database file creates unnecessary coupling and makes backup/restore harder.

**Fix**: Give `MemoryArchiveStore` its own default database path.

### 1.8 `HistorySummarizer.__init__` Constructs ModelTaskRunner from provider+model

**Severity**: 🟢 Low
**File**: `memory/history_summary.py:21-26`

```python
if self.runner is None and provider is not None and model:
    self.runner = ModelTaskRunner(provider=provider, model=model, ...)
```

But in `bootstrap.py`, the `HistorySummarizer` is constructed with `runner=model_task_runner` and `spec=AgentSpec(...)`. The `provider` and `model` parameters in `__init__` are never used — dead code paths.

---

## 2. Session Management

### 2.1 DELETE-ALL + INSERT-ALL on Every Save

**Severity**: 🔴 High
**File**: `sessions/session_store.py:113-135`

```python
self._conn.execute(sql(self.config, "DELETE FROM messages WHERE session_id = ?"), (session.id,))
execute_many(self._conn, self.config, "INSERT INTO messages ...", [...])
```

Every `save_session()` deletes ALL messages for that session and re-inserts them. For a session with 500 messages, this is 501 SQL statements per save, called after every turn. This is O(n) write amplification.

**Fix**: Track a `last_saved_seq` and only INSERT new messages since last save.

### 2.2 `SessionManager.get_or_create()` Memory Leak

**Severity**: 🟡 Medium
**File**: `sessions/session.py:52-67`

Sessions are cached in `self._sessions` dict forever. No eviction, no TTL. A long-running server with many users/sessions will grow this dict unboundedly.

**Fix**: Add an LRU eviction policy or TTL-based cleanup.

### 2.3 SQLite `check_same_thread=False`

**Severity**: 🟡 Medium
**File**: `runtime/db.py:62`

```python
conn = sqlite3.connect(str(config.sqlite_path), check_same_thread=False, timeout=10)
```

This allows multiple threads to use the same connection. While the `_lock` in `SessionStore` protects most operations, the connection object itself is not thread-safe for all operations, and SQLite in WAL mode would be a safer choice.

**Fix**: Use WAL mode (`PRAGMA journal_mode=WAL`) or create a connection per thread.

### 2.4 No Session Expiry/Cleanup

**Severity**: 🟢 Low
**File**: `sessions/session.py`

Sessions have `created_at` and `updated_at` but nothing ever reads them to expire old sessions. The database and disk storage grow unboundedly.

**Fix**: Add a background thread or cron job that deletes sessions older than N days of inactivity.

---

## 3. Tool System

### 3.1 `ToolPolicy.requires_approval()` Always False

**Severity**: 🟡 Medium
**File**: `tools/policy.py:166-175`

The method signature exists (accepts tool_name, args, session, mode, run_context) but always returns `False`. The approval workflow (dangerous tool → ask user → execute) is fully stubbed.

**Fix**: Either implement the approval workflow or remove the stub to avoid false expectations.

### 3.2 `ToolLoopGuardHook` Too Strict on Exact Fingerprint

**Severity**: 🟡 Medium
**File**: `tools/hooks.py:99-112`

`_fingerprint()` hashes `{tool_name, arguments}`. The model calling `read_file(path="a.py")` then `read_file(path="b.py")` produces different fingerprints. But the model calling `bash(command="pytest tests/test_a.py -x")` then `bash(command="pytest tests/test_b.py -x")` also produces different fingerprints — the model could be in a test-and-fix loop calling `bash` 10 times with slightly different arguments, and this guard won't catch it.

**Fix**: Add a tool-name-only repetition counter as a second check: if the same tool is called >5 times in a window regardless of arguments, flag it.

### 3.3 `ShellSafetyHook` Blocklist Too Narrow

**Severity**: 🟡 Medium
**File**: `tools/hooks.py:21-23`

```python
dangerous = ["rm -rf /", "sudo", "shutdown", "reboot", "> /dev/"]
```

Missing: `rm -rf .`, `rm -rf *`, `mkfs`, `dd if=`, `chmod 777`, `:(){ :|:& };:`, `/dev/sda`, etc. A blocklist approach to shell safety is fundamentally incomplete.

**Fix**: Consider a sandbox approach (Docker container, chroot, or restricted user) rather than pattern-matching.

### 3.4 `DEFERRED_TOOLS` Includes `task` and `parallel_tasks`

**Severity**: 🟢 Low
**File**: `tools/policy.py:66-82`

The `task` tool is in `DEFERRED_TOOLS` but not in `PRELOADED_TOOLS_BY_MODE["coding"]`. This means the lead agent must call `tool_search select:task` before it can spawn a subagent — adding a friction step to the most important tool.

**Fix**: Move `task` to `PRELOADED_TOOLS_BY_MODE["coding"]`.

---

## 4. Model/Provider System

### 4.1 Config Module-Level Side Effects

**Severity**: 🔴 High
**File**: `config.py:8-43`

`config.py` executes at import time:
- `load_dotenv(override=True)` — can change env vars globally
- Sets `HTTP_PROXY`/`HTTPS_PROXY` globally — affects ALL HTTP clients in the process
- Builds `MODEL_POOL` (which validates env vars and may raise)
- Runs `health_check_purposes()` if enabled — makes API calls at import time

Any error here crashes the process before `main()` can even set up logging.

**Fix**: Move side-effectful initialization to a `build_runtime()` or `init()` function called explicitly. `config.py` should only define constants/defaults.

### 4.2 `MODEL_HEALTHCHECK_RESULTS` Runs at Import

**Severity**: 🟡 Medium
**File**: `config.py:39-43`

If `LLM_HEALTHCHECK_ON_STARTUP=true`, module import makes blocking API calls. This means `python -c "import config"` can hang for seconds.

**Fix**: Move healthcheck to `bootstrap.py`, not module-level.

### 4.3 `SYSTEM` Prompt Captured at Import

**Severity**: 🟢 Low
**File**: `config.py:83-87`

```python
SYSTEM = f"""You are a team lead at {WORKDIR}. ...
Skills available:
{SKILL_LOADER.get_descriptions()}"""
```

`SKILL_LOADER.get_descriptions()` is called once at import. If skills are added/removed at runtime, this string is stale until the process restarts.

**Fix**: Make it a function `def get_system_prompt()` called at context build time.

### 4.4 `ModelTaskRunner` No Error Handling

**Severity**: 🟡 Medium
**File**: `models/model_task_runner.py:20-35`

`run()` calls `provider.chat()` without try/except. If the model API fails, the exception propagates to the caller. Callers (`HistorySummarizer`, `CandidateMemoryExtractor`) each have their own try/except — inconsistent error handling.

**Fix**: Either add error handling in `ModelTaskRunner.run()` or document that callers must handle errors themselves.

### 4.5 `RoutedModelProvider` Failover on Streaming Side-Effect

**Severity**: 🟢 Low
**File**: `models/model_pool.py:391-393`

```python
if stream and emitted:
    raise  # Don't retry if user already saw partial output
```

This is correct behavior but undocumented. If a streaming call fails mid-response after emitting text, the error is raised immediately rather than trying fallbacks. Callers may not expect this.

**Fix**: Document in docstring.

---

## 5. Routing System

### 5.1 `IntentClassifier` is Keyword-Only

**Severity**: 🟡 Medium
**File**: `runtime/routing/intent.py`

All classification is substring matching. Examples:
- `"文件"` + `"实现"` triggers coding intent regardless of context
- "帮我查找一下文件" triggers coding intent (contains `"文件"` + `"查找"` → matches `"文件"` in artifact_keywords)
- No semantic understanding of the user's actual intent

**Fix**: Add an embedding-based fallback to the keyword classifier, similar to the security RAG router's two-stage design. Or use the `HybridModeClassifier` (LLM-based) earlier in the pipeline.

### 5.2 Mode Switch Detection Duplicated

**Severity**: 🟢 Low
**File**: `runtime/routing/intent.py:21-41` vs `runtime/routing/execution_plan.py:27-68`

`IntentClassifier.classify()` detects `/coding`, `/chat`, `/hybrid` commands and sets `command`. `ExecutionPlanner.plan()` checks `command` again. The logic is split across two classes for no clear reason.

**Fix**: Consolidate mode switch detection into `IntentClassifier` only; `ExecutionPlanner` should only map intent→execution.

---

## 6. Plugin System

### 6.1 `PluginManager.register()` Mutates Plugin Object

**Severity**: 🟡 Medium
**File**: `plugins/plugin_manager.py:41`

```python
setattr(plugin, "_plugin_manager", self)
```

This adds a private attribute to the plugin object. If the plugin is a frozen dataclass or has `__slots__`, this fails. It's also a violation of encapsulation.

**Fix**: Store the back-reference in a `WeakKeyDictionary` keyed by plugin instance, or pass `PluginManager` as a parameter to hook methods.

### 6.2 Plugin Registration is Additive-Only

**Severity**: 🟢 Low
**File**: `plugins/plugin_manager.py:33-57`

No `unregister()` method. If a plugin needs to be hot-reloaded or disabled, there's no mechanism.

**Fix**: Add `unregister(plugin_name)` that reverses the registration steps.

---

## 7. Web/API Layer

### 7.1 `ThreadingHTTPServer` Not Production-Grade

**Severity**: 🟡 Medium
**File**: `web/server.py`

`ThreadingHTTPServer` from stdlib has known limitations: no connection pooling, no graceful shutdown, no rate limiting, no TLS, susceptible to slow-loris attacks.

**Fix**: For production, use uvicorn/FastAPI or at minimum wrap with a reverse proxy (nginx/caddy). Document this limitation.

### 7.2 Deprecated `cgi` Module

**Severity**: 🟢 Low
**File**: `web/server.py:28`

```python
import cgi  # deprecated in Python 3.11, removed in 3.13
```

The `cgi` module is used for parsing multipart form data. It will break on Python 3.13+.

**Fix**: Replace with `email.parser` + `urllib.parse` or use a library like `python-multipart`.

---

## 8. Error Handling (Codebase-Wide)

### 8.1 Excessive Bare `except Exception` Usage

**Severity**: 🟡 Medium
**Scope**: 43 files, ~200 occurrences

Many `except Exception` blocks silently swallow errors:

```python
# memory/processor.py:126-127
except Exception:
    return []

# models/model_pool.py:390
except Exception as exc:
    ...
```

The most problematic patterns:
- `tools/handlers.py`: 18 `except Exception` blocks, many return `f"Error: {e}"` — useful but inconsistent (some return error strings, some return empty dicts, some print)
- `coding_runtime/teammate.py:161`: `except Exception as e: print(f"[{name}] Error: {e}")` — teammate crash is invisible to lead

**Fix**: Adopt a consistent error handling pattern: log → trace → return structured error. Add a custom `AppError` base class for expected errors vs unexpected exceptions.

### 8.2 No Structured Logging

**Severity**: 🟢 Low
**Scope**: Entire codebase

All output uses `print()`. No log levels, no timestamp prefix, no structured format, no log file rotation. Debugging production issues requires grep'ing stdout.

**Fix**: Adopt `logging` module with a simple stdout + file handler configuration.

---

## 9. Workspace

### 9.1 `safe_workspace_path()` Doesn't Check Symlinks

**Severity**: 🟡 Medium
**File**: `runtime/workspace.py:104-112`

The function rejects `..` in paths but doesn't resolve symlinks before checking. A symlink inside the workspace pointing to `/etc/passwd` would pass the check.

**Fix**: Resolve the full path before the containment check: `target.resolve()` then `is_relative_to(root.resolve())`.

### 9.2 Workspace Not Resolved for `_session=None`

**Severity**: 🟢 Low
**File**: `runtime/workspace.py:104-112`

`safe_workspace_path(path, session=None)` falls back to `workspace_root_for_session(None)` which returns `DEFAULT_CODING_WORKSPACE`. If a code path forgets to pass session, it silently gets the default workspace rather than raising an error.

**Fix**: Require `session` parameter; raise `ValueError` if None.

---

## 10. Test Coverage Gaps

**Severity**: 🟡 Medium

39 test files is good breadth, but critical paths lack coverage:

| Gap | Risk |
|-----|------|
| No `ModelPool` fallback chain test | Provider failover may be broken without detection |
| No `SessionManager` concurrent access test | SQLite threading issues only surface under load |
| No `PluginManager` lifecycle test (register→before_turn→after_turn→after_run) | Plugin ordering bugs not caught |
| No `WorkspaceResolver` boundary test (symlinks, non-existent paths) | Path traversal may be possible |
| No `MemoryLifecycle` end-to-end test (turn→candidate→promotion→recall) | Memory pipeline bugs only found in production |
| No `CandidateMemoryStore` concurrent write test | JSON file corruption under multi-thread access |

---

## 11. Priority Summary

| Priority | Issue | Subsystem |
|----------|-------|-----------|
| 🔴 P0 | `MemoryStore.recall()` returns all memory — no retrieval | Memory |
| 🔴 P0 | Memory candidate pipeline dead by default (`HISTORY_VECTOR_ENABLED=false`) | Memory |
| 🔴 P0 | `config.py` module-level side effects (proxy, model pool, healthcheck) | Config |
| 🔴 P0 | `SessionStore` DELETE-ALL + INSERT-ALL on every save | Session |
| 🟡 P1 | `CandidateMemoryStore` O(n) JSON read/write per operation | Memory |
| 🟡 P1 | Memory embedding dilutes signal with JSON noise | Memory |
| 🟡 P1 | Memory dedup is purely lexical, misses semantic near-duplicates | Memory |
| 🟡 P1 | Session memory leak — no eviction from `_sessions` dict | Session |
| 🟡 P1 | SQLite `check_same_thread=False` without WAL mode | Session |
| 🟡 P1 | `ToolPolicy.requires_approval()` always False — stubbed | Tool |
| 🟡 P1 | `ToolLoopGuardHook` doesn't catch repeated-tool-name pattern | Tool |
| 🟡 P1 | `IntentClassifier` is keyword-only, no semantic understanding | Routing |
| 🟡 P1 | `ShellSafetyHook` blocklist too narrow | Tool |
| 🟡 P1 | 43 files with bare `except Exception`, many swallow silently | Error |
| 🟡 P1 | `ThreadingHTTPServer` not production-grade | Web |
| 🟡 P1 | `safe_workspace_path()` doesn't check symlinks | Workspace |
| 🟡 P1 | Critical test coverage gaps (ModelPool fallback, Session concurrency) | Test |
| 🟢 P2 | `PluginManager` mutates plugin object via `setattr` | Plugin |
| 🟢 P2 | `config.py` `SYSTEM` prompt stale if skills change at runtime | Config |
| 🟢 P2 | `cgi` module deprecated, will break on Python 3.13+ | Web |
| 🟢 P2 | No session expiry/cleanup | Session |
| 🟢 P2 | No structured logging — all `print()` | Error |
| 🟢 P2 | `HistorySummarizer.__init__` dead code paths | Memory |
| 🟢 P2 | `_MEMORY_KEYWORDS` hardcoded and domain-specific | Memory |
| 🟢 P2 | `task` tool in DEFERRED_TOOLS — adds friction | Tool |
| 🟢 P2 | Mode switch detection duplicated across two classes | Routing |
| 🟢 P2 | `MemoryArchiveStore` shares DB with `SessionStore` | Memory |
