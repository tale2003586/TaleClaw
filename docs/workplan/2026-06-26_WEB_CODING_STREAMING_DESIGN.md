# Web 端 Coding 模式流式活动输出设计

日期: 2026-06-26
目标: 在 web 控制台的 coding 模式提供 Codex / Claude Code 风格的实时活动流
（"正在 read_file X"、"正在跑 bash"、subagent 扇出、token/步数计数、文件 diff），
而不是当前的"思考中转圈 + 末尾一次性出全文"。

## 1. 现状（事实，已核对代码）

- `POST /api/chat/stream` 在 worker 线程跑 agent，把事件塞进 `queue.Queue`，
  目前只产出三类: `delta`(on_text 增量) / `complete` / `error`
  （[web/server.py:1485](../../web/server.py#L1485)）。
- coding 模式下 `on_text` 只在 `run_coding_task` 返回后被调用一次
  （[runtime/agent_loop.py:211-218](../../runtime/agent_loop.py#L211)），
  因此整轮没有任何中间可见输出。
- 运行时已持续发出细粒度 trace 事件到 `.runs/<run_id>/trace.jsonl`，
  通过单例 `TraceStore.append_event`（[runtime/trace/trace_store.py:41](../../runtime/trace/trace_store.py#L41)）。
- 关键事件已存在（[runtime/trace/events.py](../../runtime/trace/events.py) + reasoning_loop 字面量）:
  `run.started/completed/failed`、`reasoning.step.started/completed`、
  `model.call.started/completed/failed`(usage 带 token)、
  `tool.call.started/completed/failed`(tool_name / arguments_preview / output_preview / status / duration_ms)、
  `context.build.completed`(token 计数)、
  `subagent.started/completed`(agent_type/description/success/tool_count/reasoning_steps)、
  `workspace.diff.written`(created/modified/deleted summary)。
- 同一轮（主 agent + coding task + 所有 subagent）共享同一个 `run_id`，
  且都走同一个 `trace_store` 实例（subagent 的 RunState `run_id=parent.run_id`，
  见 [agents/subagent/trace.py](../../agents/subagent/trace.py)）。

结论: 事件粒度已经够用，缺的只是"把事件从磁盘 tee 到 SSE 流"的通道。

## 2. 架构决策

不在 pipeline 各层新增 `on_event` 参数（穿透 5 层、改动面大、易漏 subagent）。
改为在 `TraceStore` 上加进程内订阅，因为 `trace_store` 已贯穿全链路。

```
runtime 线程: append_event(run_state, name, payload)
   -> 写 trace.jsonl（原行为，不变）
   -> dispatch 给该 run 的订阅者（新增）
        -> server.py 的回调把事件投影成精简 DTO，放进 SSE queue.Queue（线程安全）
web 线程: queue.get() -> 写 NDJSON 行 -> 浏览器
```

订阅按 **session 维度** 注册（web 层只知道 session_key，不知道 run_id）：
- `TraceStore` 维护 `_run_to_session: {run_id -> top_session_id}`，在 `start_run` 时
  对顶层 run 填充。
- `append_event` 里 `owner = _run_to_session.get(run_state.run_id, run_state.session_id)`，
  派发给 `owner` 的订阅者。subagent 事件 `run_id` 相同 -> 自动归到同一个顶层 session，
  无需额外接线即可捕获 subagent 活动。

## 3. TraceStore 改动（runtime/trace/trace_store.py）

新增（用已有的 `self._lock` RLock 保护注册表，派发在锁外执行）:

```python
def subscribe(self, session_id: str, cb: Callable[[dict], None]) -> Callable[[], None]:
    with self._lock:
        self._subscribers.setdefault(session_id, []).append(cb)
    def _unsub():
        with self._lock:
            lst = self._subscribers.get(session_id, [])
            if cb in lst: lst.remove(cb)
    return _unsub
```

- `start_run`: `self._run_to_session[run_state.run_id] = run_state.session_id`
  （仅顶层 run；subagent 是 `trace_only` 且 run_id 相同，不覆盖）。
- `append_event` 末尾: 解析 owner session，复制订阅者列表，逐个 `try/except` 调用
  （回调异常绝不能影响 trace 写盘）。
- 派发的是已 `_json_safe` 且 `event_preview` 截断(500 字符)的 payload，
  不在这里做投影（投影放 web 层，保持 runtime 干净）。
- run 结束清理 `_run_to_session`（在 `write_report` 或 finish 时 pop）。

行数控制: 该文件已 478 行，新增约 40 行；若超 400 行阈值，把 pub/sub 拆到
`runtime/trace/trace_subscribers.py`，TraceStore 组合它（遵守小文件原则）。

## 4. AgentService / 流接口改动（web/server.py）

`_handle_chat_stream` 里，启动 agent 前订阅，结束后退订:

```python
unsub = self.agent_service.subscribe_session(session_key, lambda ev: events.put(
    {"type": "event", **_stream_event_projection(ev)}
))
# ... run_agent 同前 ...
# complete/error 后调用 unsub()
```

- `AgentService.subscribe_session(session_key, cb)` 转调 `runtime.loop.trace_store.subscribe`。
  `session_key = f"web:{web_chat_id(user_id, session_id)}"`（与 `_ask_async` 一致）。
- `_stream_event_projection(event)`: 白名单投影，按事件类型只取 UI 需要的字段，
  避免把 `context_report`、完整 `tool_calls`、大 payload 灌进流。例如:
  - `reasoning.step.started` -> `{event, step}`
  - `tool.call.started` -> `{event, step, span_id, parent_span_id, tool: tool_name, args: arguments_preview[:200]}`
  - `tool.call.completed/failed` -> `{event, step, span_id, tool, status, duration_ms, preview: output_preview[:200]}`
  - `model.call.completed` -> `{event, step, tokens: usage.total_tokens, model}`
  - `subagent.started/completed` -> `{event, span_id, parent_span_id, agent_type, description, success, tool_count, reasoning_steps}`
  - `workspace.diff.written` -> `{event, summary}`
  - 其它事件默认丢弃（白名单）。
- 该机制 mode 无关：hybrid/chat 也会同时有 token delta + 事件，体验一致。
- 排序与背压: trace 事件和 on_text delta 进同一个 `queue.Queue`，按到达顺序交错，天然有序。
- 取消: 现有 `/api/chat/stop` 不变；停下前事件继续流，停下后 `run.*` 收尾。
- 多标签页: 同 session 多订阅者 -> 都收到，符合预期。

## 5. 前端改动（web/static/app.js + index.html + styles.css）

`fetchJsonStream` 的 onEvent 增加 `event.type === "event"` 分支，维护一个
活动时间线（activity timeline），挂在流式 assistant 气泡上方。

- 顶部紧凑状态行（随事件更新）:
  `Step 4/24 · 12 tools · 32k tokens · 8.1s`
  （step 取 reasoning.step.started；tools 累加 tool.call.completed/failed；
  tokens 累加 model.call.completed；耗时前端本地计时）。
- 每个 reasoning step = 一个可折叠分组。组内每个 tool call 一行:
  - started: `● read_file path/to/x.py`（spinner）
  - completed: `✔ read_file · 1.2s`；failed: `✖ bash · error`
  - bash 显示命令、read_file 显示路径（取 args/preview）。
- subagent: 用 `span_id` / `parent_span_id` 建立层级，subagent 事件嵌套渲染成子分组
  （`agent_type · description`，带自己的 steps/tools 计数）。
- `workspace.diff.written` -> 渲染文件变更 chips（created/modified/deleted 数）。
- 收尾(complete): 时间线折叠成一行摘要 disclosure
  （`Worked 8.1s · 12 tools · 3 files changed`），下方按现有逻辑渲染最终 markdown 回复。
- 失败(error): 保留已收到的时间线 + 错误信息（现有 error 分支增强）。

复用现有 `tool-disclosure` 样式族，新增 `.activity-*` 类。

## 6. 分阶段实施（P0/P1）

- P0-1: TraceStore pub/sub + `_run_to_session` + 清理（runtime，纯增量、可单测）。
- P0-2: `AgentService.subscribe_session` + `_stream_event_projection` + stream handler 接线。
- P0-3: 前端 activity timeline 最小版（状态行 + 平铺工具行 + 收尾折叠）。
- P1-1: subagent 层级嵌套渲染。
- P1-2: workspace diff chips + 点击跳 run 详情页(`/runs/<id>`)。
- P1-3: 把同样的事件流接到 VS Code 插件 bridge（当前 bridge 是一次性 result.json，
  后续可改 JSONL，与本设计的投影格式对齐）。

## 7. 测试

- 单测: TraceStore `subscribe` 收到顺序事件；subagent(同 run_id 不同 session_id)
  事件能派发给顶层 session 订阅者；`unsub` 后不再收到；回调抛异常不影响写盘。
- 集成: 起 server，对一个 coding 任务 `curl -N /api/chat/stream`，断言收到
  `event`(含 tool.call.*) 且顺序在 `complete` 之前。
- 投影: `_stream_event_projection` 对各事件类型只含白名单字段、无大 payload、无 secret。

## 8. 风险与边界

- 性能: 派发在锁外、回调只做 `queue.put`；投影截断防止大 payload。
- 安全: 复用 `_json_safe`(脱敏) + `event_preview`(截断)；投影再次白名单，不泄露完整文件内容。
- 不改动 agent 执行层、不改 trace 落盘行为（纯旁路），可独立回滚。
- 顶层 run 必须先 `start_run` 再有子事件（现有顺序已满足）。
```
