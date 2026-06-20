# Agent 死循环修复工作计划

> 状态：已完成（P0-P2 已按计划落地）
> 触发证据：`.runs/run_20260618-123507-a8b1c0`
> 关联文件：实现已落地到工具 IO、loop guard、subagent、context budget、trace metrics 与对应测试。

## 1. 背景与问题

`.runs/run_20260618-123507-a8b1c0` 这次 run 暴露了一个真实缺陷：一个**只读架构梳理**任务（难度不高）跑了 24 步推理、98 次工具调用后被 `reasoning_step_limit` 强制停机，**没产出任何答案**（`final_answer` 为循环保护提示）。

trace 量化归因（已确认）：

- 98 次工具调用中 **56 次 `read_file` + 28 次 `list_files`**，且参数高度重复——`tests/` 目录被 list 了 6 次，核心文件被读 4 次以上。
- 模型自述贯穿全程的信号："files are being partially read"、"results were truncated"、"subagents all hit tool-step limits and returned truncated summaries"。
- 叠加一个 cwd 错乱 bug（step 10 bash `cd /home/tale/kaggle/mytry`，跑到了错误仓库，step 11 才自行纠正）。

根因不是模型能力，而是**多个机制缺陷叠加**，让 agent 永远凑不齐完整仓库视图、不断重读、把步数预算烧光。

## 2. 根因清单

| # | 根因 | 代码证据 |
|---|---|---|
| R1 | `read_file` 只能从头读 `limit` 行，**无 offset / 分页**，大文件被 `[:50000]` 静默硬截断，agent 拿不到后半段也无法翻页，只能反复重读 | `tools/handlers.py:77` `run_read`；`tools/schema.py:51` schema 只有 `limit` |
| R2 | 子 agent 步数上限 `min(base,12)`，超限时 `pipeline.run` 把"最后一句助手文本"当 `summary` 返回且 `success=True`——**残缺结果伪装成成功** | `agents/subagent/runner.py:79-84`；`runtime/pipeline.py:93` `return get_last_assistant_text` |
| R3 | loop guard 只按 `(tool, arguments)` 指纹判重，agent 换 offset/path 即绕过；**不检测"返回了相同结果""无信息增益"** | `tools/hooks.py:144` `_fingerprint` |
| R4（放大器）| 子 agent / bash 的 cwd 不强制锁定 workspace_root，agent 在错误目录猜路径、读空、再猜 | run trace step 10-11；`agents/subagent/runner.py` metadata 透传 workspace_root 但 bash 未强制 cd |
| R5（隐藏推手）| context budget 把旧 tool 结果压成 `<... result compressed>` 占位符，**已成功读到的内容也被丢弃**，逼 agent 重读 | `runtime/context_history.py` `_compress_old_tool_results` |

预期结果：同类只读梳理任务能在步数预算内收敛出答案；工具"信息不完整"状态对 agent 显式可见；重复无增益调用被提前拦截并引导。

## 3. 改进方案（P0 → P2）

### P0 — 修根因（最小闭环）

**P0-1 `read_file` 增加 `offset` + 显式截断元信息（R1，最高优先）**
- `tools/schema.py`：read_file 新增 `offset`（起始行，默认 0），保留 `limit`。
- `tools/handlers.py` `run_read`：按 `offset:offset+limit` 切片；存在后续内容时，结尾追加**可执行续读提示**而非静默截断，例如：
  `[read_file] showed lines 0-200 of 540. To continue: read_file(path, offset=200, limit=200). 340 lines remain.`
  字符硬上限触发时同样换成"告知剩余量 + 给出续读参数"，禁止静默 `[:50000]`。
- 同步改 `_read_text_file`（`tools/handlers.py:363`，storage/sandbox）保持一致语义。
- 验收：读 >200 行文件，返回含剩余行数与 `offset=` 续读指令。

**P0-2 子 agent 超限返回"部分结果 + 未完成标记"（R2）**
- `agents/subagent/runner.py` `SubagentResult`：新增 `truncated: bool`、`stop_reason: str | None`。
- stop_reason 回传通道：优先在 `runtime/reasoning_loop.py` 把停止原因写入 `session.metadata`，subagent 读取——**不改 `Pipeline.run` 签名**，避免波及全局调用点。
- `run()`：若 stop_reason 为 `reasoning_step_limit`，置 `success=False`、`truncated=True`、`summary` 前缀 `"[INCOMPLETE: hit step limit] "`。
- `agents/subagent/parallel.py`：聚合 `to_dict` 透传 `truncated`/`stop_reason`。
- 验收：构造必然超限的子任务，父侧拿到 `success=False` 且 summary 带 INCOMPLETE 前缀。

**P0-3 loop guard 检测"无信息增益的重复"（R3）**
- `tools/hooks.py` `ToolLoopGuardHook`：在 `(tool,args)` 指纹外，新增**结果指纹**——对 read/list 类工具记录 `hash(output)`；同一 session 内同一 `output_hash` 出现 ≥3 次即 deny，提示改为引导："你已多次得到相同结果，请改用 offset 续读、或汇总现有信息给出结论"。
- 需 `after` 段采集结果 hash；先确认 `tools/executor.py` hook 协议是否支持 after。
- **降级备选**（若 executor 改动面过大）：改在 `runtime/reasoning_loop.py` 主 agent 侧检测连续相同 output_hash，不碰 executor。
- 验收：对同一文件相同结果重复 `read_file` 3 次后被拦截并给出 offset 引导。

