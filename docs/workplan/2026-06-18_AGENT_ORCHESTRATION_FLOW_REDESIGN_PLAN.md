# Agent 编排流程改造方案：从盲目 fan-out 到有据编排

> 状态：计划（未动工）
> 触发证据：`.runs/run_20260618-123507-a8b1c0`
> 关系：本文是编排层的总设计，吸收并取代 `..._ORCHESTRATION_CAPABILITY_FIX_PLAN.md` 的 P1，复用 `..._LOOP_GUARD_TRUNCATION_FIX_PLAN.md` 的 P0 机制止血。

## 1. Context（为什么改）

原始 run 的失败本质是**乐观编排**：

```
看到多线索任务 → 直接 parallel_tasks → 子 agent 自己探索 → 父 agent 汇总
```

这条链每一环都假设"会顺利"，没有任何"先看清、后校验"的约束。结果：主 agent 在没有稳定仓库视图的情况下盲目 fan-out 4 次，子 agent 撞步数上限返回残缺结果，主 agent 不信任 → 退化成自己反复 `list_files` / `read_file` 同一批文件，24 步预算烧光、零产出。

目标流程改成**有据编排**：

```
任务分类 → 建文件地图 → 生成有边界子任务 → 执行体选择 → coverage 校验 → 汇总或精准补查
```

核心立场：**先用一次确定性操作建立全局地图，再基于地图派发有边界的任务，最后用机械断言校验覆盖**——把"会不会成功"从赌博变成可验证。

## 2. 设计原则（避免用复杂度治复杂度）

1. **分级触发**：简单任务不进编排，直接干。整套六段流程只对"多线索 / 大仓库"任务启用。
2. **地图确定性、一次拿全、不经模型**：地图是工具产物，不是模型反复 `list_files` 的结果。必须一次完整、可缓存、防截断。
3. **coverage 校验是机械断言，不是 LLM 自评**：只对结构化返回做硬检查（每线索有非空结论？有 incomplete 标记？），不引入语义级 LLM 判断。
4. **补查有硬上限**：最多补 1 轮，仍缺则如实标注未完成，绝不无限补查（否则把死循环从工具层挪到编排层）。

## 3. 六段流程详解与落点

### 3.1 任务分类（内联，不额外调模型）
- 落点：`modes/coding.py` `CODING_PROFILE.system_prompt` 加决策规则。
- 规则：
  - 单线索 / 单文件 / 小范围 → 主 agent 直接执行，**跳过编排**。
  - 多线索（用户显式列了 N 条）/ 大仓库 / 跨子系统综合 → 进编排流程。
  - 需要写码并多轮迭代 → 走 `spawn_teammate`（分析师）；只需定位/列举/提取 → `parallel_tasks`（侦察兵）。
- 不新增 LLM 分类调用，靠 prompt + 工具契约引导主 agent 首轮自己判断。

### 3.2 建文件地图 —— 新增 `repo_map` 工具（方案 B，核心新代码）
- 落点：`tools/handlers.py` 新增 `run_repo_map`，`tools/schema.py` 注册 `repo_map`，`BASE_HANDLERS` 接线。
- 行为（确定性、不经模型）：
  - 以 `workspace_root_for_session(_session)` 为根，遍历仓库，**优先用 `git ls-files`**（自动尊重 .gitignore、排除 `.git`），非 git 仓库回退到 `rglob` 并复用现有的 `.`/`__pycache__` 过滤。
  - 每个文件输出 `path` + `lines`（行数，廉价 `read_text().count("\n")`，可对二进制/超大文件跳过计数）。
  - 目录聚合：输出目录级 `file_count` + `total_lines`，让大目录可被折叠概览。
  - 支持参数：`path`（子树根，默认全仓）、`max_depth`（可选，控制粒度）、`include_lines`（默认 true）。
- **防截断**（吸取 R1 教训）：
  - 输出走与 `run_read` 一致的分页机制 —— 大仓库地图也能超 `MAX_WORKSPACE_READ_CHARS`，所以同样支持 `offset` 并在截断时给 `next_offset` 续读提示。
  - 但优先**分层概览**：默认先返回"目录树 + 每目录文件数/行数"，文件级明细按需用 `path=` 下钻，避免一次性吐出上万行。
- **缓存**：复用本次已落地的 `_tool_result_cache`（session 级，write/edit 后失效）。地图在一次 run 内稳定，命中缓存直接返回，杜绝重复扫描。
- 验收：对中型仓库一次 `repo_map` 返回完整目录概览（文件数+行数），无静默截断；二次调用命中缓存。

### 3.3 生成有边界的子任务（侦察兵契约 + 结构化返回）
- 落点：`agents/subagent/tools.py` `SUBTASK_SYSTEM_PROMPTS`。
- explore 子 agent 契约改写：
  > "你是一次性侦察兵，约 16 步预算。基于调用方给的文件清单，只做：读指定文件、提取关键片段、回报 `path + 行号 + 一句话结论`。**不要试图读完并综合整个子系统**——综合由调用方负责。信息超预算就回报已找到的清单并置 incomplete=true，不要硬撑重读。"
