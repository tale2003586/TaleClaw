# Agent 完美主义死循环修复方案（收尾约束 + bash 保护）

> 状态：计划（未动工）
> 触发证据：`.runs/run_20260619-125644-d22c85`（第七次 run：24 步顶格停机，但机制层几乎完美）
> 关系：承接前序所有编排/截断修复。机制已基本healthy（dup 0.02、fanout 1），本文修一个**全新类型**的失败：完美主义死循环。

## 1. Context：这次不是机制坏，是 agent 不肯收尾

第七次 run 被 `reasoning_step_limit` 停在 24 步，但所有机制指标都是历史最好：

| 指标 | 本次 | 评价 |
|---|---|---|
| duplicate_tool_call_ratio | 0.02 | 几乎无重复（机制健康）|
| subagent_fanout_count | 1 | 只并行一次（不横跳）|
| subagent_incomplete_count | 1 | 子任务基本成功 |
| **truncated_tool_output_count** | **122** | 异常高 |

trace 显示：主 agent 在 **step 12 就声明"核心 reasoning + 工具执行链路已经补齐"**，任务主体已完成。但 step 16-24（9 步、40+ 次 bash）全在做一件事——**反复用 `nl -ba`/`rg` 补"精确可追溯行号"**：

```
step16 "补几处带行号的证据"
step17 "用带行号的定向读取补齐引用点，确保不是凭印象"
step20 "补最终引用所需的少量行号"
step21 "再取一次明确文件列表和入口行号，然后直接总结"
step23 "现在给出总结"  ← 但没给
step24 "为了让引用更准确，我再补几段 nl -ba 的行号输出" → 停机
```

它说了至少 4 次"然后总结"却从不收尾，陷入**"再精确一点点"的完美主义循环**。

## 2. 根因

| # | 根因 | 证据 |
|---|---|---|
| **P1** | **缺"足够好就收尾"约束**。coding prompt 写满了编排/失败/降级规则，唯独没有"信息已足够时停止取证、立即产出"的边界。"带精确行号的可追溯总结"对 446 文件仓库是无底洞 | `modes/coding.py` 全文无 finish/budget-aware/stop-gathering 约束 |
| **P2** | **bash 输出不在 `preserve_tools`**，被压缩丢弃。step17-24 用 bash 取行号 → 输出被压 → "怕引用压缩掉的输出"（step20 原话）→ 重取 | `runtime/context_budget.py:21-29` 名单无 bash；`truncated_count: 122` |
| **P3（放大）** | gpt-5.5 越"认真"越钻精确性牛角尖。模型特性放大了 P1 的空白 | `models: ["gpt-5.5"]`；step 自述全是"确保不是凭印象/更准确" |

## 3. 改进方案

> 核心：给 agent 装一个"够好就收手"的刹车。前几次修的是"凑不齐信息"，这次反过来——信息凑齐了，但没有停止取证的边界。

### P0-1 coding prompt 增加收尾约束（修 P1，最高优先）
- 落点：`modes/coding.py` `CODING_PROFILE.system_prompt`。
- 新增一节 "Finishing / stop-gathering"：
  ```
  - 一旦核心问题已能回答（架构梳理：四条线索主链路已定位），立即产出结论，不要为完善细节继续取证。
  - 行号/引用是 nice-to-have，不是阻塞项。缺失的行号标注"约在 X 附近"或省略，禁止为补精确行号反复调用 nl/rg/read_file。
  - 自检触发收尾：如果你已声明"已补齐/已完成主链路"，或已用掉约 60% 步数预算，下一步必须是产出最终答案，而非继续取证。
  - 取证投入与价值匹配：把预算花在"搞清楚是什么"上，而非"让每个引用都完美"。
  ```
- 验收：构造一个"信息已足够但可无限精化"的任务，agent 在声明完成后立即产出，不再追加取证轮。

### P0-2 bash 输出纳入 preserve_tools（修 P2）
- 落点：`runtime/context_budget.py:21-29`（默认）+ :129（active_turn 的 `_env_list` 默认）+ `.env.example`。
- 加入 `"bash"`、`"rg"`、`"git_log"`（已在）等取证类工具：
  ```python
  preserve_tools = ("read_file","list_files","git_diff","git_status","git_log",
                    "code_outline","repo_map","bash","rg")
  ```
