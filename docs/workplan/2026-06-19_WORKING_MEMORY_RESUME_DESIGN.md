# 工作记忆与断点续做设计方案

> 状态：设计（未动工）
> 目标：为 coding 模式加入 (A) 用户主动停止思考、(B) 粗粒度断点续做（工作记忆）。
> 粒度决策：**子任务/线索级**检查点，不做步级。理由见 §6。

## 1. Context

当前 agent run 是"一次性"的：被 `reasoning_step_limit` 停、或用户想中途叫停时，**进度全部丢失**，重来要从零。前几次 run 已证明"部分结果有价值"（partial findings 可用），但现在这些中间产物活在内存里，run 结束即蒸发。

目标是把 run 从"一次性"变成"可中断、可续做"：
- **A 主动停止**：用户能在思考中途干净地叫停，拿到目前为止的结论。
- **B 断点续做**：被中断（用户停止 / 步数耗尽）的 run，下次能从最后一个检查点继续，不重做已完成的线索。

两者关系：A 是 B 的前提——能"干净地停并存下进度"，才能"从进度续"。先做 A，再做 B。

## 2. 现状盘点（地基已具备）

| 能力 | 现状 | 落点 |
|---|---|---|
| 推理主循环 | `while True` 每步开头查预算 | `runtime/reasoning_loop.py:90` |
| 优雅停止 | `_stop_turn` + `RunState.stop(reason)` | `reasoning_loop.py:850` |
| 停止原因枚举 | `StopReason` | `runtime/failure_reasons.py:5` |
| 会话落盘 | messages + metadata 存 SQLite | `sessions/session_store.py` |
| run 状态 | status/reasoning_steps/stop_reason | `runtime/trace/run_state.py:18` |

**结论**：循环检查点、停止机制、状态落盘都已存在。A 只需加"外部可置位的中断信号 + 每步检查"；B 只需加"工作记忆结构 + 在自然边界写盘 + resume 时注入上下文"。不改主循环架构。

## 3. A：用户主动停止（协作式中断）

### 原则：协作式取消，绝不强杀
不用 `kill thread`——会留下半写文件、半截工具调用、损坏 session。停止粒度 = **一个完整推理步**：不打断正在执行的工具（让它跑完），但不再进下一步。这样状态永远一致。

### 设计
1. **中断信号**：每个 session 关联一个取消标志（`threading.Event`，因 `parallel.py` 已用线程池）。存活在内存（不需落盘——它是瞬时控制信号）。
   - 落点：可挂在 `AgentLoop` 维护的 `{session_id: Event}` 表，或 `session.metadata` 引用。
2. **循环内检查点**：`reasoning_loop` 的 `while` 循环，在每步开头（紧邻 `_reasoning_budget_exceeded` 检查）加：
   ```python
   if self._cancel_requested(session):
       self._stop_turn(session, self._partial_summary(session),
                       reason=StopReason.USER_CANCELLED, ...)
       return
   ```
3. **停止时产出部分结论**：不空手而归。停止前让 agent（或一段确定性汇总）把"目前为止已确定的结论"作为 final_answer 返回。复用现有 `_stop_turn` 的 message 通道。
4. **信号通路**：web/bus 加一个"停止"入口 → 置位该 session 的 Event。
   - 落点：`bus`（新增一个 control 消息类型）或 web `server.py` 一个 `/stop` 端点。

### 新增
- `StopReason.USER_CANCELLED = "user_cancelled"`（`runtime/failure_reasons.py`）。
- `AgentLoop._cancel_events: dict[str, threading.Event]` + `request_cancel(session_id)` 方法。
- `reasoning_loop` 每步中断检查 + `_partial_summary`。

### 验收
用户在多步 run 中途触发停止 → 当前工具跑完后循环停止 → 返回带"目前为止结论"的 final_answer，`stop_reason=user_cancelled`，session 状态干净。

## 4. B：断点续做（工作记忆，粗粒度）

### 原则：存"结论与待办"，不存"执行过程"；resume 靠上下文重建，不恢复执行栈
- 工作记忆存"我已经知道什么 + 我还要做什么"，不存"我调了哪些工具"（那是 trace 的事）。
- resume = **新建一个 run，把工作记忆作为初始上下文注入**，让 agent 从"已知 X，待办 Y"继续。这是 LLM agent 的天然优势——状态即上下文。

