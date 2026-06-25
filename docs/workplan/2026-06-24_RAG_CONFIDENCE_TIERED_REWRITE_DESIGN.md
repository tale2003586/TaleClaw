# 安全 RAG 置信度分层 Rewrite 重构设计

> 状态：设计（未动工，无代码改动）
> 范围：`retrieval/security_router.py` 的检索/改写决策层
> 目标：把当前"先改写、再级联检索"的逻辑，重构成"先检索、按置信度决定是否改写以及如何改写"的分层策略
> 评测基线：`.evals/security_rag_matrix/matrix_20260624-124038`（250 条用户问题）

## 1. 目标流程（用户给定）

```
用户问题
  ↓
negative / unsafe / no-evidence 模式检查
  ↓
fast dense retrieval
  ↓
置信度判断
  ├─ 高置信度：不 rewrite，直接用结果
  ├─ 中置信度：lightweight query expansion
  ├─ 低置信度：LLM rewrite + retry
  ├─ 多跳/复杂：multi-query / decomposition
  └─ 仍无证据：abstain / ask clarification
```

核心变化：**改写从"输入侧前置动作"变成"检索证据驱动的条件动作"**。改写不再默认发生，只有当 fast dense 的证据不够好时才逐级升级。

## 2. 现状与差距

### 2.1 现状实现

`route()`（`security_router.py:306`）：block 门 → keyword 命中直查 → embedding 高/低/中三段。**改写在这里就发生了**：keyword 命中调 `rewrite_query()` 拼关键词扩展；embedding 高段在 score≥0.80 时拼 `matched_intent`。

`route_with_retrieval()`（`security_router.py:407`）：已经是一个级联——fast dense → hybrid → rewrite → reranker → LLM fallback → abstain。判据集中在 `_has_direct_evidence` / `_needs_rewrite` / `_needs_reranker` / `_needs_llm_fallback`。

### 2.2 与目标流程的差距

| 目标分层 | 现状 | 差距 |
|---|---|---|
| 高置信度直接用 | `_has_direct_evidence` 已实现 | 基本对齐 |
| 中置信度 lightweight expansion | `rewrite_query` 关键词/intent 拼接 | 现在是**前置无条件**触发，不是"中置信度才触发" |
| 低置信度 LLM rewrite + retry | `_needs_llm_fallback` + LLM 分类器 | LLM 既做"要不要查"又做"改写"，职责混在一起；retry 只查一次 |
| 多跳/复杂 multi-query / decomposition | 无 | **完全缺失**，多跳只是触发 LLM fallback |
| abstain / ask clarification | 只有 abstain | **无澄清分支**，无证据一律 abstain |

### 2.3 关键差距点

1. **置信度分层不显式**：没有一个统一的 `confidence_tier(analysis) -> {high, medium, low}` 判定，散落在多个 `_needs_*` 方法里，阈值语义重叠（`retrieval_direct_threshold=0.62` 与 `retrieval_low_threshold=0.35` 之间是隐式中段）。
2. **改写时机错位**：`route()` 在没有任何检索证据时就改写；理想是先 fast dense，证据不足才改写。
3. **缺多跳分解**：`_is_complex_or_multihop` 只用来触发 LLM，没有 multi-query 检索与结果合并。这是评测里最弱的一类（见 §3）。
4. **abstain 与 ask-clarification 不分**：`insufficient_evidence` block 门和 `abstain_no_evidence` 都直接拒答，但用户问题里"缺上下文（可澄清）"与"超纲（应拒绝）"是两种处理。

## 3. 评测证据（为什么这么改）

来自 `matrix_20260624-124038`（`precise_dense + router + rewrite`，p50=57ms 的最佳平衡档）：

按问题类型 p@1：

| question_type | p@1 | mrr | hit | 读法 |
|---|---:|---:|---:|---|
| rule_mapping | 1.000 | 1.000 | 1.000 | 已饱和 |
| distractor | 0.900 | 0.900 | 0.900 | 干扰项处理好 |
| comparison | 0.778 | 0.833 | 0.944 | 良好 |
| risk_judgement | 0.710 | 0.795 | 0.986 | hit 高但 p@1 偏低，**排序问题** |
| remediation | 0.708 | 0.785 | 0.875 | 中等 |
| concept | 0.682 | 0.733 | 0.864 | 偏弱 |
| practical_debugging | 0.655 | 0.759 | 0.966 | hit 高、p@1 低，**排序问题** |
| long_tail | 0.667 | 0.833 | 1.000 | hit 满分、p@1 低，**召回到了但排不上来** |
| multi_hop | 0.455 | 0.647 | 1.000 | **最弱**，hit=1.0 说明证据在库里，是召回单查询打不全 |
| unanswerable | 0.000 | — | — | 正确拒答 |

