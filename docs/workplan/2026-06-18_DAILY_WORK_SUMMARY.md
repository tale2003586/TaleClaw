# 2026-06-18 工作总结

日期：2026-06-18

## 总体目标

今天的工作围绕一个核心问题展开：**Agent 多编排系统在大型只读任务上反复触发循环保护、零产出**。

工作方式是「真实 run trace 驱动」——用两次失败的 run 日志做量化归因，每修一轮就复跑验证，逐层逼近真因。同时把多模态接入、RAGAS 评测做了可行性评估（结论先行，未动工）。

主线是后者：从「乐观编排」改造成「有据编排」。

## 一、问题的发现与两次归因

### 第一次 run（`run_20260618-123507-a8b1c0`）
一个「只读架构梳理、四条独立线索」的任务（难度不高），跑了 24 步推理、98 次工具调用后被 `reasoning_step_limit` 强制停机，**没产出任何答案**。

量化归因（trace 统计）：
- 98 次调用中 56 次 read_file + 28 次 list_files，参数高度重复（`tests/` 被 list 6 次）。
- 模型自述贯穿全程："files are being partially read"、"subagents all hit tool-step limits"。
- 叠加一个 cwd 错乱 bug（bash 一度 cd 到错误仓库）。

定位出 5 条根因：
- R1：read_file 无 offset/分页，大文件静默截断 → 重读。
- R2：子 agent 超限时把残缺 summary 伪装成 `success=True`。
- R3：loop guard 只按 `(tool,args)` 判重，换 offset 即绕过。
- R4：子 agent/bash cwd 未锁定 workspace_root。
- R5：context budget 把已读内容压成占位符，逼 agent 重读。

### 第二次 run（`run_20260618-141208-b435af`）
P0 机制修复后复跑。**部分生效**：repo_map 第一步就建图、fanout 降到 2、新指标齐全、子 agent 如实报告 3 个 incomplete。但任务**仍未收敛**，第 10 步被 `repeated_tool_call` 拦停。

新归因暴露两个未修真因：
- **R5/P1-7 是当前主因**：read_file 能翻页了，但翻完的页被 context budget 压缩丢弃 → 又回去重读全文 → 触发 loop guard。给了翻书能力，却撕掉了上一页。
- **R2/P1-1·2 未做**：8 个 explore 子 agent 仍 24-66 次调用，说明子任务没变窄，侦察兵仍在干分析师的活。

## 二、方案演进（四份计划文档）

今天的设计是迭代逼近的，留下四份递进的计划：

1. `AGENT_LOOP_GUARD_TRUNCATION_FIX_PLAN.md`——机制止血（P0：read_file offset、子 agent truncated 标记、loop guard 结果指纹）。
2. `AGENT_ORCHESTRATION_CAPABILITY_FIX_PLAN.md`——把「子 agent 能力边界未编码进工具契约」提为一等根因（R2-ext、R6）。
3. `AGENT_ORCHESTRATION_FLOW_REDESIGN_PLAN.md`——把流程从「盲目 fan-out」改成六段「有据编排」：任务分类 → repo_map 建图 → 有界子任务 → 执行体选择 → coverage 校验 → 汇总/精准补查。确定用方案 B（新增 `repo_map` 工具）。
4. `SUBTASK_NARROWING_CODE_OUTLINE_PLAN.md`（今天最后一份）——把「子任务怎么做窄」落到实现规范。

## 三、几个关键认知（讨论中固化）

**1. subagent「能力弱」是故意的，不是缺陷。**
它换来的是上下文隔离（探索过程不污染主上下文）、并行、廉价无副作用。大型只读任务恰恰最需要这种「保护主上下文」的执行体。真正的问题是「任务-能力不匹配」，修法是让任务变窄去匹配能力，而非把 subagent 升级。

**2. 两级编排够用，不该加第三级。**
讨论过「主 agent → teammate → subagent」三级方案，结论是否定：两次失败的真因（P1-7 上下文丢弃、P1-1/2 任务没变窄）加一层 teammate 解决不了，只会把未修的 bug 带到新层，还多三倍编排开销。当前代码 teammate 不能 spawn subagent、subagent 不能嵌套——这个扁平约束是对的，不该打破。

**3.「做窄」必须靠确定性信息钉死，不能靠 prompt 写「请做窄」。**
四个封闭：范围封闭（repo_map 枚举点名文件）、行为封闭（定量上限）、职责封闭（删掉 synthesize 动词）、输出封闭（findings 结构化卡片）。靠 prompt 定性约束是唯一不可靠的一种。

**4. 真实工作区实测发现「行数炸弹」。**
对 `/home/tale/kaggle/coding_space/akashic-agent` 跑 repo_map 模拟，发现 `memory2/store.py`(1827行)、`agent/core/passive_turn.py`(1821)、`bootstrap/dashboard_api.py`(1353) 等超大文件——单文件就超 50000 字符上限，必被截断。这逼出一个新结论：做窄不仅要枚举文件，大文件还必须改用「读符号骨架而非读全文」，因此规划了 `code_outline` 工具。

## 四、其他可行性评估（结论先行，未动工）

- **多模态接入**：项目已有 `media` 字段贯穿 bus/session，但入口拒收、provider 不发视觉格式、路由无视觉能力判定。给了 P0 最小切片方案（Telegram + 图像，5 个文件）。
- **RAGAS 评测**：检索层已有确定性指标（precision/recall/MRR/nDCG），RAGAS 真正能补的是生成端（faithfulness/answer_relevancy，免 ground truth）和 agent 工具行为（ToolCallAccuracy）。建议 judge 接自有 provider、用独立模型防自评偏袒、pin 版本、不进 CI。

## 五、当前状态与下一步

**代码状态**：工作区已有大量改动（P0 机制：read_file offset、结果缓存、list_files 分页、子 agent truncated、loop guard 结果指纹、repo_map、新 metrics 指标均已落地并被第二次 run 验证生效）。今天的讨论产出是**计划文档**，本人要求只规划不动工。

**测试**：`pytest tests/ -q` 当前 251 passed；18 failed 中经抽查多为环境缺 psycopg（PostgreSQL DATABASE_URL）导致，非本次逻辑改动引入，需在装好 requirements 的环境复测确认。

**下一步（按 SUBTASK_NARROWING 计划的执行顺序）**：
1. P0-B：`_compress_old_tool_results` 保护近期已读内容——当前唯一仍在制造重读的主因，最高优先。
2. P0-A：新增 `code_outline` 工具——大文件刚需。
3. P1-A：explore 侦察兵契约 + findings 结构化 schema。
4. P1-B：主 agent「按文件大小分层切分」规范写进 system prompt。
5. 回归复跑 akashic-agent，断言 `stop_reason` 正常收敛、read_file 从 277 大幅下降、四条线索都有结论。
