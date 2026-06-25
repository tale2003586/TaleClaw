# 安全 RAG「全量差异化 Rewrite」设计方案

> 状态：设计（未动工，无代码改动）
> 范围：`retrieval/security_router_core.py` 的 `route_with_retrieval` 改写决策，以及 `security_router_providers.py` 的 rewriter
> 目标：把当前"高置信度跳过改写"改成"所有 query 都改写，但按 query 类型差异化改写"
> 前提：LLM rewriter（`LLMQueryRewriteProvider` + 规则 fallback）已接入

## 1. 当前流程分析（实测代码，非记忆）

### 1.1 改写在两个地方发生，且第一处被丢弃

`route()`（`security_router_core.py:473`）会做**前置改写**：keyword 命中拼扩展、embedding≥high 拼 `matched_intent`。

但 `route_with_retrieval`（`:574`）拿到 `base_decision` 后立刻 `self._with_route(base_decision, query=original_query)`（`:615`），**把前置改写的 query 重置回原文**。fast dense 用的是原始 query。

→ 结论：`route()` 的前置改写对 `route_with_retrieval` 链路实际无效，只对单独调 `route()` 的评测脚本生效。这是当前最容易被误解的一点。

### 1.2 改写当前是「按 tier 条件触发」，不是全量

`route_with_retrieval` 的实际分发（`:632`–`:896`）：

| 进入条件 | 改写动作 | provider mode | 是否检索 |
|---|---|---|---|
| `is_complex_or_multihop` | 多查询分解 | `decompose` | 多次 + 合并 + 强制 rerank |
| `dense_tier == high` | **不改写**，直接返回 fast dense | — | 否（短路返回，`:648`） |
| `_needs_hybrid` | 不改写，换 hybrid 模式 | — | hybrid |
| `dense_tier == medium` | 轻量扩展 | `expansion` | dense + 强制 rerank |
| `dense_tier == low` | 重试改写 → LLM 分类器 → reranker | `low_retry` | 多次 |
| 终局 | 无 | — | abstain / ask_clarification |

**核心现状：HIGH 置信度的 query 完全不经过改写。** 这正是你要改的点。

### 1.3 已有的改写能力（可直接复用）

`RewriteRequest.mode` 已支持三种语义：`expansion` / `low_retry` / `decompose`（`security_router_utils.py:50`）。provider 协议 `QueryRewriteProvider.rewrite(request) -> RewriteResult`，规则版与 LLM 版都已实现，LLM 失败自动回落规则版（`providers.py:148`）。`RewriteResult` 已支持多 query 列表。

→ 差异化改写的**基础设施已经齐全**，缺的是「为每一类 query 选择 mode 的策略」和「HIGH 也要改写」。

## 2. 目标：全量差异化改写

把"是否改写"从决策里去掉——**所有 query 都至少产生一个改写候选**。差异在于"用哪种改写策略 + 是否用改写结果替换原文检索"。

### 2.1 改写策略分类（按 query 特征，而非仅 tier）

改写策略由两类信号决定：**query 静态特征**（改写前就能判）和**检索证据 tier**（fast dense 之后才有）。

| 策略 | 触发信号（静态优先，tier 兜底） | mode | 改写内容 | 设计意图 |
|---|---|---|---|---|
| **identifier-preserving** | 含 CVE/CWE/GHSA/包名/rule id（`_needs_hybrid` 命中） | `expansion`（轻量） | 保留标识符原文，仅补同义安全术语，**不漂移** | 强标识符 query 改写易伤召回，只做最小扩展 |
| **keyword-anchored** | `_keyword_matches` 命中（sqli/xss/jwt…） | `expansion` | 拼该 keyword 的英文术语扩展 | 已知漏洞类型，确定性扩展最稳 |
| **intent-expansion** | 无 keyword 但 embedding 命中某 intent | `expansion` | 拼 `matched_intent` 英文术语 | 语义清楚但表述口语化，补术语提召回 |
| **clarify-rewrite** | 短/模糊/口语（"这样安全吗"） | `low_retry` | LLM 补全攻击面、漏洞类型、修复关键词 | 表述信息量低，需 LLM 补语义 |
| **decompose** | `is_complex_or_multihop` | `decompose` | 拆 2–4 条子 query | 多跳/组合问题单查询打不全（实测 multi_hop p@1=0.455、hit=1.0） |

