# Agent 多编排能力边界与死循环修复方案（修订版）

> 状态：计划（未动工）
> 触发证据：`.runs/run_20260618-123507-a8b1c0`
> 取代：`2026-06-18_AGENT_LOOP_GUARD_TRUNCATION_FIX_PLAN.md`（保留其 R1/R3/R5，新增编排层根因 R2-extended、R6）

## 1. 背景与问题

`.runs/run_20260618-123507-a8b1c0`：一个**只读架构梳理**任务（不难）跑了 24 步推理、98 次工具调用后被 `reasoning_step_limit` 强制停机，**零产出**。

trace 量化归因：
- 98 次调用中 56 次 `read_file` + 28 次 `list_files`，参数高度重复（`tests/` list 了 6 次，核心文件读 4 次以上）。
- 主 agent 在 step 1/8/18/24 **四次** fan-out `parallel_tasks`，每次子 agent 都撞步数上限返回残缺摘要，主 agent 不信任 → 退化成自己串行重读。
- 模型自述："subagents all hit tool-step limits and returned truncated summaries"、"files are being partially read"。

**新增的核心认知**：除了"工具截断"，更深一层是**编排层缺陷**——主 agent 在派活时对子 agent 的真实能力盒子（步数、上下文、工具）是"盲"的，把"重综合任务"派给了"轻侦察兵"，必然超限。这不是模型不够聪明，是**能力边界没有被编码进系统**。

## 2. 当前两套执行体的能力差异（问题根源）

| 维度 | subagent（`TaskSubagentRunner` / `parallel_tasks`） | teammate（`TEAM.spawn`） |
|---|---|---|
| 步数预算 | 16（`SUBAGENT_MAX_REASONING_STEPS`） | 50 |
| 上下文 | 隔离、无 memory、无 security 自动注入 | 完整 |
| 工具 | `SUBTASK_TOOL_WHITELIST` 受限 | 全量 |
| 生命周期 | 一次性、返回单个 summary | 持久、可多轮 inbox 通信 |
| 适合 | 有界定位 / 提取 / 列举 | 跨文件综合、写码、多轮迭代 |
| 代码 | `agents/subagent/`、`tools/schema.py:513` LEAD_ONLY_TOOLS | `coding_runtime/teammate.py:139` `spawn` |

问题：`task` / `parallel_tasks` 的工具描述只说"focused subtask / fresh context"，**没有任何能力边界与适用/不适用提示**，也没有给主 agent "重任务请走 teammate" 的判定依据。主 agent 只能裸猜。

## 3. 根因清单

| # | 根因 | 证据 |
|---|---|---|
| R1 | `read_file` 无 offset/分页，大文件静默截断 → 重读 | `tools/handlers.py` `run_read`、`tools/schema.py` |
| R2 | 子 agent 超限时 summary 伪装成 `success=True` 残缺结果 | `agents/subagent/runner.py:79-84` |
| **R2-ext** | **子 agent 能力边界未编码进工具契约**：主 agent 无从判断任务是否超出子 agent 盒子，重任务被派给轻 agent | `tools/schema.py:513` task/parallel_tasks 描述；`agents/subagent/tools.py` 提示过于泛化 |
| R3 | loop guard 只按 `(tool,args)` 判重，换 offset/path 即绕过，不检测"相同结果/无增益" | `tools/hooks.py` `_fingerprint` |
| R4 | 子 agent / bash cwd 未强制锁定 workspace_root | trace step 10-11 |
| R5 | context budget 把旧 read 结果压成占位符，已读内容被丢弃 → 重读 | `runtime/context_history.py` `_compress_old_tool_results` |
| **R6** | **缺少"任务复杂度 → 执行体"的路由判定**：没有规则/信号把重任务导向 teammate、轻任务导向 subagent | 无对应代码；主 agent 临场发挥，判断不稳 |

## 4. 改进方案

> 设计总原则：**不要让主 agent 去精确预估"子 agent 步数够不够"（模型做不到），而是把能力边界编码进工具层，让超出盒子的任务要么走对路（teammate），要么天然落在盒子内（侦察兵式窄任务），要么超限时可被安全察觉。**