按路由 p@1：`keyword`=0.868（最强），`fast_dense_default`=0.613，`embedding_high`=0.000（样本极少，可忽略）。

档位对比（同一 250 题）：

| 档位 | p@1 | mrr | p50 ms | 结论 |
|---|---:|---:|---:|---|
| precise_dense + router + rewrite | 0.702 | 0.784 | 57 | **基线最佳平衡** |
| precise_hybrid + router + rewrite | 0.617 | 0.726 | 60 | hybrid 在该集**反而掉点** |
| precise_hybrid + reranker | 0.803 | 0.866 | 1272 | 质量最高但慢 22× |

**三条直接结论**：

- **multi_hop / long_tail：hit=1.0 但 p@1 低** → 证据在库里，单条 query 召回但排不上来。这正是 multi-query / decomposition 的目标场景，而不是换 embedding。
- **hybrid 默认开反而掉点** → 不能无条件 hybrid，应保留为"带强标识符（CVE/CWE/rule id）时才用"的条件分支（现状 `_needs_hybrid` 已是此思路，需保留）。
- **reranker 贵 22×** → 必须是低置信度兜底的最后一跳，不能进高/中置信度主路径（现状已是最后一跳，需保留并明确"仅低置信度触发"）。

## 4. 重构设计

### 4.1 总体：四步管线 + 显式置信度分层

```
route_with_retrieval(query):
  1. gate(query)                      # 复用现有 block_patterns，新增 clarify/abstain 区分
     ├─ unsafe/deceptive/privacy/oos  → abstain(拒绝)
     └─ insufficient_evidence         → ask_clarification(可澄清)
  2. fast_dense = search(query, dense, no_rerank)
  3. tier = confidence_tier(analyze(fast_dense))
  4. dispatch by tier:
     HIGH    → 直接返回 fast_dense
     MEDIUM  → lightweight_expansion → search → 取更优者
     LOW     → llm_rewrite → search(retry) →（仍低）reranker 兜底
     COMPLEX → decompose → multi-query search → merge → （可选 rerank）
     若任何一跳达到 HIGH 证据，提前返回（短路）
  5. 终局：best 证据 ≥ low_threshold → 返回；否则 abstain / ask_clarification
```

"COMPLEX（多跳/复杂）"是**与置信度正交的旁路**：在第 3 步前先判 `is_complex(query)`，若是则直接进 decomposition 分支，不必等 fast dense 失败。理由：多跳问题 fast dense 经常"恰好有一条命中"导致 top_score 不低，但覆盖不全（hit=1.0、p@1=0.455 就是这个形态），靠置信度门发现不了。

### 4.2 置信度分层判据（核心新增）

新增单一判定函数，取代散落的 `_needs_*`：

```python
def confidence_tier(analysis) -> str:   # "high" | "medium" | "low"
    top = analysis["top_score"]
    gap = analysis["score_gap"]
    conc = analysis["source_concentration"]
    if top >= direct_threshold and (gap >= gap_threshold or conc >= concentration_threshold):
        return "high"          # = 现 _has_direct_evidence
    if top >= medium_threshold:
        return "medium"        # 新增显式中段
    return "low"
```

阈值（沿用现有 `SecurityRouteConfig`，新增一个 `retrieval_medium_threshold`）：

| 字段 | 现值 | 语义 |
|---|---:|---|
| `retrieval_direct_threshold` | 0.62 | high 下界 |
| `retrieval_medium_threshold` | **新增，建议 0.48** | medium 下界（介于 direct 与 low 之间） |
| `retrieval_low_threshold` | 0.35 | 低于此视为无有效证据，进终局 abstain |
| `retrieval_gap_threshold` | 0.06 | top1 与 top2 拉开 |
| `retrieval_concentration_threshold` | 0.50 | 命中是否集中在同一来源 |