注意：前三种都是 `expansion` mode，差别在改写**素材**（标识符 vs keyword 表 vs intent 串）。这正是"差异化"的体现——同一个 mode，喂给 rewriter 的上下文不同，产出不同。`RewriteRequest` 已经带了 `keyword_matches` / `matched_intent` / `top_hits`，rewriter 能据此差异化（LLM prompt 已用这些字段，见 `utils.py:78`）。

### 2.2 关键安全设计：always-rewrite 但 best-of 选择

「所有 query 都改写」最大的风险是**伤害本来就强的 query**（改写漂移）。实测里 rewrite 对总体只有 +0.037 p@1 的提升，强 query 上收益更小、风险更大。

所以全量改写必须配 **best-of-N 候选竞争**，而不是无脑用改写结果替换：

```
candidates = [original_query]            # 原文永远是候选之一
candidates += rewrite_result.queries     # 差异化改写产出
对每个 candidate 检索（HIGH 时可只检索原文 + 1 条改写）
按 (top_score, score_gap, concentration) 选最优 candidate 的 hits
```

这样：
- 改写有效 → best-of 选中改写结果，召回提升。
- 改写漂移 → best-of 仍保留原文结果，不回退。
- HIGH query → 多检索 1 次（改写候选），代价可控，且不会比原文差。

### 2.3 各 tier 在新方案下的行为

| tier | 现状 | 新方案（全量改写） |
|---|---|---|
| HIGH | 不改写，直接返回 fast dense | **生成 1 条差异化改写候选**，与原文 best-of，取优。短路条件改为"原文已 HIGH 且改写未超越即用原文"，避免无谓多检索 |
| MEDIUM | expansion + rerank | 不变（已是改写），但改写素材按 §2.1 差异化 |
| LOW | low_retry + LLM + rerank | 不变 |
| COMPLEX | decompose | 不变 |

→ 实际只有 **HIGH 这一档行为改变**：从"零改写"变成"一条差异化改写候选 + best-of"。MEDIUM/LOW/COMPLEX 本就在改写，只需把"选 mode 的素材"按 §2.1 表显式化。

## 3. 改动落点（仅描述，不写代码）

### 3.1 新增「改写策略选择器」

在 `SecurityRetrievalRouter` 加一个纯函数，把 query 特征映射到 `(mode, 改写素材标签)`：

```
choose_rewrite_strategy(query, decision, tier) -> RewriteStrategy
  identifier  if _needs_hybrid(query)
  keyword     elif decision.keyword_matches
  decompose   elif is_complex(query)
  intent      elif tier in {high, medium} and matched_intent
  clarify     else  (low / 模糊)
```

它只决定 mode 和喂给 `RewriteRequest` 的侧重，不自己改写。LLM rewriter 仍是唯一改写执行者（规则版兜底）。

### 3.2 HIGH 分支改为 best-of

`route_with_retrieval` 里 `if dense_tier == "high"`（`:648`）的直接返回，改为：

1. `strategy = choose_rewrite_strategy(...)`
2. 生成 1 条改写候选 → 检索
3. `best_of([dense_hits, rewrite_hits])`
4. 返回 best（route 标记 `high:rewrite_kept` 或 `high:rewrite_win`，便于 trace 区分改写是否赢过原文）

### 3.3 统一 best-of 工具

把"多个 candidate hits 选最优"抽成一个方法（`_best_of(analyses) -> hits`），MEDIUM/LOW 的"取更优者"逻辑（现在散在 `if ... >= best_score`）也复用它，减少重复。

### 3.4 rewriter prompt 差异化

`_llm_rewrite_prompt`（`utils.py:47`）已按 mode 给不同指令。建议为 §2.1 的 identifier / keyword / intent 三种 expansion 子类，在 prompt 里多传一个 `strategy` 字段，让 LLM 知道"这次只保标识符做最小扩展"还是"可以补 intent 术语"。这是 prompt 级差异，不改架构。

## 4. 配置与可观测