### P1 — 防复发 + 体验

**P1-1 同一 run 内 read/list 结果缓存 + "已读过"提示（R1/R3 协同）**
- session 级维护 `{(tool, normalized_args): (output_hash, step)}`；命中返回缓存并标注 `(already read at step K; unchanged)`，省真实 IO 与上下文重复。失效条件：workspace 发生 write/edit。

**P1-2 `list_files` 分页（R1 同类问题）**
- `tools/handlers.py` `run_list_files`：500 上限 → 支持 `offset`，`truncated:true` 时附 `next_offset` 与"还有 N 项"。

**P1-3 子 agent 步数预算独立 + 可配（R2）**
- `SUBAGENT_MAX_REASONING_STEPS` 默认 12 偏紧；提到 16，并允许 `parallel_tasks` 调用方按任务复杂度传 per-task 上限。收口于 `runtime/bootstrap.py:250` 与 `agents/subagent/runner.py:44`。

**P1-4 context budget 保护"近期已读文件内容"（R5）**
- `runtime/context_history.py` `_compress_old_tool_results`：把 `read_file`/`list_files` 的**最近一次**结果纳入 `preserve_tools` 保护；或对被压缩的读结果在占位符里保留 `(path, lines, offset)` 元信息，让 agent 知道"读过且如何重取"。

### P2 — 可观测性 + 健壮性

**P2-1 cwd 强制锁定（R4）**
- 子 agent / `bash` 执行强制以 `workspace_root` 为 cwd，禁止 agent 自行 `cd` 到无关路径；系统提示说明"所有相对路径基于 workspace_root"。先确认 `tools/handlers.py` bash handler 的 cwd 来源。

**P2-2 重复率 / 截断率进 metrics（量化回归）**
- run 结束写 `metrics.json` 时新增 `duplicate_tool_call_ratio`、`truncated_tool_output_count`、`subagent_incomplete_count`。作为确定性 agent 行为指标。

**P2-3 任务复杂度自适应步数预算（R2 治本）**
- 对显式声明"多条独立线索"的任务，路由层上调 `max_reasoning_steps`，或在 system prompt 引导"先一次性 `parallel_tasks` 分发，再汇总"。

## 4. 关键文件清单

- `tools/handlers.py` — read_file / list_files / bash / _read_text_file
- `tools/schema.py` — read_file / list_files 参数定义
- `tools/hooks.py` — ToolLoopGuardHook 指纹与拦截
- `tools/executor.py` — hook 协议，确认是否支持 after
- `agents/subagent/runner.py` — SubagentResult / run / 步数预算
- `agents/subagent/parallel.py` — 结果聚合透传
- `runtime/pipeline.py` — stop_reason 回传通道（不改签名）
- `runtime/context_history.py` — _compress_old_tool_results 保护读结果
- `runtime/reasoning_loop.py` — stop_reason 写入 session.metadata
- `runtime/bootstrap.py` — loop guard / 子 agent 步数配置
- `config.py` — 新增可配项

## 5. 验证方案（端到端）

1. **单元/集成测试**（扩展现有 `tests/test_pipeline_tool_loop_guard.py`）：
   - `read_file(offset=...)` 续读语义 + 截断提示文案。
   - 子 agent 超限 → `success=False, truncated=True`。
   - loop guard 对"相同结果重复"拦截。
   - 全量回归：`pytest tests/ -q`。
2. **回归复跑原始 case**：用原始 user 请求（akashic-agent 只读梳理）重跑，断言：
   - `stop_reason != reasoning_step_limit`（能收敛）。
   - `metrics.json` 中 `duplicate_tool_call_ratio` 显著下降。
   - `final_answer` 非空且四条线索都有结论。
3. **指标对比目标**：修复前 98 次调用 / 0 产出 → 修复后 <40 次调用且有完整答案。

## 6. 取舍与边界

- 不动模型、不动路由本体；只改工具 IO 语义、子 agent 失败信号、loop guard、上下文保护。改动可逆、按 P 级独立可回滚。
- `Pipeline.run` 优先用 `session.metadata` 回传 stop_reason，不改其签名。
- P0 三项是最小闭环，足以让原始 case 不再死循环；P1/P2 为防复发与可观测性，可分 PR 渐进合入。
- 风险点：loop guard 加 `after` hook 需确认 executor 协议；改动面过大时走 P0-3 降级备选。

## 7. 执行顺序建议

P0-1 → P0-2 → P0-3（最小闭环，合一个 PR 并回归原始 case）→ P1-3 / P1-4 → P1-1 / P1-2 → P2-1 → P2-2 → P2-3。

## 8. 实施记录

- 已完成 `read_file(offset, limit)`、`list_files(offset)` 与 storage/sandbox read 的续读提示。
- 已完成子 agent `truncated/stop_reason` 透传，`reasoning_step_limit` 不再作为成功结果返回。
- 已完成 read/list 结果 hash 级 loop guard，无信息增益重复结果第 3 次拦截。
- 已完成同一 session 内 read/list 缓存，`write_file/edit_file` 后失效。
- 已完成 coding workspace 提示、子 agent 默认预算 16、复杂多线索任务预算上调与 trace metrics 新指标。
- 验收：`python -m pytest -q tests/` 通过（271 passed，2 warnings）。