判据基于检索结果的 `top_score / score_gap / source_concentration`，全部已在 `_analyze_hits` 算好，无需新增检索字段。

### 4.3 各分层动作

**HIGH — 直接用**
- 不改写、不再检索。返回 fast dense 结果。
- 对应现 `dense_direct`。这是最常见路径，保证低延迟。

**MEDIUM — lightweight query expansion**
- 复用现有 `rewrite_query()` 的关键词/intent 拼接（确定性、零模型成本）。
- 关键差异：**只在 medium 段触发**，而不是 `route()` 里无条件触发。
- 改写后检索一次，与 fast dense 取 `top_score` 更优者；若升到 HIGH 证据则短路返回。

**LOW — LLM rewrite + retry**
- 调 `llm_classifier` 拿到改写 query（现有 `LlmSecurityRouteClassifier` 已能返回 `query` 字段）。
- 用改写 query 重检索一次（retry）。
- 仍未达 HIGH/MEDIUM 证据 → 触发 **reranker 兜底**（仅此分层允许，控制 22× 成本）。
- 仍无证据 → 进终局。

**COMPLEX — multi-query / decomposition（新增能力）**
- 触发条件：`is_complex(query)` 命中（沿用现 `_is_complex_or_multihop` 的标记词："同时/组合/链/多跳/权衡/排查顺序"等）。
- 分解策略两选一（建议先做 A，B 作为 P2）：
  - **A. 确定性子查询**（零模型成本）：按"和/与/同时/->/，"等切分句子，加上 keyword 扩展，生成 2–4 条子 query。
  - **B. LLM 分解**：复用 LLM 分类器，prompt 改成"把复杂安全问题拆成 2–4 个可独立检索的子问题"，返回 JSON 列表。
- 每条子 query 各检索 top_k，**合并去重**（按 chunk id，取最高分），再按分数重排，可选过 reranker。
- 合并后仍按 §4.2 判 tier，决定是否兜底或 abstain。

**终局 — abstain vs ask_clarification（新增区分）**
- best 证据 `top_score ≥ retrieval_low_threshold`：返回 best。
- 否则看进入原因：
  - 命中 `insufficient_evidence` gate（缺代码/日志/架构）或 COMPLEX 分解后仍无证据 → **ask_clarification**：返回结构化追问（缺什么上下文），`action="ask_clarification"`，`use_rag=False`。
  - 命中 unsafe/deceptive/privacy/out_of_scope gate → **abstain**：拒答，`action="abstain"`。

### 4.4 数据结构变化（最小）

- `SecurityRouteConfig`：新增 `retrieval_medium_threshold: float = 0.48`。
- `RetrievalDecision`：新增可选 `clarification: str = ""`（ask_clarification 时填要追问的上下文清单），不破坏现有字段。
- `RoutedRetrievalPlan.action`：扩展取值集合，新增 `"expansion" | "llm_rewrite" | "decompose" | "ask_clarification"`（现有 `direct/dense/hybrid/rewrite/reranker/llm_fallback/abstain/blocked` 保留）。
- 新增 `RetrievalSearchRecord.tier` 字段记录每跳判定的置信度分层，便于 trace 与评测归因。

### 4.5 改写逻辑落点调整

| 改写动作 | 现位置 | 目标位置 |
|---|---|---|
| keyword 扩展 | `route()` 无条件 | 仅 MEDIUM 分层 |
| intent 拼接（score≥0.80） | `route()` 无条件 | 仅 MEDIUM 分层 |
| LLM 改写 | `route_with_retrieval` fallback | 仅 LOW 分层，职责单一化为"改写"而非"要不要查" |
| multi-query 分解 | 无 | 新增 COMPLEX 分层 |

`route()` 单独调用（被 `eval_security_router_only.py`、`label_rag_relevance.py`、ablation 脚本使用）仍需返回一个 `query`。**为保持向后兼容，`route()` 的改写行为保留**，但 `route_with_retrieval` 不再依赖 `route()` 的前置改写——它以 `query` 原文做 fast dense，改写在分层里按需发生。这样两个入口都不破坏。

## 5. 对现有调用方的影响