### P0 — 止血：让"派错活"不再灾难性

**P0-1 `read_file` 加 `offset` + 显式续读提示（R1）**
- `tools/schema.py`：read_file 新增 `offset`，保留 `limit`。
- `tools/handlers.py` `run_read`：按 `offset:offset+limit` 切片，有后续内容时追加 `read_file(path, offset=.., limit=..). N lines remain.`，禁止静默 `[:50000]`。storage/sandbox 的 `_read_text_file` 同步。
- 验收：读 >200 行文件返回含剩余行数与续读指令。

**P0-2 子 agent 超限返回"部分结果 + 未完成标记"（R2）**
- `SubagentResult` 加 `truncated: bool`、`stop_reason: str|None`。
- stop_reason 经 `session.metadata` 回传（`runtime/reasoning_loop.py` 写入，`agents/subagent/runner.py` 读取），**不改 `Pipeline.run` 签名**。
- 超限时 `success=False`、`truncated=True`、summary 前缀 `"[INCOMPLETE: hit step limit] "`；`parallel.py` 聚合透传。
- 验收：构造必然超限子任务，父侧拿到 `success=False` + INCOMPLETE 前缀。

**P0-3 loop guard 检测"无信息增益的重复"（R3）**
- `tools/hooks.py`：除 `(tool,args)` 指纹，对 read/list 增加**结果指纹** `hash(output)`，同 session 同 hash ≥3 次即 deny 并引导"改用 offset 续读或汇总现有信息"。
- 需 `after` 段采集 hash；确认 `tools/executor.py` 协议，否则降级到 reasoning_loop 主 agent 侧检测。
- 验收：相同结果重复 read 3 次后被拦截。

### P1 — 编排层：把能力边界编码进系统（本次修订重点）

**P1-1 把 subagent 重定位为"侦察兵（locate & extract）"而非"分析师"**
- `agents/subagent/tools.py` `SUBTASK_SYSTEM_PROMPTS`：明确写入能力契约，例如 explore：
  > "你是一次性侦察兵，约 16 步预算。只做：定位文件、提取关键片段、回报路径+行号+一句话结论。**不要试图读完并综合整个子系统**——综合由调用方负责。若信息超出预算，回报已找到的清单并标注未完成，不要硬撑。"
- 配合**结构化返回**（见 P1-2），让窄任务在预算内必然完成。

**P1-2 subagent 结构化返回 schema（窄而完成，而非宽而截断）**
- 让 explore 子 agent 返回结构化结果（`findings: [{path, lines, note}]` + `incomplete: bool`）而非长 summary。
- 主 agent 拿到可靠"地图"后自己做跨文件综合。降低单个子 agent 的认知负载，从源头避免超限。

**P1-3 `task` / `parallel_tasks` 工具描述补能力边界与选型指引（R2-ext）**
- `tools/schema.py:513`：在 task/parallel_tasks 描述里写清：
  > "子 agent：约 16 步、隔离上下文、只读/受限工具。**适合**：有界定位、列举、提取局部事实、独立分片探索。**不适合**：跨多文件深度综合、写代码并迭代、需要多轮反馈的任务——那类请用 `spawn_teammate`。"
- 让选型依据进入模型可见的工具契约，而不是埋在长 system prompt 里。

**P1-4 "复杂度 → 执行体"半显式路由规则（R6）**
- 在 coding 模式 system prompt（`modes/coding.py`）加一条明确决策规则：
  > "需要综合/写码/多轮迭代 → `spawn_teammate`（50 步、完整上下文）；只需定位/列举/提取 → `parallel_tasks`（一次性侦察）。"
- 可选增强：给一个**粗粒度信号**辅助判断（任务描述含 summarize/refactor/implement/design 等动词，或预估涉及文件数 > 阈值时，工具层返回一句软提示"此任务可能超出子 agent 预算，考虑 teammate"）。不强制，避免误杀。

### P1.5 — 防复发（原计划 P1）