新增 env（默认保持当前行为，灰度开启全量改写）：

| env | 默认 | 作用 |
|---|---|---|
| `SECURITY_RAG_REWRITE_ALWAYS` | `0` | 1 时 HIGH 也走 best-of 改写 |
| `SECURITY_RAG_REWRITE_BEST_OF` | `2` | 每档最多并比的 candidate 数 |
| `SECURITY_RAG_REWRITE_LLM_ENABLED` | `0`（现有） | 是否用 LLM rewriter |

`RetrievalSearchRecord` 已有 `tier`；建议再加 `rewrite_strategy` 与 `rewrite_won`（改写是否赢过原文），trace 里能直接归因"全量改写在 HIGH 档到底有没有用、有没有反伤"。

## 5. 风险与边界

- **延迟**：HIGH 档从 1 次检索变 2 次（+~30ms dense）。若 HIGH 占比高，p50 会升。缓解：HIGH 的改写候选只跑 dense、不跑 rerank；`best_of` 早停（原文已 HIGH 且改写未超越即停）。
- **改写漂移反伤强 query**：best-of 保留原文兜底是硬要求，不能用改写结果直接替换。上线后用 `rewrite_won` 指标确认 HIGH 档改写胜率，低于阈值就关 `REWRITE_ALWAYS`。
- **LLM 成本/抖动**：全量改写若都走 LLM，QPS 翻倍且引入模型抖动。建议 HIGH/identifier/keyword 三类**走规则改写**（零成本、确定性），只有 clarify/decompose 走 LLM。即"全量改写"≠"全量 LLM"。
- **标识符漂移**：identifier 策略必须在 prompt 里硬约束"不得新增原文没有的 CVE/包名"（现有 prompt `utils.py:66` 已有这条，复用）。
- **缓存**：改写候选会变 query，缓存 key 变多。`use_cache` 已逐跳透传，影响可控。

## 6. 实施计划（按优先级）

**P0 — 策略选择器 + HIGH best-of（行为可灰度）**
1. 加 `choose_rewrite_strategy` 纯函数 + 单测。
2. 抽 `_best_of`，HIGH 分支接 best-of，受 `REWRITE_ALWAYS` 控制，默认关。
3. 验证：`REWRITE_ALWAYS=0` 时 matrix 指标与当前完全一致（回归保护）。

**P1 — 差异化改写素材**
4. expansion 拆 identifier/keyword/intent 三种素材，prompt 传 `strategy`。
5. 规则改写承接 identifier/keyword/intent，LLM 只接 clarify/decompose。
6. 验证：`REWRITE_ALWAYS=1` 下 `by_question_type` 的 concept/remediation p@1 上升，`rewrite_won` 在 HIGH 档 ≥ 阈值。

**P2 — trace 归因 + 阈值固化**
7. `RetrievalSearchRecord` 加 `rewrite_strategy`/`rewrite_won`，落 trace。
8. 用 matrix 扫 best-of 收益，决定 `REWRITE_ALWAYS` 是否转默认开。

## 7. 验证方式

```bash
# 回归：关闭全量改写应与当前完全一致
SECURITY_RAG_REWRITE_ALWAYS=0 python scripts/run_security_rag_matrix.py \
  --only precise_dense_router_on_rewriter_on_reranker_off

# 全量改写收益
SECURITY_RAG_REWRITE_ALWAYS=1 python scripts/run_security_rag_matrix.py \
  --only precise_dense_router_on_rewriter_on_reranker_off

python -m pytest tests/test_security_router.py
```

主指标：`by_question_type` 弱项（concept/remediation/long_tail）p@1；约束指标：总体 p50、HIGH 档 `rewrite_won` 胜率。

## 8. 不做什么

- 不在 fast dense 之前就用改写替换原文（保留"原文先检索 + best-of"，避免盲改伤召回）。
- 不让 HIGH/identifier/keyword 走 LLM（确定性规则改写足够，省成本省抖动）。
- 不动 `route()` 的前置改写语义（评测脚本依赖，且 `route_with_retrieval` 本就忽略它）。
- 不默认开 `REWRITE_ALWAYS`，先灰度 + trace 验证胜率再固化。