- 结构化返回：explore 返回 `{findings: [{path, lines, note}], incomplete: bool}` 而非长 summary（配合 P0-2 的 `truncated/stop_reason`）。
- 主 agent 用 `repo_map` 的地图把"探索 memory2"细化成"读这 5 个具体文件，回报每个的职责"——**有界来自地图，不再靠猜**。

### 3.4 执行体选择
- 落点：同 3.1 的 system prompt 规则 + `tools/schema.py:513` task/parallel_tasks 描述补能力边界。
- task/parallel_tasks 描述加：
  > "子 agent：约 16 步、隔离上下文、受限只读工具。**适合**有界定位/列举/提取/独立分片探索；**不适合**跨多文件深度综合、写码迭代、多轮反馈——那类用 `spawn_teammate`。"

### 3.5 coverage 校验（确定性断言，主 agent 侧）
- 落点：主 agent 推理逻辑由 prompt 引导（不新增校验 LLM 调用）；`agents/subagent/parallel.py` 已透传 `truncated/incomplete`，主 agent 据此判断。
- 机械检查项（主 agent 拿到聚合结果后自检）：
  1. 每个派出的子任务都返回了吗？
  2. 有 `truncated=True` / `incomplete=True` 的吗？
  3. 用户声明的 N 条线索，每条至少有一个非空 finding 吗？
- 全通过 → 进汇总；任一不通过 → 进补查。

### 3.6 汇总或精准补查
- 落点：system prompt 规则 + 补查硬上限。
- 缺口 → 针对**缺失的那条线索**派一个窄子任务补查（用地图锁定具体文件），**不整体重跑**。
- 补查**最多 1 轮**；仍缺则如实汇总并标注"线索 X 信息不全（原因：子任务超限/文件过大）"，停止。

## 4. 与底层机制修复的关系（互补，缺一不可）

| 层 | 来源 | 作用 |
|---|---|---|
| 机制止血（P0） | LOOP_GUARD 计划 | read_file offset、子 agent truncated 标记、loop guard 结果指纹 —— 让"截断/重复/残缺"可见可拦 |
| **编排有章法（本文）** | 本文 | 先地图后派活、有界子任务、coverage 校验、精准补查 |

光有本文流程但底层还静默截断 → 地图和子结果照样不可靠；光有 P0 机制但仍盲目 fan-out → 省不下步数。两者叠加才闭环。**注：P0 的 read_file offset / 结果缓存 / list_files 分页等已在当前工作区落地，本文的 `repo_map` 直接复用其分页与缓存设施。**

## 5. 关键文件清单

- `tools/handlers.py` — 新增 `run_repo_map`（复用 `_tool_result_cache` / `_format_line_window` / `workspace_root_for_session`）；`BASE_HANDLERS` 接线
- `tools/schema.py` — 注册 `repo_map`；补 task/parallel_tasks 能力边界描述
- `agents/subagent/tools.py` — explore 侦察兵契约 + 结构化返回约定
- `agents/subagent/runner.py` / `parallel.py` — 结构化 findings 解析与透传（已有 truncated/stop_reason 基础）
- `modes/coding.py` — 六段流程的分类/选型/校验/补查规则写入 system prompt
- `runtime/trace/summary.py` — 新增 `subagent_fanout_count` 等编排健康指标
- `config.py` — `REPO_MAP_MAX_CHARS`、补查轮数上限等开关

## 6. 验证方案

1. **单元/集成**：
   - `repo_map` 在 git 仓库返回完整目录概览、行数正确、二次命中缓存、大仓库分页不静默截断（新增 `tests/test_repo_map.py`）。
   - explore 子 agent 结构化返回可被父侧解析；`incomplete` 透传。
   - `pytest tests/ -q` 全绿。
2. **回归复跑原始 case**（akashic-agent 只读梳理）：
   - 主 agent 首轮调一次 `repo_map` 建图，后续 `list_files` 调用数大幅下降。
   - `stop_reason != reasoning_step_limit`；`subagent_fanout_count` ≤ 2；四条线索都有非空结论。
3. **指标目标**：修复前 98 次调用 / 0 产出 / 28 次 list_files / 4 次 fan-out → 修复后 <40 次调用、list_files <8 次、fan-out ≤2、有完整答案。

## 7. 取舍与边界

- **方案 B（新 `repo_map` 工具）的理由**：它从根上满足"地图必须一次拿全、不截断、可缓存"这个硬约束。复用 `bash`（方案 A）虽零新代码，但 bash 输出同样受 50000 字符截断，等于把地图不可靠的问题原样保留。
- 分级触发避免简单任务被六段流程拖累；coverage 与补查都是确定性 + 硬上限，不引入新的 LLM 自评循环。
- 不动模型、不动路由本体；新增一个只读工具 + 改子 agent 契约 + 改 system prompt 规则，按段独立可回滚。
- 风险：`repo_map` 行数统计对超大/二进制文件要跳过，避免读爆内存；补查轮数上限需实测（默认 1 轮）。

## 8. 执行顺序

P0 机制（已落地，校验）→ `repo_map` 工具（3.2，核心新代码）→ 子 agent 侦察兵契约 + 结构化返回（3.3）→ system prompt 六段规则（3.1/3.4/3.5/3.6）→ 编排健康指标（trace）→ 回归复跑原始 case。