**P1-5 同一 run 内 read/list 结果缓存 + "已读过"提示**
- session 级 `{(tool,args): (output_hash, step)}`，命中返回缓存并标注 `(already read at step K; unchanged)`，write/edit 后失效。

**P1-6 `list_files` 分页**
- `run_list_files`：500 上限 → 支持 `offset`，截断时附 `next_offset` 与剩余数。

**P1-7 context budget 保护近期已读内容（R5）**
- `_compress_old_tool_results`：把 read/list 最近一次结果纳入 `preserve_tools`，或在占位符里保留 `(path, lines, offset)` 让 agent 知道如何重取。

### P2 — 可观测性 + 治本

**P2-1 cwd 强制锁定（R4）**：子 agent / bash 强制以 workspace_root 为 cwd，提示"相对路径基于 workspace_root"。

**P2-2 编排健康指标进 metrics**：`metrics.json` 新增 `duplicate_tool_call_ratio`、`truncated_tool_output_count`、`subagent_incomplete_count`、`subagent_fanout_count`（fan-out 次数高 = 编排不健康的信号）。

**P2-3 任务复杂度自适应预算**：声明"多条独立线索"的任务，路由层上调 `max_reasoning_steps` 或引导一次性 fan-out 后汇总。

## 5. 关键文件清单

- `tools/handlers.py` — read_file / list_files / bash / _read_text_file
- `tools/schema.py` — read_file/list_files 参数；task/parallel_tasks 能力边界描述
- `tools/hooks.py` / `tools/executor.py` — loop guard 结果指纹 / after 协议
- `agents/subagent/tools.py` — SUBTASK_SYSTEM_PROMPTS 侦察兵契约、结构化返回
- `agents/subagent/runner.py` / `parallel.py` — SubagentResult / truncated / 透传
- `modes/coding.py` — "复杂度 → 执行体"路由规则
- `coding_runtime/teammate.py` — teammate 触发路径（已具备，无需大改）
- `runtime/reasoning_loop.py` / `pipeline.py` — stop_reason 回传
- `runtime/context_history.py` — 保护读结果
- `runtime/bootstrap.py` / `config.py` — 预算与开关配置

## 6. 验证方案

1. **单元/集成**（扩展 `tests/test_pipeline_tool_loop_guard.py`、`test_subagent_runner.py`）：
   - read_file offset 续读语义 + 提示文案；
   - 子 agent 超限 → `success=False, truncated=True`；
   - loop guard 相同结果重复拦截；
   - subagent 结构化返回解析；
   - `pytest tests/ -q` 全绿。
2. **回归复跑原始 case**（akashic-agent 只读梳理）：
   - `stop_reason != reasoning_step_limit`；
   - `subagent_fanout_count` 与 `duplicate_tool_call_ratio` 显著下降；
   - `final_answer` 非空且四条线索都有结论。
3. **指标对比目标**：修复前 98 次调用 / 0 产出 / 4 次 fan-out → 修复后 <40 次调用、有完整答案、fan-out ≤1 次或子结果可靠。

## 7. 取舍与边界

- **核心立场**：能力边界编码进工具契约 > 指望主 agent 临场判断。模型对"步数够不够"的自我估算不可靠，这是这次 run 判断错 4 次的根因。
- subagent = 侦察兵（窄、一次性、可超限察觉）；teammate = 分析师（重、持久、多轮）。两者职责分明，不让 subagent 干 teammate 的活。
- 不动模型、不动路由本体；改的是工具 IO 语义、子 agent 契约与失败信号、loop guard、上下文保护。按 P 级可独立回滚。
- 风险：loop guard `after` hook 需确认 executor 协议；P1-4 软提示阈值需调，避免误杀正常 subagent 用法。

## 8. 执行顺序

P0-1 → P0-2 → P0-3（最小止血闭环，回归原始 case）→ P1-1 / P1-2 / P1-3 / P1-4（编排层，本次修订重点）→ P1-5 / P1-6 / P1-7 → P2-1 → P2-2 → P2-3。
