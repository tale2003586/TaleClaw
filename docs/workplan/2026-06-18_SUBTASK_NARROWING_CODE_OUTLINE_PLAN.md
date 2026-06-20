# 子任务做窄实现规范：按文件大小分层 + code_outline + findings schema

> 状态：计划（未动工）
> 触发证据：`.runs/run_20260618-141208-b435af`（第二次失败）+ 真实工作区 `/home/tale/kaggle/coding_space/akashic-agent` 实测
> 关系：本文是 `..._ORCHESTRATION_FLOW_REDESIGN_PLAN.md` 中 P1-1/P1-2「子任务做窄」的落地实现规范，从原则细化到「对着这个仓库能直接跑」。

## 1. Context（为什么需要这一层）

第二次复跑（`run_20260618-141208-b435af`）证明：`repo_map`、read_file offset、子 agent truncated 标记都已生效，但任务仍未收敛（第 10 步被 `repeated_tool_call` 拦停）。两个真因尚未修：

- **R5/P1-7**：context budget 仍把已读内容压成占位符，agent 翻页后留不住 → 重读。
- **R2/P1-1·2**：子任务没变窄，8 个 explore 子 agent 仍 24-66 次调用、3 个 incomplete。

对真实工作区做 `repo_map` 实测后，找到了「窄」迟迟落不了地的硬障碍——**仓库里有一批超大文件**：

| 文件 | 行数 | 后果 |
|---|---|---|
| `memory2/store.py` | 1827 | 单文件超 `MAX_WORKSPACE_READ_CHARS`(50000)，必被截断 |
| `agent/core/passive_turn.py` | 1821 | 同上 |
| `bootstrap/dashboard_api.py` | 1353 | 同上 |
| `frontend/dashboard/src/main.tsx` | 987 | 接近上限 |
| `agent/looping/core.py` / `memory2/retriever.py` | 736 / 661 | 偏大 |

子任务范围里只要含一个这种文件，侦察兵就必然 read→截断→翻页→被压缩→重读→超限。**所以「做窄」不仅是枚举文件，还必须对大文件改用「读骨架而非读全文」。**

## 2. 目标

让主 agent 能基于 `repo_map` 的「文件清单 + 行数」把任务切成**侦察兵在 16 步内必然完成**的窄子任务，核心是「四个封闭」+ 大文件特判：

1. 范围封闭——文件由 repo_map 枚举点名，子 agent 不自己找。
2. 行为封闭——定量上限（≤N 文件、≤M 行）。
3. 职责封闭——动词只能是 locate/extract/report，删掉 synthesize/summarize。
4. 输出封闭——结构化 findings 卡片，非自由长文。
5. **大文件特判**——超阈值文件只取符号骨架，不读全文。

## 3. 实现项

### P0-A 新增 `code_outline` 工具（大文件刚需，核心新代码）
- 落点：`tools/handlers.py` 新增 `run_code_outline`；`tools/schema.py` 注册；`BASE_HANDLERS` + subagent 白名单接线。
- 行为（确定性、不经模型）：
  - 输入 `path`（单文件）。读取后用语言无关的轻量正则抽取**顶层/类成员符号 + 行号**：
    - Python：`^class `、`^def `、`^\s+def `、`^\s+async def `。
    - JS/TS：`^(export )?(async )?function `、`^(export )?class `、`^\s*[\w]+\s*\(.*\)\s*\{`（方法）、顶层 `const X = (`。
  - 输出 `{path, total_lines, symbols: [{name, kind, line}]}`，按行号排序。
  - 复用 `_tool_result_cache`（session 级，write/edit 失效）。
  - 输出本身也可能大（巨型文件符号多），同样走 `_format_line_window` 分页，但符号表通常远小于全文。
- 为什么不让 agent 手写 `grep`（方案 A 的替代）：这仓库一堆 1000+ 行文件，符号骨架是高频刚需；工具化后确定性、可缓存、跨语言统一，比每次靠 prompt 教 grep 可靠。
- 验收：对 `memory2/store.py`(1827行) 返回类/函数符号表（几十行）而非全文；命中缓存二次零 IO。

### P0-B context budget 保护近期已读内容（R5/P1-7，提为 P0）
- 落点：`runtime/context_history.py` `_compress_old_tool_results`。
- 改法（二选一或叠加）：
  - 把 `read_file`/`repo_map`/`code_outline`/`list_files` 的**最近一次**结果纳入 `preserve_tools` 保护，不压缩。
  - 对被压缩的读结果，占位符里保留 `(path, offset, lines)` 元信息，让 agent 知道「读过、如何重取」而非盲目重读全文。
- 这是当前唯一仍在制造重读的源头，必须先于编排优化修。
- 验收：构造「读大文件 → 若干步后该结果仍在上下文（或占位符含可重取元信息）」的用例断言。

