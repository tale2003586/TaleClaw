# 主-子 Agent 失败处置与重试降级方案

> 状态：计划（未动工）
> 触发证据：`.runs/run_20260619-035728-f053b7`（第三次失败）
> 关系：承接 `SUBTASK_NARROWING_CODE_OUTLINE_PLAN.md`。失败**分类**已落地（`agents/subagent/failure.py` 生效），本文补的是分类之后的**消费端**——重试、降级、主 agent 处置——以及让失败本身减少的做窄前置。

## 1. Context（这次 run 证明了什么）

第三次复跑（`run_20260619-035728-f053b7`）：8 步、393 次工具调用、`repeated_tool_call` 停机、零产出。但 trace 暴露的事实**纠正了上一轮的误判**：

**通信契约没有 bug，失败分类已生效。** 主 agent 完整收到了 8 个子 agent 的结构化失败：
- 6 × `subagent_tool_error`（loop guard，retry_hint="Retry with a narrower task..."）
- 2 × `subagent_step_limit`（retry_hint="Split the task into a smaller scope..."）
- 每条都带 `recoverable: true` + `retry_hint` + `files_touched`。

真正的三个问题：

| # | 问题 | 证据 |
|---|---|---|
| A（主因）| 子 agent 自己在死循环：做窄未落地 | 8 子 agent tool_count = 65/65/65/54/38/31/24/19；失败多为 `repeated_tool_call`（自撞 loop guard）；step3 prompt 仍是"Investigate the main execution path... for each directory describe"宽任务 |
| B | 主 agent 收到 retry_hint 却不消费 | 全部 `recoverable: true`，但 step4 主 agent 反应是"switch to direct reading myself"，未按 hint 重试/降级 |
| C | 主 agent 自己重复 list_files | step5/6/8 反复 list 同批目录，step8 第三次触发 `repeated_tool_call` 停机（P0-B context 丢弃已读未修）|

一句话：**失败诊断做好了，但没人消费诊断；而且失败本身（子 agent 超限）还没被"做窄"减少。**

## 2. 改进方案

> 顺序原则：先让失败**可处置**（B/C），再让失败**少发生**（A 的做窄）。因为现在诊断已在手，处置逻辑是最小改动、最快见效的一环。

### P0-1 主 agent 失败处置规则写进 system prompt（消费 retry_hint，解决 B）
- 落点：`modes/coding.py` `CODING_PROFILE.system_prompt`。现在只有"怎么选执行体"，缺"子任务失败后怎么办"。
- 新增规则（按 `failure_reason` 决策）：
  ```
  当 parallel_tasks/task 返回 success=false 时，先读每个结果的 failure_reason / recoverable / retry_hint，再决定：
  - recoverable=true 且 reason ∈ {subagent_step_limit, subagent_scope_too_broad}:
      → 按 retry_hint 把该线索拆成更窄的子任务重试一次（点名具体文件、用 code_outline 读大文件）。
  - recoverable=true 且 reason = subagent_tool_error (loop guard):
      → 重试时必须改变方法（换 code_outline / 换文件 / 减少范围），禁止原样重发。
  - reason ∈ {subagent_missing_required_files, subagent_empty_findings} 或 infeasible:
      → 不重试，记录该线索无法完成的原因，继续其他线索。
  - 已重试过一次仍失败 → 不再派子 agent，主 agent 直接处理该线索或如实标注未完成。
  禁止：忽略 retry_hint 直接退回自己全量 read/list（这会烧光主预算）。
  ```
- 验收：构造子任务失败的 run，主 agent 至少发生一次"按 hint 重试更窄任务"，而非立即自己重读。

### P0-2 确定性重试/降级包装（机械部分自动化，解决 B 的可靠性）
- 落点：`agents/subagent/parallel.py` 或新增 `agents/subagent/retry.py`。
- 把"纯机械、不需要语义"的重试自动化，**不依赖主 agent 每次判断对**：
  - `INTERNAL_ERROR` / provider 瞬时失败：**自动原样重试 1 次**（可能是瞬时故障）。
  - `TIMEOUT`：自动重试 1 次（不改任务）。
  - `STEP_LIMIT` / `TOOL_ERROR` / `SCOPE_TOO_BROAD`：**不自动重试**——这些需要"改变任务内容"，交给主 agent（P0-1）按语义决策。
  - `MISSING_REQUIRED_FILES` / `EMPTY_FINDINGS` / `infeasible`：**不重试**，直接终结。
- 全局预算：每次 `parallel_tasks` 调用内，自动重试总数 ≤ 任务数；单任务自动重试 ≤ 1。到顶即停，把最终失败如实返回主 agent。
- 边界：**纯机械的归包装（瞬时故障重试），需要改任务内容的归主 agent（语义降级）**。不要在包装里做"自动把任务改窄"——那需要语义，会出错。
- 验收：注入一个 INTERNAL_ERROR 子任务，包装自动重试 1 次；注入 STEP_LIMIT，包装不重试、原样上报。

