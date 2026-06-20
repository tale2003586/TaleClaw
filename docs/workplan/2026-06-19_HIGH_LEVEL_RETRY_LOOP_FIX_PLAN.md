# Agent 高层重试死循环修复方案

> 状态：计划（未动工）
> 触发证据：`.runs/run_20260619-044737-50c28f`（第四次失败：36 步顶格 / 1832 工具调用 / 209 万 token / 11 分钟 / 零产出）
> 关系：承接 `2026-06-19_SUBAGENT_FAILURE_HANDLING_RETRY_PLAN.md`。上一轮修了"失败可处置"，但处置逻辑**不收敛**，本文修"让处置收敛"。

## 1. Context：这次问题的性质变了

前三次是"单点死循环"（工具截断 / 子 agent 超限）。第四次，**所有新机制都生效了**——code_outline 在用、降级阶梯被引用（trace 原话 "Per the degradation ladder, I'll handle these directly"）、失败分类带 retry_hint。但结果**更差**：

| 指标 | run3 (035728) | run4 (本次) |
|---|---|---|
| reasoning_steps | 8 | **36（顶格）** |
| tool_calls | 393 | **1832** |
| total_tokens | 42 万 | **209 万** |
| duplicate_tool_call_ratio | 0.27 | **0.46** |
| subagent_incomplete_count | 8 | **27** |
| subagent_fanout_count | 2 | **8** |

**新失败模式：主 agent 陷入"重试-降级"的高层死循环。** step 序列证据：

```
step4   parallel_tasks → 子agent超限
step5-9 降级自己 code_outline + list_files
step10/11/12  又 parallel_tasks ×3 → "scope was too broad"，全超限
step13  "Per the degradation ladder, I'll handle these directly" → 自己读
step20/23/26/29  反复再 parallel_tasks → 每次 "All subagents hit step limits again"
step14/30/31  "Results are getting compressed"
step36  reasoning_step_limit 停机
```

把单点死循环，升级成了**带重试逻辑的循环**——正是"把死循环搬个家"。

## 2. 根因（两个已用代码确认 + 一个行为确认）

| # | 根因 | 确凿证据 |
|---|---|---|
| **G1** | **重试/降级是纯 prompt 规则，代码层无全局预算/状态机**。主 agent 在 `subagent ⇄ 自己读` 之间横跳 6+ 轮，每轮重新"发现"子 agent 不行又重试。降级阶梯设计是单向的，但靠模型自觉执行 → 模型做不到单向 | step 4/10/11/12/20/23/26/29 反复 parallel_tasks；同一线索重试 6+ 次；无去重 |
| **G2** | **`preserve_tools` 没包含 `code_outline` / `repo_map`**。`runtime/context_budget.py:21` 与 :123 的 preserve_tools = `("read_file","list_files","git_diff","git_status","git_log")`——新工具的输出被 `_compress_old_tool_results` 压成占位符 → 几步后丢失 → 重取 | step14/30/31 三次 "compressed"；duplicate_ratio 0.46 |
| **G3** | **子任务实际仍是宽的**，P1-1"按 code_outline 点名文件切窄"没真正执行 | 主 agent step12 自述 "scope was too broad"；27 个 incomplete |

补充：**conversation_history 预算仅 10000 字符**（`context_budget.py:101`），36 步的长 run 里历史被狂压，进一步逼 agent 重读。

## 3. 改进方案

> 核心立场：**收敛不能靠模型自觉，必须用确定性状态机在代码层强制。** 上一轮把"降级阶梯"写进 prompt 是不够的——这次证明模型不会单向执行。本轮把它升级为"代码强制单向 + 硬预算 + 已读保护"。

### P0-1 子任务调度状态机：强制单向降级 + 硬预算（修 G1，最高优先）
- 落点：`coding_runtime/task.py`（task_session 层）或新增 `agents/subagent/orchestration_state.py`，由 `tools/handlers.py` 的 `_run_parallel_subagent_tasks` / `_run_subagent_task` 接入。
- 机制：在 task_session 维护每条线索（按 description/scope 归一化的 key）的尝试账本：
  ```
  {clue_key: {"parallel_attempts": n, "subagent_failures": n, "stage": "subagent|narrowed|teammate|self|terminal"}}
  ```
- **硬规则（代码强制，非 prompt）**：
  - 同一 clue_key 的 `parallel_tasks`/`task` 累计失败 ≥ 2 次 → **工具层直接拒绝该线索再派 subagent**，返回结构化提示："此线索 subagent 已失败 N 次（step_limit/scope_too_broad），按降级阶梯请改用 spawn_teammate 或主 agent 直接处理。禁止再 fan-out。"
  - 全局 fan-out 预算：单次 run 内 `subagent_fanout_count` ≤ 阈值（如 4），超出后 `parallel_tasks` 工具被禁用，返回引导。
  - stage 单向递进：`subagent → narrowed_subagent → teammate → self`，**禁止回退**（已到 self 不允许再 subagent）。
- 验收：构造连续子 agent 失败的 run，第 3 次对同线索 parallel_tasks 被工具层拒绝；fan-out 总数不超过阈值。

