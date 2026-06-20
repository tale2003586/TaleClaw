# 子任务文件名脑补与失败诊断污染修复方案

> 状态：计划（未动工）
> 触发证据：`.runs/run_20260619-055053-cb10a7`（任务整体 completed=25 步收敛，但 11 个子 agent 全失败）
> 关系：承接 `2026-06-19_HIGH_LEVEL_RETRY_LOOP_FIX_PLAN.md`。状态机与 preserve_tools 生效后任务能收敛了，本文修"子 agent 高失败率"的两个新根因。

## 1. Context：任务通了，但子 agent 全军覆没

这次 `status: completed`、25 步、`subagent_fanout_count: 2`——状态机和上下文保护生效，主 agent 没再陷入高层死循环。但 `subagent_incomplete_count: 9`，11 个子 agent 基本全失败。失败分两类：

**类型一：找不到文件（主 agent 把文件名编错了）**
对比主 agent 派给子任务的文件名 vs 仓库真实文件：

| 主 agent 写的 | 真实 | 错误 |
|---|---|---|
| `package.js` | `package.json` | 扩展名 |
| `frontend/dashboard/src/main.ts` | `main.tsx` | 扩展名 |
| `frontend/dashboard/build.js` | `build.mjs` | 扩展名 |
| `frontend/dashboard/tsconfig.js` | `tsconfig.json` | 扩展名 |
| `tests/agent/looping/test_agent_loop.py` | `tests/proactive_v2/test_agent_loop.py` | 目录 |
| `agent/looping/loop_state.py`、`logger.py` | 不存在 | 凭空捏造 |
| `agent/turns/dispatch.py`、`side_effects.py` | 不存在 | 凭空捏造 |

子 agent 失败 trace 印证：`subagent_missing_required_files: "FileNotFoundError: .../build.js"`、`"Directory not found: tests/agent"`。

**类型二：超限（大文件 read 硬啃）**
9 个失败里 5 个 `subagent_step_limit`，tool_count 46/56/73——子 agent 拿到 `store.py`(1827)/`passive_turn.py`(1821) 仍 read_file 硬读到超限，没用 code_outline。

## 2. 根因（均已用代码确认）

| # | 根因 | 证据 |
|---|---|---|
| **N1** | **主 agent 用先验脑补文件名，没看 repo_map 的真实清单**。repo_map 调过、返回真实文件名，但切任务时主 agent 写的是"前端项目通常长这样"的常规名（.ts/.js/package.js），而这仓库不常规（.tsx/.mjs/proactive_v2）| 上表；P1-1"范围封闭"是 prompt 建议、未强制 |
| **N2** | **派活前无文件存在性校验**。错误文件名被直接发给子 agent，跑 17-21 步撞 FileNotFoundError 才暴露 | `tools/handlers.py` `_run_parallel_subagent_tasks` 入口无校验 |
| **N3** | **failure_message 被代码内容污染**。`agents/subagent/failure.py:_looks_like_tool_error` 用子串匹配 `"valueerror"/"error:"/"exception"`，子 agent read_file **读到的代码行** `except ValueError as exc:` 命中 `"valueerror"` → 被误判为工具错误，`_first_line` 把代码行当 failure_message | trace: `failure_message: "except ValueError as exc:"`、`"self,"`、`"{"` |
| **N4** | **大文件未强制走 code_outline**。子 agent 工具层不阻止对超大文件 read_file | 5 个 step_limit，tool_count 高 |

## 3. 改进方案

> 核心：把"范围封闭"从 prompt 建议升级成**派活前的确定性校验**，让脑补的文件名在 0 成本处被拦截并纠正，而不是让子 agent 跑几十步撞错。

### P0-1 派 subagent 前做文件存在性校验（修 N2，最高优先）
- 落点：`tools/handlers.py` `_run_parallel_subagent_tasks` / `_run_subagent_task` 入口。
- 机制：
  - 从每个子任务的 `scope`（结构化文件列表，优先）或 prompt 正则抽取出现的文件路径。
  - 对每条路径用现有 `safe_path(p, session=_session)` + `.exists()` 校验（workspace 相对）。
  - 存在不存在的路径 → **派活前直接返回给主 agent**，不真正 spawn：
    ```
    {"dispatch_rejected": true,
     "missing_paths": ["package.js", "frontend/dashboard/build.js", "tests/agent/..."],
     "hint": "这些路径在 workspace 不存在。请用 repo_map/list_files 确认真实文件名（注意扩展名 .tsx/.mjs/.json 与目录结构）后重派。"}
    ```
  - 可选增强：对每个 missing 路径做"近似建议"——同目录下同名不同扩展名的真实文件（`build.js`→`build.mjs`），直接在 hint 里给出，省一轮往返。
- 验收：构造含错误文件名的子任务，派活前被拒并返回 missing_paths + 建议，不产生 spawn。