### 数据结构 `WorkingMemory`
```python
@dataclass
class WorkingMemory:
    task_id: str
    objective: str                       # 原始任务
    completed_units: list[dict]          # [{unit_id, conclusion, evidence_refs}]
    pending_units: list[dict]            # [{unit_id, description, scope_files}]
    archived_findings: dict              # 子任务归档的 partial findings
    last_checkpoint_step: int
    status: str                          # running | suspended | completed
```
"unit" = 一条线索 / 一个子任务（粗粒度）。

### 检查点：在自然边界写盘，不是每步
触发点：
- 每个子任务（parallel_tasks 的一个 task）完成后；
- 每条用户线索完成后；
- 用户主动停止时（A 顺手打一个检查点 → 这就是 A 与 B 的衔接）；
- 步数预算耗尽 `_stop_turn` 时。

写盘位置：`session.metadata["working_memory"]`（复用现有 session 落盘），或独立 `working_memory` 表（若结构大）。建议先用 metadata，简单。

### resume 语义
1. 新 run / 显式 `resume` 命令加载该 task 的 `WorkingMemory`。
2. 把它渲染成上下文注入块：
   ```
   <working-memory>
   原始任务: ...
   已完成: 线索1=[结论], 线索2=[结论]
   归档发现: {...}
   待办: 线索3, 线索4
   </working-memory>
   指令：基于已完成部分，只处理待办线索，不要重做已完成的。
   ```
3. agent 从待办继续。已完成线索的结论直接进最终汇总，不重新探索。

### 新增
- `runtime/working_memory.py`：`WorkingMemory` + 序列化 / 反序列化 + 渲染成上下文块。
- 检查点写入：在 `parallel.py` 子任务完成处、`reasoning_loop` 停止处调用 `checkpoint(session, unit_result)`。
- resume 入口：`AppRuntime` / `agent_loop` 加载 working_memory 并在 `ContextBuilder` 注入。
- `ContextBuilder` 增加 `working_memory` section（复用现有 section 机制，`runtime/context_sections.py`）。

### 验收
一个四线索任务，完成线索 1、2 后被中断（停止或超限）→ working_memory 落盘含线索 1、2 结论 + 线索 3、4 待办 → resume → agent 只做线索 3、4 → 最终汇总含全部四条，且 trace 显示线索 1、2 未被重新探索。

## 5. 关键文件清单

- `runtime/failure_reasons.py` — `StopReason.USER_CANCELLED`
- `runtime/reasoning_loop.py` — 每步中断检查、停止时打检查点、partial 汇总
- `runtime/agent_loop.py` — cancel events 表、request_cancel、resume 加载
- `runtime/working_memory.py`（新）— WorkingMemory 结构 / 序列化 / 渲染
- `agents/subagent/parallel.py` — 子任务完成处写检查点
- `runtime/context.py` / `context_sections.py` — working_memory 上下文 section
- `sessions/session_store.py` — working_memory 落盘（用 metadata 或新表）
- `bus/` 或 `web/server.py` — 停止信号通路 + resume 命令
- `config.py` — 检查点开关、resume 开关

## 6. 取舍与边界

- **粗粒度（线索/子任务级）而非步级**：LLM agent 重做一个子任务的代价远低于维护步级检查点的复杂度与脆弱性；且粗粒度与已有的"子任务归档 partial findings"机制天然契合（归档即检查点）。步级 resume 收益有限、实现复杂，不做。
- **协作式中断，不强杀**：停在步与步之间，工具调用完整。保证 session/workspace 状态一致。
- **resume 靠上下文重建，不恢复执行栈**：不做进程级 checkpoint（极难且脆）。利用"状态即上下文"。
- **A 与 B 衔接**：用户停止时顺手打检查点，使"主动停止"的 run 自动可续。
- **中断信号是瞬时控制态，不落盘**；工作记忆是持久任务态，落盘。两者分开。
- 不改主循环架构、不动模型、不动路由本体。

## 7. 执行顺序

A1（StopReason + cancel event + 每步检查 + partial 汇总）
→ A2（停止信号通路：web/bus）
→ B1（WorkingMemory 结构 + 序列化落盘）
→ B2（自然边界写检查点：子任务完成 / 停止时）
→ B3（resume：加载 + 上下文注入 section）
→ 端到端验证（中断 → resume → 不重做）。