### P0-2 `preserve_tools` 加入 code_outline / repo_map（修 G2，一行级改动）
- 落点：`runtime/context_budget.py:21` 与 :123（`SectionBudgetRule.preserve_tools` 默认值 + active_turn 的 `_env_list` 默认值）。
- 改为：`("read_file", "list_files", "git_diff", "git_status", "git_log", "code_outline", "repo_map")`。
- 同步更新 `.env.example` 的 `CONTEXT_ACTIVE_TURN_PRESERVE_TOOLS` 默认。
- 这是 step14/30/31 "compressed" 的直接修复——确定性工具的产出必须能留在上下文，否则 outline/map 省下的 IO 又被重取吃回去。
- 验收：读 code_outline → 若干步后该结果仍在上下文（或占位符保留可重取元信息）。

### P0-3 占位符保留可重取元信息（G2 兜底）
- 落点：`runtime/context_history.py` `_compress_old_tool_results`。
- 即使某结果超出 keep_recent 被压缩，占位符不要只写 `<code_outline result compressed>`，而要写 `<code_outline(path=X) compressed — re-fetch with code_outline(path=X)>`，让 agent 知道"读过、怎么重取"，避免盲目重读全文（重读全文比重取 outline 贵得多）。
- 验收：被压缩的 read/outline 占位符含 path 与重取指令。

### P1-1 子任务做窄真正落地（修 G3）
- 落点：`modes/coding.py` system prompt + `agents/subagent/tools.py` 侦察兵契约。
- 强制序列（写进 prompt，并由 P0-1 状态机的失败提示反复强化）：
  ```
  派 subagent 前必须：1) 已有该范围的 code_outline/repo_map；
  2) 子任务 prompt 点名 ≤5 个具体文件 + 明确 deliverable；
  3) 大文件只让子 agent 取 outline 不读全文。
  禁止把 "investigate the X path / for each directory describe" 这类宽任务交给 subagent。
  ```
- 可选硬约束：subagent 的 prompt 若不含具体文件路径列表，工具层给警告或要求 scope 字段非空。
- 验收：派出的子任务 prompt 含具体文件清单；子 agent 单个 tool_count 从 65 大幅下降、incomplete 率下降。

### P1-2 conversation_history 预算与长 run 适配
- 落点：`runtime/context_budget.py:101`、`config.py`。
- 36 步长 run 下 10000 字符历史预算过紧。评估上调，或对 coding task_session 用独立更宽的预算档。
- 验收：长 run 中早期已读结论不因历史压缩而丢失到需要重读。

### P1-3 重试/降级可观测性（验证收敛）
- 落点：`runtime/trace/summary.py` metrics。
- 新增 `subagent_retry_count`、`subagent_degrade_count`、`fanout_rejected_count`（被状态机拦下的次数）、`subagent_recovered_count`。
- 这些指标用来确认 P0-1 状态机是否真的把横跳拦住了。

## 4. 关键文件清单

- `coding_runtime/task.py` / 新增 `agents/subagent/orchestration_state.py` — 调度状态机（P0-1）
- `tools/handlers.py` — `_run_parallel_subagent_tasks` 接状态机、拒绝逻辑
- `runtime/context_budget.py` — preserve_tools 加 code_outline/repo_map（P0-2）；历史预算（P1-2）
- `runtime/context_history.py` — 占位符保留重取元信息（P0-3）
- `modes/coding.py` — 做窄强制序列 + 降级阶梯（P1-1）
- `agents/subagent/tools.py` — 侦察兵契约强化
- `runtime/trace/summary.py` — 收敛指标（P1-3）
- `config.py` / `.env.example` — 预算与阈值开关

## 5. 验证方案

1. **单元**：
   - 状态机：同线索 3 次 parallel_tasks 被拒；fan-out 超阈值被禁；stage 不可回退（新增 `tests/test_orchestration_state.py`）。
   - preserve_tools 含 code_outline/repo_map；压缩占位符含重取元信息（扩展 `tests/test_context_*`）。
   - `pytest tests/ -q` 全绿。
2. **回归复跑** `akashic-agent`：
   - `subagent_fanout_count` ≤ 4（本次 8）；`fanout_rejected_count > 0`（状态机生效）。
   - 无 "compressed" 后重取 outline 的 step（P0-2 生效）。
   - 主 agent 不再在 subagent⇄自读横跳（trace 检查）。
   - reasoning_steps 远低于 36；有完整答案或四条线索明确"未完成+原因"。
3. **指标对比**：本次 1832 调用 / 209 万 token / 0 产出 → 目标 <300 调用、<50 万 token、fan-out ≤4、有答案。

## 6. 取舍与边界

- **最关键的认知**：收敛靠代码状态机，不靠 prompt。第四次 run 证明，把降级阶梯写进 prompt，模型仍会在压力下回退重试。P0-1 必须是工具层的硬拒绝。
- 状态机的 clue_key 归一化要稳健（按 description + scope 文件集做 key），避免主 agent 改几个字就绕过预算。
- P0-2 是一行级低风险高收益改动，应最先合入验证。
- 大型仓库梳理可能本质是"单轮做不完"的任务——若上述修完仍顶格，需考虑**跨轮检查点**（另立方案，不在本文范围）。
- 不动模型、不动路由本体。

## 7. 执行顺序

P0-2（preserve_tools，一行级、立即验证）→ P0-3（占位符元信息）→ P0-1（调度状态机，核心）→ P1-1（做窄落地）→ P1-2（历史预算）→ P1-3（指标）→ 回归复跑。