### P0-2 修 failure_message 代码污染（修 N3）
- 落点：`agents/subagent/failure.py` `_looks_like_tool_error` / `_classify_tool_failure`。
- 问题：子串匹配把文件**正文**里的 `ValueError`/`error:`/`exception` 误判为错误。
- 改法：
  - 只在 **tool message 的 `status == "error"`** 时归类为 TOOL_ERROR；删除/收紧 `_looks_like_tool_error` 的内容启发式（或要求匹配行首 `Traceback`/`Error:`/`Tool error:` 这种工具实际错误前缀，而非正文任意位置出现关键词）。
  - failure_message 优先取 tool 的结构化错误字段（executor 已有 `execution_error`），而非 `_first_line(content)`——content 对 read_file 是文件正文，本就不该当错误信息。
- 验收：子 agent 成功 read 一个含 `except ValueError` 的文件后超限，failure_reason 应是 step_limit 而非 tool_error，failure_message 不含代码行。

### P1-1 文件清单作为派活强制输入（治本 N1）
- 落点：`modes/coding.py` system prompt + `task`/`parallel_tasks` schema。
- 让子任务的文件清单**结构化**进 `scope.files` 字段（而非埋在 prompt 自由文本），并在 prompt 规则里强制：
  ```
  派 subagent 前必须先 repo_map/list_files 目标目录，scope.files 只能填刚刚列出的真实路径。
  禁止凭常规命名猜测文件名（本仓库用 .tsx/.mjs，测试在 proactive_v2/ 等非常规位置）。
  ```
- 配合 P0-1：scope.files 为空或含不存在路径 → 派活被拒。把"看图派活"变成硬约束。
- 验收：派出的子任务带 scope.files，且全部通过存在性校验。

### P1-2 大文件强制 code_outline（修 N4）
- 落点：子 agent 工具层（`agents/subagent/tools.py` 白名单已含 code_outline）+ `tools/handlers.py` `run_read`。
- 机制：子 agent 上下文中，read_file 对 > N 行（如 600）的文件，返回前 K 行 + 提示"此文件 1827 行，建议先 code_outline(path) 取骨架再定点 offset 读"，而非让它一路读到截断。
- 或更软：侦察兵契约里硬性写明"大文件先 outline"。
- 验收：子 agent 对 store.py 走 outline，tool_count 显著下降，step_limit 失败减少。

### P1-3 派活校验可观测性
- 落点：`runtime/trace/summary.py`。
- 新增 `dispatch_rejected_count`、`subagent_missing_file_count`，量化"脑补文件名"被拦截的次数与残留。

## 4. 关键文件清单

- `tools/handlers.py` — `_run_parallel_subagent_tasks`/`_run_subagent_task` 派活前校验（P0-1）；run_read 大文件提示（P1-2）
- `agents/subagent/failure.py` — `_looks_like_tool_error`/`_classify_tool_failure` 收紧（P0-2）
- `modes/coding.py` — scope.files 强制 + 禁止猜名规则（P1-1）
- `tools/schema.py` — task/parallel_tasks 增 scope.files 结构化字段
- `agents/subagent/tools.py` — 侦察兵大文件 outline 契约（P1-2）
- `runtime/trace/summary.py` — 校验指标（P1-3）

## 5. 验证方案

1. **单元**：
   - 派活校验：错误文件名子任务被拒、返回 missing + 近似建议（新增 `tests/test_subagent_dispatch_validation.py`）。
   - failure 分类：含 `except ValueError` 正文的成功 read 不被误判为 tool_error（扩展 `tests/test_subagent_failure*`）。
   - `pytest tests/ -q` 全绿。
2. **回归复跑** `akashic-agent`：
   - `subagent_missing_file_count` → 0（或被派活校验拦下、`dispatch_rejected_count > 0` 后主 agent 纠正）。
   - 子 agent step_limit 失败显著下降（大文件走 outline）。
   - 子 agent 成功率明显提升；failure_message 不再出现代码片段。
3. **指标对比**：本次 11 子 agent 几乎全失败 → 目标多数 subagent 成功或返回有效 partial findings。

## 6. 取舍与边界

- **派活前校验是性价比最高的一招**：把"跑 17 步撞 FileNotFoundError"提前到"派活前 0 成本拦截 + 给真实文件名建议"，直接打断 N1→N2 链。
- N3 的修复要谨慎别走偏：收紧内容启发式后，真正的工具错误仍需被识别——优先依赖 executor 的结构化 `status/execution_error`，而非文本猜测。
- P1-1 的 scope.files 结构化是治本，但需主 agent 配合填充；P0-1 的校验是即使主 agent 不配合也能兜底的硬闸。
- 不动模型、不动路由本体。

## 7. 执行顺序

P0-1（派活前校验，立即拦截脑补文件名）→ P0-2（修 failure_message 污染，让诊断可信）→ P1-1（scope.files 强制）→ P1-2（大文件 outline）→ P1-3（指标）→ 回归复跑。
