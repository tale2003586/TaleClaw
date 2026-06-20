# 2026-06-12 Agent 测试与框架收束计划

## 目标

明天的核心目标不是单纯证明 agent 能生成 patch，而是验证当前系统作为一个 agent 架构是否站得住。测试需要回答三个问题：

1. 系统能不能稳定完成至少 12 个 coding / SWE-bench 风格任务，其中包含 3 个左右复杂任务。
2. 主 agent / 子 agent 的任务分发协议，是否真的让复杂任务更清晰、更可靠、更容易复盘。
3. trace、workspace、失败分类、最终报告这些基础设施，是否足够支撑后续持续迭代。

最终希望得到的是一套更收束的 benchmark 闭环：任务定义清楚，运行产物完整，trace 可读，失败能分类，workspace 边界明确，多 agent 的收益可以被指标证明。

## 明天要覆盖的范围

明天重点做四件事：

- 跑至少 12 个任务，覆盖简单、中等、复杂、SWE-bench 真实任务。
- 修补主 agent / 子 agent 之间的协议通信，让任务分发和结果回传有结构。
- 改进 trace 可读性，让失败分析不再依赖手动读很长的 `trace.jsonl`。
- 明确 workspace，让模型、工具、trace、报告都知道当前任务到底应该在哪个目录里执行。

这份计划不是按时间排的日程表，而是按“明天要完成什么、怎么判断完成”来写。

## 任务组合

明天至少跑 12 个任务。

建议组合：

- 5 个简单任务：验证基础 coding 稳定性。
- 3 个中等任务：验证跨文件定位、diff 控制、工具使用。
- 1 个 SWE-bench Lite 真实任务：验证真实 benchmark 闭环。
- 3 个复杂多 agent 任务：专门体现主 agent 发布任务、子 agent 领取任务、主 agent 汇总决策的流程。

复杂任务不要只是“代码更多”，而是要真的需要分工。如果单 agent 很容易一路做完，就体现不出多 agent 架构。

## 简单任务

简单任务用于检查基础稳定性。

可选任务风格：

- 边界条件 bugfix，例如 clamp、range、空输入。
- parser edge case，例如嵌套括号、空 token、非法 token。
- 新增小工具函数，并补充或通过已有测试。
- invalid edit recovery，故意让第一次编辑失败，观察恢复能力。
- workspace scope safety，检查 agent 是否会跑到 benchmark 仓库根目录而不是任务 workspace。

验收标准：

- 测试通过。
- 只修改预期文件。
- shell 命令没有逃逸出任务 workspace。
- final answer 说明修改了什么、怎么验证的。

## 中等任务

中等任务用于检查更接近真实开发的行为。

可选任务风格：

- 跨文件行为修复：测试文件提示行为，实现文件在另一个模块。
- git diff discipline：要求只允许实现修复，不允许改测试或元数据。
- context / memory 使用：需要记住前面发现的文件或约束，不要反复全仓库搜索。

验收标准：

- agent 在编辑前能定位目标文件。
- 最终检查 diff。
- trace 中能看出有限的 inspect -> edit -> verify 流程。
- 避免重复执行完全相同的失败命令；如果触发失败，能换策略恢复。

## 复杂多 Agent 任务

复杂任务用于展示你的架构优势。

### 复杂任务 A：多 agent 定位与修复

任务设置：

- 主 agent 收到一个有多个潜在 bug 位置的项目。
- 子 agent 1 负责读失败测试和错误日志。
- 子 agent 2 负责定位相关实现文件。
- 子 agent 3 负责提出最小修复方案和风险。
- 主 agent 汇总三方证据，决定最终 patch，并执行修改。

重点观察：

- 子 agent 是否分工明确。
- 主 agent 是否使用子 agent 的证据，而不是重新自己乱搜。
- 最终 patch 是否仍然小而准。

### 复杂任务 B：多 agent 代码审查与修复

任务设置：