| 调用方 | 影响 | 处理 |
|---|---|---|
| `runtime/context.py:642` 自动上下文 | 走 `route_with_retrieval`，受益于分层 | 无需改，新增 action 透传到 trace |
| `scripts/security_rag_ask.py` | 同上 | 无需改 |
| `scripts/eval_security_rag*.py` / ablation / matrix | 调 `route()` + 外部 search | `route()` 行为保留，兼容 |
| `scripts/eval_security_router_only.py` | 只测 `route()` 的 use_rag 判定 | 兼容 |
| `tests/test_security_router.py` | 现有 7 个用例 | `route()` 用例不动；`route_with_retrieval` 用例 action 取值需更新断言 |

**向后兼容是硬约束**：`route()` 的签名、返回类型、改写语义都不变，只有 `route_with_retrieval` 内部管线重排。

## 6. 实施计划（分阶段，按优先级）

**P0 — 显式置信度分层（不改外部行为，纯重构）**
1. 加 `retrieval_medium_threshold` 到 `SecurityRouteConfig` 与 `build_*_from_env`。
2. 抽 `confidence_tier()`，用它重写 `route_with_retrieval` 的分发，替换散落的 `_needs_rewrite`/`_needs_reranker` 判断（保留方法，内部改为基于 tier）。
3. `RetrievalSearchRecord` 加 `tier`，trace 落盘。
4. 验证：`matrix --only precise_dense_router_on_rewriter_on_reranker_off` 的 p@1/mrr 不回退（±0.01 容差）。

**P1 — 多跳 multi-query（确定性版 A）**
5. 加 `is_complex` 旁路 + 确定性子查询分解 + 合并去重。
6. 验证：`by_question_type` 的 `multi_hop` p@1 从 0.455 上升；总体 p50 不超过当前 hybrid 档（~60ms）。

**P1 — abstain / ask_clarification 区分**
7. gate 命中 `insufficient_evidence` → `ask_clarification`，带缺失上下文清单。
8. 验证：`unanswerable` 仍 0 误召回；`insufficient_evidence` 路由的拒答理由可读。

**P2 — LLM 改写职责单一化 + LLM 分解（版本 B）**
9. LLM 分类器拆成 `should_retrieve` 与 `rewrite`/`decompose` 两个 prompt 用途。
10. 仅在 LOW/COMPLEX 分层调用，默认 `SECURITY_RAG_ROUTE_LLM_ENABLED=0` 不影响离线评测。

## 7. 风险与边界

- **延迟**：MEDIUM 多一次检索（~30ms），LOW 多一次 + 可能 reranker（+1200ms），COMPLEX 多 N 次检索。需保证 HIGH 短路覆盖多数请求，否则 p50 上升。缓解：HIGH 阈值不要调高；reranker 严格限 LOW。
- **hybrid 回退风险**：评测显示 hybrid 在该集掉点，**不要把 hybrid 提进主路径**，保留为强标识符条件分支。
- **多跳合并的去重正确性**：按 chunk id 去重、取最高分，需测同一 chunk 跨子查询出现的情况。
- **ask_clarification 的产品语义**：追问会打断自动上下文注入流程。建议自动上下文路径下 ask_clarification 退化为"静默不注入 + trace 标记"，只有显式工具调用路径才真正向用户追问。
- **阈值是经验值**：`retrieval_medium_threshold=0.48` 需用 matrix 扫描确认；阈值都走 env，可在不改码下调。

## 8. 验证方式

```bash
# P0 回归（同一档不回退）
python scripts/run_security_rag_matrix.py --only precise_dense_router_on_rewriter_on_reranker_off

# 分层归因看 by_route / by_question_type
python scripts/run_security_rag_ablations.py --only precise_dense

# 路由判定单测
python -m pytest tests/test_security_router.py
```

每阶段以 `by_question_type` 的弱项（multi_hop / concept / long_tail）p@1 为主指标，总体 p50 为约束指标。

## 9. 不做什么（划界）

- 不换 embedding / 不重建 collection（chunking 与 BGE-M3 已是当前最优，见 matrix `new_chunking` delta +0.25 p@1）。
- 不默认开 hybrid（该集掉点）。
- 不把 reranker 放进高/中置信度路径（22× 成本）。
- 不做 step 级检索缓存改造（属 `knowledge/caching.py` 范畴，与本次解耦）。