### P0-3 降级阶梯定义（解决 B 的"降级到哪"）
- 落点：P0-1 prompt 规则 + `runtime/failure_reasons.py` 可加阶梯常量。
- 单向递进，禁止横跳：
  ```
  窄 subagent  --STEP_LIMIT/再次失败-->  更窄的多个 subagent（基于 code_outline 点名文件）
               --仍失败-->                spawn_teammate（50步+完整上下文）
               --teammate 也不行-->        主 agent 自己做 / 如实上报该线索未完成
  ```
- 每条线索一个降级预算（默认最多 2 次升级），到顶如实汇报，不回头。

### P1-1 落地 code_outline + 侦察兵契约（解决 A，让失败少发生）
- **直接引用** `SUBTASK_NARROWING_CODE_OUTLINE_PLAN.md` 的 P0-A（code_outline 工具）、P1-A（侦察兵契约 + findings schema）、P1-B（主 agent 分层切分）。
- 本次 run 再次证明其必要性：没有 code_outline，子 agent 对 `store.py`(1827)/`passive_turn.py`(1821) 这类文件必然 read→截断→重读→自撞 loop guard（tool_count 65）。
- 这是降低"子 agent 失败率"的根本手段；P0-1~3 只是让失败可处置，不减少失败。

### P1-2 context budget 保护已读（解决 C）
- **直接引用** `SUBTASK_NARROWING_CODE_OUTLINE_PLAN.md` 的 P0-B（`_compress_old_tool_results` 保护近期 read/list/repo_map 结果）。
- 本次 run 主 agent 在 step8 因重复 list_files 停机，正是 C 的表现。

### P1-3 重试/降级可观测性
- 落点：`runtime/trace/summary.py` metrics。
- 新增 `subagent_retry_count`、`subagent_degrade_count`、`subagent_infeasible_count`、`subagent_recovered_count`（重试后转成功的数量）。
- 用于回归验证"重试降级是否真的把失败救回来了"，而非空转。

## 3. 关键文件清单

- `modes/coding.py` — 失败处置 + 降级阶梯规则（P0-1/P0-3）
- `agents/subagent/parallel.py` / 新增 `retry.py` — 确定性重试包装（P0-2）
- `agents/subagent/failure.py` — 已生效；如需可补 `infeasible` 的识别
- `runtime/failure_reasons.py` — 降级阶梯常量（可选）
- `agents/subagent/tools.py` / `tools/handlers.py` / `tools/schema.py` — code_outline + 侦察兵契约（P1-1，引用既有计划）
- `runtime/context_history.py` — 保护已读（P1-2，引用既有计划）
- `runtime/trace/summary.py` — 重试降级指标（P1-3）

## 4. 验证方案

1. **单元**：
   - 重试包装：INTERNAL_ERROR/TIMEOUT 自动重试 1 次；STEP_LIMIT/TOOL_ERROR 不自动重试（新增 `tests/test_subagent_retry.py`）。
   - 降级预算：连续失败到顶后停止、如实上报。
   - `pytest tests/ -q` 全绿。
2. **回归复跑** `akashic-agent`：
   - 主 agent 对失败子任务发生**按 hint 重试**（trace 里能看到更窄的二次子任务），而非立即自己重读。
   - `subagent_recovered_count > 0`（至少救回部分线索）。
   - 子 agent 单个 tool_count 从 65 大幅下降（做窄生效）。
   - `stop_reason` 正常收敛；四条线索都有结论或明确"未完成+原因"。
3. **指标对比**：第三次 393 调用 / 8 子 agent 全失败 / 0 产出 → 目标 <150 调用、子 agent 失败率显著下降、有完整答案。

## 5. 取舍与边界

- **分工原则**：机械故障（瞬时错误/超时）自动重试归包装；需要改任务内容的降级归主 agent 语义决策。不让包装去"猜怎么改窄"。
- **重试必须改变条件**：禁止原样重发（否则把死循环搬个家）。`TOOL_ERROR`/`STEP_LIMIT` 的重试必须伴随更窄范围或换工具。
- **降级单向 + 有预算**：窄 subagent → 更窄 subagent → teammate → 主 agent 自做/上报，到顶即停。
- **P0（处置）能立刻见效，P1（做窄/保护已读）是治本**：但 P1 不做，子 agent 失败率不降，P0 只是把"失败得明白"做好，任务仍可能因反复重试耗尽预算。两者都要，P0 先行。
- 不动模型、不动路由本体。

## 6. 执行顺序

P0-1（主 agent 处置规则）→ P0-2（确定性重试包装）→ P0-3（降级阶梯）→ P1-2（保护已读，止主 agent 重复）→ P1-1（code_outline + 侦察兵契约，降子 agent 失败率）→ P1-3（指标）→ 回归复跑。