- 给一个已有但有缺陷的 patch，或者给一个容易引入隐藏回归的任务。
- 子 agent 1 负责 correctness review。
- 子 agent 2 负责测试影响和回归风险。
- 子 agent 3 负责 diff 范围和 workspace hygiene。
- 主 agent 处理冲突意见，产出最终修复。

重点观察：

- 子 agent 是否能发现不同类型的问题。
- 主 agent 是否显式记录采纳/拒绝了哪些建议。
- 最终修改是否避免过度修复。

### 复杂任务 C：SWE-bench 风格真实任务

任务设置：

- 主 agent 读取 problem statement。
- 拆分出代码搜索、根因分析、修复设计、验证策略几个子任务。
- 子 agent 返回带文件路径和证据的发现。
- 主 agent 生成最终 patch / prediction。
- 官方 harness 结果记录为 resolved / unresolved / infra error。

重点观察：

- trace 是否能看出完整分工。
- 失败时能不能区分 patch 错误和 Docker / 依赖 / 网络等 infra 错误。
- 是否能生成官方 `predictions.jsonl` 并完成后续评测。

复杂任务统一验收标准：

- trace 中能看到任务分发、子 agent 输出、主 agent 汇总。
- 子 agent 尽量产生不重叠的证据。
- 主 agent 明确说明最终决策依据。
- 最终 patch 保持最小化。
- 失败报告能区分 patch failure、infra failure、tool/runtime failure、protocol failure。

## 主 Agent / 子 Agent 协议

明天需要把主子 agent 之间的通信协议先定一个最小可用版本。目标不是复杂，而是让任务可控、结果可聚合、trace 可读。

## 子任务分发结构

每个子 agent assignment 至少包含：

```json
{
  "assignment_id": "short-stable-id",
  "role": "diagnosis | search | review | verification | risk",
  "objective": "一个明确的子任务",
  "workspace_root": "当前任务 workspace 的规范路径",
  "allowed_paths": ["允许读取或关注的路径前缀"],
  "forbidden_paths": ["禁止修改或读取的路径前缀"],
  "inputs": {
    "task_request": "任务摘要",
    "known_files": [],
    "known_failures": []
  },
  "expected_output": {
    "summary": "required",
    "evidence": "required",
    "recommendation": "optional",
    "confidence": "required"
  },
  "stop_condition": "做到什么程度就停止"
}
```

## 子任务结果结构

每个子 agent 回传结果至少包含：

```json
{
  "assignment_id": "same id",
  "status": "completed | blocked | inconclusive",
  "summary": "一句话结论",
  "evidence": [
    {
      "file": "relative/path.py",
      "line": 12,
      "claim": "这条证据支持什么判断"
    }
  ],
  "recommendation": "建议主 agent 下一步怎么做",
  "risks": ["风险或不确定点"],
  "confidence": "low | medium | high"
}
```

## 主 Agent 职责

主 agent 需要做到：

- 发布边界清楚的子任务。
- 不要让所有子 agent 都去扫全仓库。
- 把子 agent 发现合并成 decision table。
- 显式处理冲突意见。
- 最终 patch 由主 agent 汇总后统一执行，避免多个子 agent 乱改。
- 记录为什么采纳或拒绝每个子 agent 的建议。

## 协议指标

每个复杂任务记录：

- 子任务数量。
- 子任务完成数量。
- 子 agent 输出中有多少被主 agent 使用。
- 重复劳动比例：多个子 agent 是否报告了同一个文件、同一个结论。
- 冲突处理质量：主 agent 是否解释了冲突意见。
- patch 最小化程度：实际修改文件数和预期修改文件数是否一致。

## Trace 可读性改进

现在 raw `trace.jsonl` 信息太密，适合机器记录，不适合日常复盘。明天需要做一个紧凑摘要产物。

建议每个 run 生成：

```text
trace_summary.md
```

建议包含：

