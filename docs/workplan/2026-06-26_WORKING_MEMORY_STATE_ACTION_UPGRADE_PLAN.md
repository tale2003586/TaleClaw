# Working Memory 状态-动作升级方案与工作计划

> 状态：已落地 P0/P1/P2-1；P2-2 压缩回调写 ledger 保留为后续评估项。
> 目标：把 coding 模式的 working memory 从"结论+待办的有损摘要"升级为"agent 的显式状态机"，用来 (1) 从源头压制重复工具调用，(2) 强化"状态→下一步动作"的控制力。
> 前置：本方案建立在 [2026-06-19_WORKING_MEMORY_RESUME_DESIGN.md](2026-06-19_WORKING_MEMORY_RESUME_DESIGN.md) 已落地的检查点机制之上，不改主循环架构。

---

## 1. 背景与问题定位

### 1.1 当前链路（事实）

coding task 跑在隔离 task session 里，一个 turn 内 `conversation_history` 基本为空，体量几乎全在 `active_turn`（工具调用/结果原文）。证据：

- task session 只塞一条 user 消息后跑单 turn（[agents/coding/runner.py:123-147](../../agents/coding/runner.py#L123-L147)）。
- `active_turn_start_index` 指向那唯一一条 user 消息（[runtime/pipeline.py:121](../../runtime/pipeline.py#L121)），`_split_active_turn` 因此切出空 history（[runtime/context.py:1063-1071](../../runtime/context.py#L1063-L1071)）。

working memory 现状：

- 仅 coding 模式注入（[runtime/context.py:417-422](../../runtime/context.py#L417-L422)）。
- 已在自然边界打粗粒度检查点，存 `completed_units / pending_units / archived_findings / step_checkpoints`（[runtime/working_memory.py:183-267](../../runtime/working_memory.py#L183-L267)）。
- `step_checkpoints` 里其实存了每步的 `tool_calls / tool_results`，但 `render_working_memory_block` 只渲染了工具名，没渲染"已观察到什么"（[runtime/working_memory.py:379-392](../../runtime/working_memory.py#L379-L392)）。

### 1.2 根因

"重复工具调用反复出现"的根因不在 WM 本身，而在两点没接起来：

1. **active turn 一旦压缩，模型就忘了自己读过什么**（压缩是有损的，旧 tool result 被截断/摘要）。
2. **WM 存了观察痕迹，却几乎没渲染出来**，模型看不到"我已经在 offset=0 读过 foo.py 了"，于是再读一次。

现有 `ToolLoopGuardHook`（[tools/hooks.py:98-196](../../tools/hooks.py#L98-L196)）是**事后反应式**防线：重复 20 次、或拿到 3 次相同结果才拦截。我们要的是**事前约束**——让模型在产生下一步动作前就知道"这个调用没必要"。这正是 WM 该承担的职责。

### 1.3 设计原则（边界，不可越界）

- **WM 是状态，不是内容存储**。observation ledger 只存一行 gist（≤200 字符），不存工具结果原文。原文留在 active turn。一旦 ledger 存原文，就退化成"WM 接管 active turn"，会撑爆 WM 预算（`WORKING_MEMORY_CONTEXT_BUDGET=4000`，[config.py:59](../../config.py#L59)）并引入摘要漂移。
- **热留原文，冷进 ledger**：active turn 里的最新结果逐字保留；被压缩掉的旧结果转写成 ledger 一行。
- **指令性增强必须有结构可指**：先建 ledger 和有序队列（P0/P1），命令式协议（P2）才有抓手，否则又是空话。
- **不改主循环架构**，所有改动集中在 `working_memory.py` + 一个共享指纹函数 + 一处压缩回调。

---

## 2. 目标数据结构

在 `WorkingMemory`（[runtime/working_memory.py:33-71](../../runtime/working_memory.py#L33-L71)）新增/升级字段：

```python
@dataclass
class WorkingMemory:
    task_id: str
    objective: str
    completed_units: list[dict]      # 不变：[{unit_id, conclusion, evidence_refs}]
    pending_units: list[dict]        # 升级：加 priority / state / blocked_by
    archived_findings: dict          # 不变
    step_checkpoints: list[dict]     # 不变（trace 用途）
    observed_calls: list[dict]       # 新增：observation ledger
    last_checkpoint_step: int
    status: str
    updated_at: str
```

### 2.1 observed_calls（observation ledger）

每项：

```python
{
    "signature": "<共享指纹: tool + 关键 args>",
    "tool": "read_file",
    "gist": "含 parse_config / load_model 两个函数",  # ≤200 字符
    "step": 7,
    "info_gain": True,   # 结果是否带来新信息（与上次相同结果则 False）
}
```

- `signature` 复用 loop guard 的指纹逻辑（[tools/hooks.py:205-214](../../tools/hooks.py#L205-L214) 的 `_fingerprint`），抽成共享函数保证两边口径一致。
- 去重：同 signature 只保留最新一条 + 出现次数计数。
- 上限：保留最近 N 条（建议 30），超出按 step 淘汰最旧。

### 2.2 pending_units 升级

每项新增：

```python
{
    "unit_id": "unit-3",
    "description": "...",
    "scope_files": [...],
    "priority": "P0",            # 新增 P0/P1/P2
    "state": "in_progress",      # 新增 todo / in_progress / blocked / 自动从 status 映射
    "blocked_by": ["unit-1"],    # 新增，可选
    "last_failure_reason": "",
}
```

向后兼容：`from_payload` 对缺字段给默认值（priority=P1, state=todo, blocked_by=[]）。

---

## 3. 渲染升级（render_working_memory_block）

目标输出结构（替换 [runtime/working_memory.py:341-400](../../runtime/working_memory.py#L341-L400)）：

```
<working-memory task_id="..." status="running">
原始任务: ...
最后检查点步骤: 7

下一步动作队列（有多个独立 ready units 时优先 parallel_tasks）:
→ NEXT [P0] unit-3: 修改 parse_config 支持 yaml    state=in_progress  scope=src/config.py
↔ PARALLEL [P0] unit-4: 检查 model loader 边界      state=todo   scope=src/model.py
  [P1] unit-5: 给 parse_config 加测试             state=todo   blocked_by=unit-3
  [P2] unit-7: 更新 README                         state=todo

已观察（不要重复这些调用，直接用结论）:
- read_file(src/config.py, offset=0): 含 parse_config / load_model 两个函数
- grep("TODO", src/): 命中 3 处，均在 tests/
- list_files(src/model): 12 个文件，无 __init__.py

已完成:
- [unit-1] 定位配置入口
  结论: 配置在 src/config.py:parse_config
  证据: src/config.py

归档发现: (保留现状，截断渲染)
</working-memory>

<working-memory-protocol critical="true">
每个推理步开始前，按顺序执行：
1. 读"已观察"账本。如果相同 signature 已观察过，且目标文件/目录未被修改，优先复用结论；如果相关文件已被修改，则允许重读。
2. 如果队列中有 "PARALLEL" 或多个非 blocked 且 scope 不重叠的 ready units，优先调用 parallel_tasks 批量分派；每个子任务必须有清晰 scope、objective、deliverable。
3. 如果没有可并行项，推进 NEXT；若 NEXT blocked，跳到下一个非 blocked ready unit。
4. 子任务结果返回后，逐个更新 unit 结论/状态，再决定下一轮是否继续 parallel_tasks。
5. 最终汇总必须合并 completed_units 的所有结论。
</working-memory-protocol>
```

要点：

- `step_checkpoints` 那段冗长渲染（当前 [working_memory.py:379-392](../../runtime/working_memory.py#L379-L392)）降级或移除，由 observation ledger 取代——ledger 带 args + 结果要点，比"只有工具名"有用得多。
- 队列排序：state=in_progress 优先 → priority 升序 → 非 blocked 优先；第一个非 blocked 项标 `→ NEXT`，scope 不重叠的后续 ready 项标 `↔ PARALLEL`。
- 整块仍受 `WORKING_MEMORY_CONTEXT_BUDGET` 约束，ledger 与队列各设软上限，溢出时优先保 NEXT 与 completed 结论。

---

## 4. 写入时机（接到压缩路径）

关键是"冷进 ledger"：当 active turn 把一个旧工具结果压缩掉时，往 `observed_calls` 写一条 gist。

- 写入点 A（主）：`checkpoint_reasoning_step` 已在每步收 `tool_results`（[runtime/working_memory.py:234-267](../../runtime/working_memory.py#L234-L267)），在此处把本步 tool_calls/tool_results 转写成 ledger 条目（gist 由结果首行/摘要截断生成）。
- 写入点 B（可选增强，后续阶段）：在 `runtime/context_history.py` 压缩旧 tool result 时（`_compress_old_tool_results`），回调 WM 写 ledger，使 ledger 与 active turn 压缩严格同步。本阶段先用 A，B 列入 P2 评估。
- `info_gain` 判定：复用 loop guard 的 result hash 思路（[tools/hooks.py:282-289](../../tools/hooks.py#L282-L289)），同 signature 结果 hash 不变则 `info_gain=False`。

---

## 5. 工作计划（按优先级分阶段）

### P0 — observation ledger（收益最高，直接压重复调用）

| # | 任务 | 落点 | 验收 |
|---|---|---|---|
| P0-1 | 抽共享指纹函数 `tool_call_signature(tool, args)` | 新建 `runtime/tool_signature.py`，`hooks.py` 与 `working_memory.py` 共用 | hook 行为不变，单测覆盖指纹一致性 |
| P0-2 | `WorkingMemory` 加 `observed_calls` 字段 + `from_payload` 兼容 | [working_memory.py:33-68](../../runtime/working_memory.py#L33) | 旧 payload 反序列化不报错，字段默认空 |
| P0-3 | `checkpoint_reasoning_step` 写 ledger（去重+gist+info_gain+上限） | [working_memory.py:234-267](../../runtime/working_memory.py#L234) | 每步后 ledger 含本步调用，重复调用只一条+计数 |
| P0-4 | `render_working_memory_block` 渲染"已观察"段 | [working_memory.py:341-400](../../runtime/working_memory.py#L341) | 渲染含 tool(args): gist，受预算约束 |
| P0-5 | 单测：ledger 写入/去重/渲染/预算裁剪 | `tests/test_working_memory_*.py` | 全绿 |

### P1 — next-action 队列

| # | 任务 | 落点 | 验收 |
|---|---|---|---|
| P1-1 | pending_units 加 priority/state/blocked_by + 兼容默认 | working_memory.py upsert/from_payload | 旧数据默认 P1/todo |
| P1-2 | 渲染有序队列 + `→ NEXT` 标记 + blocked 标注 | render_working_memory_block | 排序正确，仅一个 NEXT |
| P1-3 | checkpoint_subtask_* 写入 priority/state（从 task/result 推导） | [working_memory.py:156-231](../../runtime/working_memory.py#L156) | 子任务状态正确流转 |
| P1-4 | 单测：队列排序、NEXT 选择、blocked 过滤 | tests | 全绿 |

### P2 — 命令式协议 + 压缩同步（可选）

| # | 任务 | 落点 | 验收 |
|---|---|---|---|
| P2-1 | 替换散文指令为 `<working-memory-protocol>` 步骤协议 | [working_memory.py:393-399](../../runtime/working_memory.py#L393) | 渲染含 4 步协议 |
| P2-2 | 评估写入点 B：context_history 压缩回调 WM | context_history.py + working_memory.py | ledger 与 active turn 压缩同步 |
| P2-3 | 行为评估：跑长 coding task 对比 duplicate_tool_call_ratio | trace metrics（[trace_store.py:263-265](../../runtime/trace/trace_store.py#L263)） | 比率下降 |

---

## 6. 验证策略

1. **单元**：每阶段配套 pytest，覆盖序列化兼容、去重、排序、预算裁剪。
2. **指标对照**：用现有 trace metric `duplicate_tool_call_ratio`（[runtime/trace/trace_store.py:263-265](../../runtime/trace/trace_store.py#L263)）做前后对比，这是本方案的核心成功指标。
3. **基线脚本（建议先做）**：跑一个长 coding task，打印 context report 各 section 的 `rendered_chars` 占比，确认瓶颈在 active_turn、ledger 注入后 WM 块未超预算。
4. **回归**：`WORKING_MEMORY_CHECKPOINT_ENABLED / WORKING_MEMORY_RESUME_ENABLED` 关闭时行为不变。

---

## 7. 风险与边界

| 风险 | 缓解 |
|---|---|
| ledger 存原文导致 WM 撑爆 | 强制 gist ≤200 字符，硬上限条数，单测断言总长 |
| 渲染膨胀超 WORKING_MEMORY_CONTEXT_BUDGET | 块内分段软上限，溢出优先保 NEXT + completed |
| 指纹两套口径不一致 | P0-1 抽共享函数，hook 与 WM 同源 |
| 旧 session metadata 反序列化失败 | from_payload 全字段默认值，单测覆盖旧 payload |
| 协议过强导致模型不敢并行正当任务 | 已把协议调整为：多个独立 ready units 时优先 `parallel_tasks`，NEXT 只作为单线任务的默认 |

---

## 8. 不做（subtraction）

- **不让 WM 接管 active turn**：active turn 工具结果原文保持逐字 + 现有压缩，WM 只做状态与 ledger。
- **不做步级精确记账**：检查点维持自然边界粗粒度。
- **不改 bot/assistant 模式**：本方案仅 coding 模式生效（注入已门控）。
- **不动 loop guard 拦截逻辑**：它退为兜底防线，参数不变。

---

## 9. 落点汇总

| 改动 | 文件 | 阶段 |
|---|---|---|
| 共享指纹函数 | 新建 `runtime/tool_signature.py` | P0 |
| `observed_calls` 字段 + 兼容 | `runtime/working_memory.py` | P0 |
| ledger 写入（checkpoint） | `runtime/working_memory.py` | P0 |
| ledger 渲染 | `runtime/working_memory.py` | P0 |
| pending_units 升级 + 有序队列渲染 | `runtime/working_memory.py` | P1 |
| 命令式协议块 | `runtime/working_memory.py` | P2 |
| 压缩回调写 ledger（评估） | `runtime/context_history.py` | P2 |
| 单测 | `tests/test_working_memory_*.py` | 各阶段 |