### P1-A explore 子 agent 侦察兵契约 + findings schema（P1-1/P1-2）
- 落点：`agents/subagent/tools.py` `SUBTASK_SYSTEM_PROMPTS["explore"]`。
- 契约文案（要点）：
  > "你是一次性侦察兵，约 16 步。基于调用方给的明确文件清单，只做 locate/extract：读点名的文件（大文件用 `code_outline` 取骨架），回报结构化 findings。**禁止**自己扩大范围、读未点名文件、做跨文件综合。信息超预算就置 `incomplete=true` 回报已有结果，不要重读。"
- findings schema（返回约定，`agents/subagent/runner.py` 解析）：
  ```json
  {"findings": [{"path": "...", "lines": "1-20", "role": "一句话职责", "entry": "入口函数名"}],
   "incomplete": false}
  ```
- 验收：子 agent 对「读 4 个点名小文件」任务返回结构化 findings、调用数 < 10、不超限。

### P1-B 主 agent「按大小分层切分」规范（写进 system prompt）
- 落点：`modes/coding.py` `CODING_PROFILE.system_prompt`。
- 切分序列（固化为规则）：
  ```
  1. repo_map 拿到目标范围的文件清单 + 行数
  2. 按行数分层切子任务：
     - 小文件(≤~300行): 批量直接 read_file，回报职责+入口
     - 大文件(>~300行): 用 code_outline 取符号骨架，回报符号表+行号，不读全文
  3. 子任务枚举具体文件、给定量上限、动词限定 locate/extract
  4. parallel_tasks 派发(只读侦察) 或 spawn_teammate(需综合/写码)
  5. 主 agent 自己用 findings 做跨线索综合
  ```
- 针对本仓库四条线索的样例切分写进 prompt 注释/few-shot（见附录）。

## 4. 针对 akashic-agent 四条线索的样例切分（验证用例）

- **线索1（agent looping/turns/core）**：turns/* 全小文件直接读；`passive_turn.py`(1821)、`looping/core.py`(736) → `code_outline`。
- **线索2（memory2）**：11 个 ≤365 行文件批量读取（retriever.py 取前 80 行）；`store.py`(1827)、`retriever.py`(661) → `code_outline`。
- **线索3（前端/dashboard）**：`package.json`(29)、`build.mjs`(19) 直接读；`main.tsx`(987)、`dashboard_api.py`(1353) → `code_outline`。
- **线索4（tests）**：`repo_map path=tests` 拿清单 → 按 loop/memory/dashboard 关键字匹配文件名定位，不读测试体。

## 5. 关键文件清单

- `tools/handlers.py` — 新增 `run_code_outline`（复用 cache/分页/workspace_root）
- `tools/schema.py` — 注册 `code_outline`
- `agents/subagent/tools.py` — explore 侦察兵契约 + findings schema + 白名单加 `code_outline`
- `agents/subagent/runner.py` / `parallel.py` — findings 解析与透传
- `runtime/context_history.py` — `_compress_old_tool_results` 保护已读（P0-B）
- `modes/coding.py` — 主 agent 分层切分规范
- `config.py` — `CODE_OUTLINE_*`、大文件行数阈值开关

## 6. 验证方案

1. **单元**：`code_outline` 对 Python/TS 大文件返回符号表（新增 `tests/test_code_outline.py`）；context budget 保护已读用例（扩展 `tests/test_context_*`）；findings 解析。`pytest tests/ -q` 全绿。
2. **回归复跑** `akashic-agent` 只读梳理：
   - `stop_reason` 既非 `reasoning_step_limit` 也非 `repeated_tool_call`（正常收敛）。
   - `read_file` 总数从 277 大幅下降；大文件改走 `code_outline`。
   - `duplicate_tool_call_count` 从 79、`truncated_tool_output_count` 从 77 显著下降。
   - 四条线索都有非空结论。
3. **指标目标**：第二次 404 次调用 / 277 read_file / 0 产出 → <120 次调用、read_file <60、有完整答案。

## 7. 取舍与边界

- P0-B（保护已读）必须先做——否则 code_outline/repo_map 的产出同样会被压缩丢弃，白搭。
- `code_outline` 用正则而非完整 AST：跨语言、零依赖、够用；代价是对非常规写法可能漏符号，可接受（它只用于定位，不用于精确分析）。
- 大文件阈值（~300 行）需实测调；过低会让小文件也走 outline 丢失细节，过高则大文件仍被读爆。
- 不动模型、不动路由本体；新增一个只读工具 + 改子 agent 契约 + 改 system prompt + 修上下文压缩。按项可独立回滚。

## 8. 执行顺序

P0-B（保护已读，止当前主因）→ P0-A（code_outline 工具）→ P1-A（侦察兵契约 + findings schema）→ P1-B（主 agent 分层切分规范）→ 回归复跑 akashic-agent。