- Run metadata：task id、run id、model、workspace root、最终状态。
- Outcome：pass、fail、infra error、loop guard、empty response、timeout。
- Workspace：requested workspace、resolved workspace、allowed root。
- Tool timeline：只保留关键工具调用，按 reasoning step 分组。
- File access summary：读了哪些文件、改了哪些文件、哪些路径被拒绝。
- Agent delegation summary：主 agent 分发了什么任务，子 agent 返回了什么，主 agent 如何决策。
- Verification summary：跑了哪些测试，命令状态，最终 diff 状态。
- Failure diagnosis：主失败原因和证据。

同时建议生成机器可读版本：

```text
trace_summary.json
```

最小字段：

```json
{
  "run_id": "",
  "task_id": "",
  "status": "",
  "failure_category": "none | patch_wrong | infra_error | tool_error | protocol_error | model_empty | loop_guard | timeout",
  "workspace_root": "",
  "tools": {
    "total_calls": 0,
    "denied_calls": 0,
    "repeated_calls": 0
  },
  "files": {
    "read": [],
    "modified": [],
    "unexpected_modified": []
  },
  "verification": {
    "commands": [],
    "passed": false
  },
  "multi_agent": {
    "assignments": 0,
    "completed_assignments": 0,
    "used_findings": 0
  }
}
```

trace 摘要验收标准：

- 一个失败 run 可以在 3 分钟内从 `trace_summary.md` 判断主要原因。
- 摘要能区分模型错误、工具/runtime 错误、verifier 错误、Docker/SWE-bench infra 错误、任务设计错误。
- 摘要能链接或指向 raw trace、最终 diff、关键日志。

## Workspace 明确化

workspace 必须在每一层都清楚：模型上下文、工具执行、trace、报告。

## 模型上下文

任务 prompt 或 runtime context 应明确写入：

- 当前 workspace root。
- allowed root。
- 路径是否应相对 workspace。
- 推荐的测试命令形式。

建议文案：

```text
Workspace root: /tmp/.../task_workspace
所有文件路径和 shell 命令都必须在该 workspace 内执行。
除非 workspace resolver 明确提供绝对路径，否则使用相对路径。
不要 cd 到 benchmark 仓库根目录。
```

## 工具约束

继续保留并完善：

- `list_files`、`read_file`、`edit_file` 不能逃逸 workspace。
- shell 命令如果 `cd` 到 workspace 外的绝对路径，直接拒绝。
- 被拒绝的调用必须进入 trace summary。

## 报告字段

每个 run report 应包含：

```text
workspace_requested
workspace_resolved
workspace_allowed_root
workspace_escape_attempts
commands_with_external_cd
```

workspace 验收标准：

- 没有任何成功执行的 shell 命令跑到任务 workspace 外。
- 外部 `cd` 尝试会被拒绝，并分类为 workspace violation。
- 模型遇到 workspace denial 后能改用 `.` 或 resolved workspace path 恢复。

## 消融实验设计

明天的测试中要加入一些消融设计，用来说明你的架构为什么有价值。

## 消融 A：单 agent vs 多 agent

同一个复杂任务跑两种模式：

- 单 agent：一个 agent 从头做到尾。
- 多 agent：主 agent 分发 diagnosis / search / review / verification 子任务。

比较指标：

- pass rate。
- reasoning steps。
- tool calls。
- 重复读取文件次数。
- patch size。
- 找到正确目标文件所需步数。
- 失败分类。

预期体现：

- 多 agent 在复杂任务上应当降低诊断模糊度，提升证据质量，减少漏看风险。

## 消融 B：结构化协议 vs 自由文本分发

同一个复杂任务跑两种子任务分发方式：

- 结构化 assignment/result schema。
- 自由文本口头分发。

比较指标：

- 子 agent 输出是否可直接聚合。
- 重复劳动比例。
- 主 agent 汇总质量。
- trace 可读性评分。

预期体现：

- 结构化协议应该减少含糊输出，让 trace summary 更容易生成。

## 消融 C：trace summary vs raw trace only

选几个成功和失败 run，比较两种复盘方式：

- 只看 raw `trace.jsonl`。
- 先看 `trace_summary.md`，必要时再回 raw trace。

比较指标：