- 注意权衡：bash 输出可能很大，全保护会占预算。建议**只保护最近 N 次**（keep_recent 已有机制），而非全部；或对 bash 输出在压缩占位符里保留命令+前若干行，让 agent 知道"取过、值是什么"避免重取。
- 验收：bash 取证输出在后续步骤仍可见（或占位符含命令与摘要），不触发重取。

### P0-3 软收尾信号：步数预算感知（修 P1 的可靠性）
- 落点：`runtime/reasoning_loop.py`。
- 当 `reasoning_steps` 达到 `max_reasoning_steps` 的某比例（如 70%）时，向上下文注入一条系统提示："已用 N/M 步，请在剩余步数内产出最终答案，停止补充性取证。"
- 这是 prompt 约束（P0-1）的代码层兜底——不靠模型自觉记得预算，而是到点主动提醒。
- 验收：长 run 在 70% 预算处收到收尾提示，trace 可见。

### P1-1 区分"必要证据"与"锦上添花"的产出契约
- 落点：`.agent/coding.md` Reporting 节 + coding prompt。
- 明确：架构梳理类任务的**交付标准是"模块关系 + 关键入口 + 验证命令"**，行号是可选增强。让"完成"有明确定义，避免 agent 自我加码到"每个引用都带精确行号"。
- 验收：产出符合"够用即完成"标准，不因缺行号判定为未完成。

### P1-2 完美主义循环的可观测性
- 落点：`runtime/trace/summary.py`。
- 新增 `post_completion_tool_calls`（agent 声明完成类语句后仍发生的工具调用数）、`evidence_gathering_steps`。用于量化"是否还在收尾后空转"。
- 这是和前几次不同的失败信号，值得单独监控。

## 4. 关键文件清单

- `modes/coding.py` — 收尾约束 + 产出契约（P0-1/P1-1）
- `runtime/context_budget.py` — preserve_tools 加 bash/rg（P0-2）
- `runtime/context_history.py` — bash 压缩占位符保留命令+摘要（P0-2 兜底）
- `runtime/reasoning_loop.py` — 步数预算感知收尾提示（P0-3）
- `.agent/coding.md` — Reporting 产出标准（P1-1）
- `runtime/trace/summary.py` — 完美主义指标（P1-2）
- `config.py` / `.env.example` — 收尾比例阈值、preserve 名单开关

## 5. 验证方案

1. **单元**：
   - preserve_tools 含 bash/rg；bash 压缩占位符含命令摘要（扩展 `tests/test_context_*`）。
   - 步数预算感知：达 70% 注入收尾提示（扩展 `tests/test_reasoning_loop*`）。
   - `pytest tests/ -q` 全绿。
2. **回归复跑** `akashic-agent`：
   - `stop_reason` 正常收敛（非 reasoning_step_limit）。
   - reasoning_steps 显著低于 24；agent 在主链路补齐后即产出，无 9 步补行号尾巴。
   - `truncated_tool_output_count` 下降；`post_completion_tool_calls` ≈ 0。
   - 产出仍含模块关系 + 入口 + 验证命令（质量不降）。
3. **指标对比**：本次 24 步顶格 / 127 万 token / 收尾失败 → 目标 <15 步、能产出、无完美主义尾巴。

## 6. 取舍与边界

- **这是和前六次不同性质的问题**：前面修"凑不齐信息"，这次修"凑齐了不收手"。机制修复已成功（dup 0.02、fanout 1），不要再往机制层加东西，重点是收尾边界。
- **收尾约束别矫枉过正**：不能让 agent 在信息不足时也草草收尾。P0-1 的触发条件是"核心问题已能回答 / 60% 预算"，不是"尽早结束"。质量底线（模块关系+入口+验证命令）要守住。
- bash 全保护有预算代价，用 keep_recent 限制最近 N 次即可。
- P3（模型特性）不单独修——gpt-5.5 的"认真"是优点，用 P0-1/P0-3 的边界约束它，而非换模型。
- 不动模型、不动路由本体。

## 7. 执行顺序

P0-1（收尾约束，直接针对主因）→ P0-2（bash 保护，断掉重取燃料）→ P0-3（预算感知兜底）→ P1-1（产出契约）→ P1-2（指标）→ 回归复跑。