- 定位失败原因所需时间。
- 失败分类准确率。
- 是否能快速知道改了什么、为什么改、怎么验证。

预期体现：

- trace summary 应显著降低复盘成本，避免把 infra error 误判成 patch failed。

## 消融 D：workspace guard 开启 vs 关闭

选择容易让模型跑错目录的任务，比较：

- workspace guard 开启。
- workspace guard 关闭或仅记录不拦截。

比较指标：

- 错误测试命令数量。
- workspace escape 次数。
- 因跑错测试目录导致的误失败数量。

预期体现：

- workspace guard 应减少错误验证和误导性失败。

## 每个任务都要收集的核心指标

```text
task_id
run_id
mode: single-agent | multi-agent
task_type: simple | medium | complex | swebench
final_status: pass | fail | infra_error
failure_category
reasoning_steps
tool_calls
denied_tool_calls
empty_model_responses
loop_guard_events
workspace_escape_attempts
files_read_count
files_modified_count
unexpected_files_modified_count
tests_run_count
verification_passed
patch_bytes
duration_seconds
```

多 agent 任务额外收集：

```text
assignments_created
assignments_completed
assignments_blocked
subagent_findings_total
subagent_findings_used
duplicate_findings
conflicting_findings
conflicts_resolved
main_agent_decision_recorded
```

trace 可读性额外收集：

```text
trace_summary_exists
trace_summary_has_workspace
trace_summary_has_failure_category
trace_summary_has_file_summary
trace_summary_has_tool_timeline
diagnosis_time_minutes
```

## 成功目标

最低目标：

- 至少跑 12 个任务。
- 至少 9 个任务不是 infra error，能完整完成评测。
- 至少 3 个复杂任务使用多 agent 模式。
- 至少完成 2 组消融对比。
- 100% run 生成 `trace_summary.md` 或等价摘要。
- 100% run 记录 workspace root 和 allowed root。
- 0 次成功写入 workspace 外。

更理想目标：

- 12 个任务中至少 10 个通过，或者失败被正确分类。
- 复杂多 agent 任务相比单 agent，诊断更清楚、证据更完整。
- 失败 run 能在 3 分钟内完成主要原因定位。
- workspace 错误会被拦截并恢复，而不是污染测试结果。

## 明天优先考虑的框架改动

优先做小而关键的改动，让每次 run 更可信。

1. 每个 run 后生成 `trace_summary.md` 和 `trace_summary.json`。
2. 模型上下文和报告中显式写入 workspace 信息。
3. 增加主 agent / 子 agent assignment 和 result schema。
4. 增加主 agent synthesis record，记录采纳了哪些子 agent 发现。
5. 给 run report 增加 failure category。
6. 提取消融实验需要的 metrics。
7. 生成每个任务一行的 eval dashboard 数据。

建议失败分类：

```text
pass
patch_wrong
test_failed
infra_error
docker_error
dependency_error
workspace_violation
tool_denied
loop_guard
empty_model_response
timeout
task_design_error
verifier_error
protocol_error
```

## 明天交付物

明天结束时希望留下：

- 一组 12 个任务的运行结果。
- 至少 3 条复杂多 agent 任务 trace。
- 至少 2 组消融实验结果。
- 第一版 trace summary 生成逻辑或产物。
- 一份主 agent / 子 agent 协议说明。
- 一处 workspace 明确化的 prompt/runtime/report 改动。
- 一份最终复盘报告：每个任务的结果、失败分类、下一步框架修复点。

## 最终复盘问题

明天结束时，用这些问题判断是否达到目标：

- 我能不能从摘要里看出每个任务为什么通过或失败？
- 我能不能区分 patch 错误和 Docker / 依赖 / 网络错误？
- 我能不能清楚看到 workspace 是哪里，以及有没有被强制执行？
- 子 agent 的发现有没有被主 agent 真正使用？
- 多 agent 相比单 agent 是否提升了可靠性、诊断清晰度或 patch 质量？
- 剩下的问题是否都能落到具体框架改动上？
