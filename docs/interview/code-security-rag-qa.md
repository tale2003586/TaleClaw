# 代码安全 RAG 系统逐题答辩稿

本文针对“代码安全 RAG 系统追问”逐题作答，答案基于当前仓库实现和本机知识库状态。重点代码入口：

- `knowledge/security_rag.py`：Qdrant 索引、dense / hybrid 检索、RRF、payload filter、reranker、环境变量构建。
- `knowledge/chunking/*.py`：安全资料切块策略，包括 advisory、Semgrep rule、Markdown / plain text。
- `retrieval/security_router_core.py`：安全检索路由、query rewrite、dense / hybrid / rerank 分层检索、evidence gate。
- `retrieval/security_router_defaults.py`：安全关键词、意图模板、负向拦截规则。
- `plugins/security_rag/plugin.py`：`security_rag_search` 工具。
- `runtime/context.py`、`runtime/pipeline.py`：安全知识自动注入上下文和每轮只注入一次的控制。
- `runtime/trace/rag.py`、`knowledge/tracing.py`：RAG runtime trace 和独立 JSONL trace。
- `scripts/ingest_security_kb.py`、`scripts/eval_security_rag_v2.py`、`scripts/run_security_rag_matrix.py`：入库和评测脚本。
- `tests/test_security_rag_chunking.py`、`tests/test_security_rag_hybrid.py`、`tests/test_security_rag_observability.py`、`tests/test_security_router.py`：切块、混合检索、trace 和路由测试。

当前本机知识库状态：

- 原始目录：`/home/tale/kaggle/code-security-kb`，约 `6.5G`。
- 当前主集合：`code_security_kb_bge_m3_hybrid`。
- 当前 Qdrant point 数：`768870`，dense 向量维度 `1024`，另有 sparse vector。
- 当前 `.env`：`SECURITY_RAG_EMBEDDING_PROVIDER=bge_m3`、`SECURITY_RAG_HYBRID_ENABLED=1`、`SECURITY_RAG_SPARSE_EMBEDDING_PROVIDER=hash`、`SECURITY_RAG_RERANKER_CANDIDATES=12`。
- 当前 `.env` 里 `SECURITY_RAG_AUTO_CONTEXT_ENABLED=0`，所以自动注入能力在代码里支持，但当前默认关闭；`SECURITY_RAG_PLUGIN_ENABLED=1`，工具检索默认开启。

## 0. 总括回答

我的代码安全 RAG 不是简单“向量库查一下资料”，而是给 Agent 增加一层本地安全证据系统。它解决三类问题：

1. 安全知识时效性和可追溯性。漏洞、依赖公告、Semgrep 规则和 OWASP 文档需要引用本地证据，不能只靠模型记忆。
2. 检索决策。不是所有问题都应该检索，所以我做了 security router，先负向 gate，再按关键词、embedding intent、retrieval evidence 分层判断。
3. 可复盘。每次路由、改写、检索、重排、命中文档和注入内容都会写 trace，用来定位失败是路由、召回、排序还是上下文注入问题。

一条典型链路是：

```text
用户问题
-> security router 判断是否需要安全知识
-> rule / LLM query rewrite 或复杂问题拆解
-> dense 检索
-> 必要时 hybrid 检索，dense + sparse 通过 RRF 融合
-> 中低置信时 reranker 重排
-> evidence gate 判断是否足够
-> 注入 <security_knowledge> 或由 security_rag_search 工具返回
-> trace / eval 记录
```

面试开场可以这样说：

> 我做这个系统的核心目的，是把代码安全问答从“模型凭记忆回答”变成“基于本地安全证据回答”。当前知识库约 6.5GB，主集合约 76.9 万个 chunk，使用 BGE-M3 dense 向量加 sparse hybrid 检索，再用路由、改写、RRF、reranker 和 trace 做工程化闭环。

## 1. 你为什么要做“代码安全 RAG”，而不是让模型直接回答安全问题？

因为代码安全问题有三个特点，不适合完全依赖模型参数记忆。

第一，漏洞知识会变。CVE、GHSA、依赖受影响版本、修复版本、CVSS 严重性都会更新。模型直接回答容易过期。

第二，安全问答需要证据。比如“这个 Java 依赖版本有没有漏洞”“这个 Semgrep 规则是什么意思”“JWT 放 localStorage 有什么风险”，回答最好能指向 advisory、规则或 OWASP 文档，而不是只给泛化建议。

第三，模型容易编造具体编号。安全领域里编一个不存在的 CVE、错误包名或错误修复版本，风险比普通闲聊高很多。

所以我把 RAG 作为安全场景的证据层：模型仍然负责理解、归纳和给修复建议，但事实依据来自本地知识库。代码里体现为 `security_rag_search` 工具和 `ContextBuilder._build_security_knowledge_block()` 自动注入能力。

工程边界也要说明：RAG 不能替代真实代码审计、SCA、SAST 和运行时日志。它解决的是“引用已有安全资料、降低幻觉、提高召回”的问题，不是证明系统一定安全。

## 2. 你说原始安全资料约 6GB，具体包括哪些来源？比如 CVE、GitHub Advisory、Semgrep 规则、OWASP 文档分别怎么处理？

当前本地目录约 `6.5G`，主要来源在 `/home/tale/kaggle/code-security-kb/data/raw`：

| 来源目录 | 当前规模 | 处理方式 |
| --- | ---: | --- |
| `advisory-database` | 约 33.9 万个文件，主要是 JSON | 用 `JsonAdvisoryChunking` 结构化解析 GitHub Advisory / GHSA。CVE 主要来自 `id` 或 `aliases` 字段，例如 `CVE-*`。 |
| `semgrep-rules` | 约 4309 个文件，主要是 YAML，也有规则测试代码 | 用 `SemgrepYamlChunking` 解析 `rules:`，按 rule id、message、severity、languages、metadata、patterns 切块。 |
| `CheatSheetSeries` | 约 188 个文件，主要是 Markdown | 作为 OWASP Cheat Sheet 文档，用 `MarkdownDocChunking` 按标题层级和语义段落切块。 |
| `ASVS` | 约 513 个文件，含 Markdown、JSON、PDF、docx、图片等 | 当前 `iter_source_files()` 只索引 `.md/.txt/.yaml/.yml/.json/.py/.rst` 等文本后缀，PDF / docx / 图片不会直接入库，除非先转换成文本。 |
| `CVEfixes`、`PrimeVul`、`diversevul` | 小规模代码/数据资料 | 当前没有专门 AST 解析策略，主要通过通用文本/代码后缀策略入库。 |

这里要诚实说：当前不是把所有格式都做了深度解析。结构化处理最完整的是 GitHub Advisory JSON 和 Semgrep YAML；OWASP 类 Markdown 文档按标题和段落处理；PDF、docx、图片类资料目前不在默认文本索引范围内。

## 3. 70 万个 chunk 是怎么切出来的？按文档段落、规则、CVE 条目，还是固定 token 长度？

不是单一固定长度切块，而是“按语料类型先结构化，再做语义长度切分”。

当前主集合实际是 `768870` 个 point，可以说“约 76.9 万 chunk”。切块入口是 `chunks_from_file()` 和 `ChunkingRouter`，它会按文件类型和路径选择策略：

- Advisory JSON：按 `summary`、`details`、`affected`、`references` 等字段切。每个 chunk 前面带 advisory header，包括 ID、aliases、severity、published、modified、package、CWE。
- Semgrep YAML：通常一个 rule 一个 chunk。如果 rule 太长，会把 patterns 拆成多个 part，但每个 part 仍保留 rule header。
- Markdown / RST / TXT / Python：Markdown 先按标题层级拆 section，再用 `split_semantic_text()` 做语义切分；普通文本和代码按通用语义切分。

也就是说，CVE/GHSA 更接近“按公告字段切”，Semgrep 更接近“按规则切”，OWASP 文档更接近“按标题段落切”。固定长度只是在字段或段落过长时作为上限保护。

## 4. chunk 大小和 overlap 怎么设置？为什么这么设置？

入库脚本默认参数是：

```text
--chunk-chars 1800
--overlap-chars 220
```

也就是默认每块约 1800 字符，普通文档相邻块保留约 220 字符 overlap。

这样设置的原因：

- 1800 字符足够容纳一个安全概念、一个 OWASP 小节、一个 Semgrep 规则摘要或 advisory 的一个字段，不会太碎。
- 220 字符 overlap 能保留跨段落的上下文，例如漏洞描述和修复建议刚好跨切分点。
- `split_semantic_text()` 会优先在空行、列表、表格行、换行、英文句号、中文句号处分割，并尽量避免切开 Markdown 代码块。

对结构化数据我做了区别处理：Advisory 的 `details`、`affected` 和 Semgrep 的 patterns 如果过长，内部拆分时 overlap 是 `0`。原因是这些字段本身已经带完整 header，重复 overlap 反而会制造重复证据和重复匹配。

## 5. 你怎么保证 chunk 可追溯？元数据里保存了哪些字段？

每个 chunk 都是 `KnowledgeChunk`，核心字段包括：

- `id`：稳定 id，格式是 `security-kb:<sha1>`。
- `text`：最终检索文本，前面统一加 `TITLE` 和 `SOURCE`。
- `source_path`：本地绝对路径。
- `source_relpath`：相对知识库根目录的路径，用于展示和引用。
- `title`：文档标题、advisory id 或 Semgrep rule id。
- `chunk_index`：同一文件内第几个 chunk。
- `char_start` / `char_end`：原文件字符范围。
- `source_type`：如 `advisory`、`semgrep_rule`、`markdown_doc`、`plain_text`。
- `metadata`：结构化元数据。

稳定 id 来自 `chunk_stable_id(path, start, end, text)`，会把 `CHUNKING_VERSION`、路径、字符范围和文本前 120 字符做 SHA1。这样同一个资料、同一个位置切出来的 chunk 可以稳定追踪；当切块策略升级时，通过 `CHUNKING_VERSION` 避免新旧策略混淆。

不同语料的 metadata 不同：

- Advisory：`advisory_id`、`aliases`、`severity`、`packages`、`cwes`、`field`、`part`、`published/modified` 会进入 header。
- Semgrep：`rule_id`、`severity`、`message`、`languages`、`cwe`、`category`、`technology`、`confidence`、`likelihood`、`impact`。
- Markdown / plain text：`heading`、`filename`、`parent`、`strategy_version` 等。

## 6. CVE、CWE、severity、package、rule id、language 这些元数据在检索时怎么用？

当前有四种使用方式。

第一，进入 chunk 文本 header。比如 advisory chunk 会写入 `ID`、`Aliases`、`Severity`、`Package`、`CWEs`；Semgrep rule 会写入 `Rule ID`、`Languages`、`CWE/OWASP`。这会让 dense 和 sparse 检索都能命中这些关键字段。

第二，进入 Qdrant payload。`KnowledgeHit.metadata` 会带回这些字段，最终进入工具返回、上下文注入和 trace，方便模型引用和开发者复盘。

第三，部分字段支持 payload filter。`SecurityKnowledgeIndex.search()` 当前支持 `source_type`、`severity`、`language` 三类过滤参数。

第四，路由和证据分析会用 metadata 判断命中意图。例如 `_hit_intent_bucket()` 会参考 `source_type`、`field`、`rule_id`、`cwe/cwes`、`category` 等，辅助 evidence gate 判断结果是否集中。

边界要说清楚：

- `security_rag_search` 工具当前只暴露 `query/top_k/min_score`，没有把 `source_type/severity/language` filter 暴露给模型。
- `package/version/CVE/rule_id` 当前主要靠 query exact term、sparse 检索和 header 命中，不是独立 filter 参数。
- Semgrep metadata 里是 `languages` 列表，但 `SecurityKnowledgeIndex._payload_filter()` 现在用的是 `metadata.language` 单数键，所以语言过滤和 Semgrep `languages` 还没有完全打通。

## 7. BGE-M3 为什么适合你的场景？相比 bge-small、text-embedding-3-small、e5-mistral 有什么考虑？

BGE-M3 适合这个场景主要因为四点：

1. 中英混合能力好。我的问题和资料会混合中文面试问题、英文安全术语、CVE/GHSA/CWE、Semgrep rule 和 OWASP 文档。
2. 向量维度和表达能力更强。当前 BGE-M3 集合是 1024 维；旧的 `code_security_kb` 是 512 维，规模也只有约 14.8 万 point。
3. 本地部署可控。安全资料和代码上下文不适合全部发外部 API，BGE-M3 可以通过 FlagEmbedding 本地跑，`.env` 里使用本地模型路径和 `cuda:0`。
4. 生态匹配。BGE-M3 可以和 `bge-reranker-v2-m3` 这类 reranker 配合，便于做召回加重排。

和几个备选相比：

- `bge-small`：速度和资源占用更低，但表达能力弱，更适合小规模或低成本 baseline。
- `text-embedding-3-small`：质量和稳定性不错，但要走外部 API，有成本、网络、数据边界和可复现问题。
- `e5-mistral`：效果可能更强，但模型更重，工程成本和延迟更高。对我当前 76.9 万 chunk 的本地系统，BGE-M3 是质量、成本、部署复杂度之间更均衡的选择。

当前实现还要注意：hybrid 的 sparse provider 在 `.env` 里是 `hash`，不是直接使用 BGE-M3 sparse 输出。也就是说当前主链路是 BGE-M3 dense 加 hash sparse 的混合检索。

## 8. 你说用了 hybrid search，稀疏检索和稠密检索分别解决什么问题？

稠密检索解决“语义相似但词不完全一样”的问题。例如用户问“登录 token 放浏览器本地存储安全吗”，dense 可以召回 JWT、localStorage、XSS、HttpOnly cookie 相关资料。

稀疏检索解决“标识符必须精确命中”的问题。例如：

- `CVE-2021-44228`
- `GHSA-xxxx`
- `CWE-79`
- Maven / npm / PyPI 包名
- Semgrep rule id
- 文件名、配置项、函数名

代码里 `SecurityKnowledgeIndex.search()` 在 hybrid 模式下会同时算 dense vector 和 sparse vector，然后用 Qdrant named vectors 查询 `dense` 和 `sparse` 两路。路由里 `_needs_hybrid()` 会对 CVE/CWE/GHSA、包名、Semgrep、依赖、版本、Dockerfile、Kubernetes、Terraform、GitHub Actions 等精确匹配场景触发 hybrid。

一句话概括：

> dense 负责理解“你在问什么安全问题”，sparse 负责不丢掉“你提到的具体编号和组件名”。

## 9. RRF 是什么？为什么 hybrid search 后还要用 RRF 融合？

RRF 是 Reciprocal Rank Fusion，倒数排名融合。它不直接比较 dense score 和 sparse score 的绝对值，而是看文档在不同召回列表里的排名，再把排名转成分数融合。

需要 RRF 的原因是 dense 和 sparse 的分数空间不同：

- dense score 更像语义相似度。
- sparse score 更受关键词、编号和词频影响。

如果直接把两者 score 相加，很容易因为量纲不一致导致一路检索压倒另一路。RRF 更稳健：一个文档如果在 dense 和 sparse 两路都排得靠前，就会被推上去；如果只在一路很好，也仍然有机会进入候选。

实现上在 `SecurityKnowledgeIndex._query_points_hybrid_vectors()` 里用的是 Qdrant 的：

```text
prefetch dense
prefetch sparse
FusionQuery(fusion=Fusion.RRF)
```

所以这里的 hybrid 不是简单拼接结果，而是由 Qdrant 在服务端做 RRF 融合。

## 10. reranker 放在流程的哪一步？候选数量是多少？重排后保留多少条证据？

reranker 放在第一阶段召回之后、最终返回之前。

在 `SecurityKnowledgeIndex.search()` 里：

1. 先用 dense 或 hybrid 召回候选。
2. `candidate_limit = max(top_k, reranker_candidates)`。
3. 如果 `use_reranker=True` 且配置了 reranker，就调用 `_rerank_hits(query, hits)`。
4. 最后返回 `hits[:top_k]`。

当前 `.env` 里 `SECURITY_RAG_RERANKER_CANDIDATES=12`，也就是说默认最多拿 12 条候选给 reranker 排序。`top_k` 在 router 默认是 `5`，eval 里常用 `10`，工具 `security_rag_search` 默认 `5` 且最大 `10`。

路由层不是所有请求都强制 rerank，而是分层使用：

- 高置信 dense / hybrid 直接通过 evidence gate 时，不一定 rerank。
- medium evidence 进入 expansion 时，会强制 rerank。
- low evidence 且候选数量足够时，会走 reranker fallback。
- complex / multihop 问题拆解后会合并并进入 `decompose_rerank`。

这样做是为了控制延迟。评测里开启 LLM rewrite / rerank 后 P@1、MRR 有提升，但 p50 延迟从约 `90ms` 增到约 `4341ms`，所以不能无脑全开。

## 11. 你怎么判断一个用户问题需要注入安全知识？查询路由规则是什么？

路由分两层：基础路由 `route()` 和带证据的路由 `route_with_retrieval()`。

基础路由流程：

1. 空 query 直接不检索。
2. 先跑负向 block patterns，包括未授权攻击、伪造审计、隐私/密钥、采购报价、未来预测、证据不足、普通非安全问题。
3. 如果命中安全关键词，比如 `cve`、`cwe`、`jwt`、`xss`、`ssrf`、`路径穿越`、`越权`、`文件上传`，直接判定需要 RAG，confidence `0.90`。
4. 否则用 embedding 和 `DEFAULT_SECURITY_INTENTS` 做相似度匹配，高于 high threshold 就检索。
5. 低于 low threshold 但没有负向 gate 命中时，当前代码仍会尝试 fast dense retrieval，而不是直接拒绝。这是为了降低漏召回。
6. 中间模糊区可以交给 LLM classifier。

带证据路由 `route_with_retrieval()` 会继续：

- 先做 fast dense。
- 分析 top score、score gap、source concentration、intent concentration。
- 高置信通过 direct evidence gate 直接返回。
- 精确标识符问题触发 hybrid。
- 中低置信走 rewrite、rerank 或 LLM fallback。
- 复杂问题触发 decompose 多子查询。
- 没有足够证据时 abstain 或 ask clarification。

当前自动注入发生在 `ContextBuilder._build_security_knowledge_block()`，但 `.env` 默认关闭。工具侧则通过 system guidance 提醒模型：涉及 CVE/CWE/GHSA、依赖、认证授权、注入、XSS、SSRF、token、secrets、文件上传、路径穿越、安全编码时，应先调用 `security_rag_search`。

## 12. 如果路由误判，不该检索却检索了，会有什么问题？

误检索主要有四个问题。

第一，浪费 token。`security_knowledge` 默认上下文预算是 `3000` 字符，误注入会挤占历史、工作记忆或当前任务上下文。

第二，增加延迟。dense 检索本身还能接受，但 hybrid、rewrite、rerank 或 LLM rewrite 会显著增加耗时。

第三，干扰模型注意力。比如用户问普通 Python dataclass，误注入一堆安全资料，模型可能把普通问题回答成安全审计。

第四，可能诱导错误答案。检索到的资料如果和问题无关，模型可能强行关联，造成“有引用但引用不相关”。

评估上，router-only eval 会统计 false positive rate。当前 250 条用户问题测试集里，`always_use_rag` baseline 的 false positive rate 是 `1.0`，说明所有非安全问题都被检索；router 在该测试集上 FP 为 `0`。但这个 1.0 只能说明该测试集表现，不能承诺真实线上永远没有误判。

## 13. 如果应该检索但没检索，会导致什么风险？怎么评估这种漏召回？

漏检索比误检索更危险，尤其在代码安全场景：

- 模型可能凭旧知识回答已经变化的依赖漏洞。
- 可能编造不存在的 CVE / GHSA。
- 可能给出过时或不适用于该生态的修复版本。
- 对 Semgrep 规则、CWE 映射、OWASP 建议这类问题，可能失去来源依据。

我用两层指标评估漏召回。

第一层是路由召回。`scripts/eval_security_router_only.py` 会计算 `recall`、`false_negative_rate`。当前 250 条用户问题测试集里，`router_embedding_only` 的 route recall 是 `1.0`，FN 为 `0`。

第二层是检索召回。`scripts/eval_security_rag_v2.py` 会在 should_use_rag 的正例上计算 `recall_at_5`、`recall_at_10`、`hit_rate`、`MRR`。例如 2026-06-24 的 matrix 评测里，`precise_dense_router_on_rewriter_on_reranker_off` 在 250 条用户问题集上 `recall@10=0.9468`、`hit_rate=0.9468`。

代码层面为了降低漏召回，当前 low similarity 但没有负向 gate 的请求不会直接 skip，而是进入 fast dense；精确标识符触发 hybrid；中低置信会 rewrite / rerank；复杂问题会 decompose。

## 14. 你的 RAG 系统如何处理代码片段？是直接把代码作为 query，还是先让模型提取风险点？

当前没有单独做 AST 风险抽取器，也没有把用户代码先跑静态分析再检索。主要有两种方式：

1. 直接把用户问题或代码片段作为 query 进入 router / search。
2. 由模型或 query rewrite 把代码片段中的风险点提取成检索 query，例如 `SQL injection raw query tenant id`、`path traversal file upload zip slip`、`JWT localStorage XSS`。

`_llm_rewrite_prompt()` 里明确要求保留用户提到的语言、框架、组件名、漏洞类型、CVE/CWE/GHSA、文件/配置关键词，同时禁止引入原问题没有的具体 CVE、GHSA、包名、框架。

所以面试里可以这样说：

> 目前处理代码片段更偏“检索 query 改写”，不是完整静态分析。对于 coding agent 场景，模型会先理解代码风险点，再调用 `security_rag_search` 查本地证据。后续我会把 Semgrep / AST / 依赖解析接到 query 构建前面，让代码片段先变成结构化风险特征。

## 15. 针对一个 Java 依赖漏洞问题，检索流程怎么走？会用 package / version / CVE 元数据过滤吗？

以“Spring 某个 Maven 依赖版本是否受某 CVE 影响”为例，流程大致是：

1. 用户问题里出现 `CVE/GHSA`、`依赖`、`包`、`版本`、`Maven/Gradle` 等词，router 会判定需要安全知识。
2. pre-dense rewrite 会保留包名、版本、CVE/GHSA，同时补充 `dependency vulnerability remediation package upgrade advisory` 之类安全术语。
3. 先跑 dense 检索，召回语义相近的 advisory。
4. `_needs_hybrid()` 会因为 CVE/GHSA、package/version 类标识符触发 hybrid exact 检索。
5. Advisory chunk 的 header 和 metadata 里有 `Aliases`、`Package`、`Affected packages and ranges`，稀疏检索更容易命中具体包名和编号。
6. 中低置信时可能走 reranker，把更相关的 advisory 排到前面。
7. 最终回答时必须检查返回 chunk 里的 `affected` range 和 references，不能只看标题。

当前边界：

- `SecurityKnowledgeIndex.search()` 还没有 `package`、`version`、`cve` 的一等 filter 参数。
- `packages` 和 `aliases` 已经在 metadata 和 chunk header 里，但检索主要依赖 query exact term、sparse 检索和重排。
- 版本区间判断也不是独立求解器。模型需要基于 advisory 的 affected ranges 作解释，严谨场景应接入生态特定版本比较器，例如 Maven semver/range parser。

## 16. 针对一个 Semgrep 规则，如何把规则内容转成适合检索的 chunk？

Semgrep 规则由 `SemgrepYamlChunking` 处理。

流程是：

1. 只处理路径包含 `semgrep-rules` 且后缀是 `.yaml/.yml` 的文件。
2. 用 `yaml.safe_load_all()` 读取 YAML docs。
3. 遍历 `rules:` 列表。
4. 每个 rule 取 `id` 作为 title。
5. 从 rule 和 metadata 中抽取 `severity`、`message`、`languages`、`cwe/owasp`、`category`、`technology`、`confidence`、`likelihood`、`impact`。
6. 生成统一 header：

```text
Semgrep Rule
Rule ID: ...
Severity: ...
Languages: ...
CWE/OWASP: ...
Category: ...
Message: ...
```

7. body 中保留 `message/severity/languages/metadata/patterns` 等 JSON 化内容。
8. 如果 rule 太长，先保留非 patterns 的 prefix，再把 patterns 拆成多个 part。每个 part 都保留 rule header。

这样做的好处是，用户按 rule id、语言、CWE、message 或 pattern 关键词都可能召回同一条规则；同时 chunk 里有足够上下文解释规则意图和修复方向。

## 17. 你怎么评估检索质量？P@1、Recall@10、MRR、Hit Rate 有没有测？

有。`scripts/eval_security_rag_v2.py` 会计算：

- route：`route_accuracy`、`route_precision`、`route_recall`。
- ranking：`precision_at_1/3/5`、`recall_at_5/10`、`MRR`、`NDCG@5/10`、`Hit Rate`。
- latency：`p50/p95/p99`。

测试集有两类：

- `benchmarks/security_rag_user_questions_testset.jsonl`：250 条，正例 188，负例 62，覆盖 risk judgement、remediation、multi-hop、concept、practical debugging、comparison、unanswerable、distractor 等。
- `benchmarks/security_rag_testset.jsonl`：100 条，正例 90，负例 10，覆盖 vulnerability principle、rule explanation、dependency vulnerability、language difference、conflicting evidence 等。

当前几个可引用结果：

- 2026-06-24 matrix，250 条用户问题集，`precise_dense_router_on_rewriter_on_reranker_off`：`P@1=0.7021`、`Recall@10=0.9468`、`MRR=0.7843`、`Hit Rate=0.9468`、p50 约 `91.9ms`、p95 约 `158.4ms`。
- 2026-06-25 tiered rewrite 对比，75 条子集：关闭 rewrite 时 `P@1=0.6727`、`Recall@10=0.9636`、`MRR=0.7912`；开启 rewrite 时 `P@1=0.7091`、`Recall@10=0.9818`、`MRR=0.8162`，但 p50 延迟从约 `90ms` 增到约 `4341ms`。

这里要补一句边界：没有 `relevant_chunk_ids` 的样本会用 expected terms / source hints 做启发式相关性判断，所以这些指标适合比较 pipeline，不应包装成严格学术 benchmark。

## 18. 你简历里写“将检索决策、命中文档和注入内容写入 trace”，这个 trace 对调试 RAG 有什么帮助？

trace 能把 RAG 失败拆成可定位的问题。

runtime trace 事件包括：

- `security.rag.started`
- `security.rag.search.completed`
- `security.rag.search.failed`
- `security.rag.completed`
- `security.rag.failed`

`runtime/trace/rag.py` 里会记录：

- 原始 query 和 rewritten query。
- router decision：`use_rag`、`route`、`confidence`、`reason`。
- action：`direct`、`dense`、`hybrid`、`expansion`、`reranker`、`abstain` 等。
- 每次 search 的 stage、retrieval mode、是否 reranker、hit count、top score、score gap、candidate count、final count、latency。
- final hits 的 id、score、source、title、chunk_index、metadata。

独立 RAG trace 还会写到 `~/.claude/rag_traces/rag_traces_YYYY-MM-DD.jsonl`，字段包括 `trace_id`、`timestamp`、`source`、`query`、`rewritten_query`、`router_decision`、`final_hits`、`latency_ms`、`error`。

这对调试很有用：

- 如果没注入证据，看是 router 判了 no_rag，还是 search 没结果。
- 如果命中不准，看 rewritten query 有没有改坏，dense 和 hybrid 哪个阶段失败。
- 如果排序不对，看 top hit、candidate count、reranker 是否开启。
- 如果延迟高，看是 embedding、Qdrant、reranker 还是 LLM rewrite 慢。
- 如果模型答错，看它当时实际看到的 source 和 metadata，而不是事后猜。

## 19. RAG 生成修复建议时，如何避免模型编造不存在的 CVE 或错误修复方案？

我做了几层约束。

第一，先查本地证据。工具描述和 runtime guidance 要求安全问题先调用 `security_rag_search`，自动上下文模式也会把 `<security_knowledge>` 注入到模型前。

第二，检索内容带来源。注入 block 里有 source path、title、score、metadata，并提示模型 “Prefer cited source paths when answering”。

第三，query rewrite 不能编造标识符。`_llm_rewrite_prompt()` 明确要求不要引入原问题没有的具体 CVE/GHSA/包名/框架，要保留原始语言、框架、组件名、漏洞类型、CVE/CWE/GHSA。

第四，没有足够证据时不强答。`route_with_retrieval()` 如果检索证据不足，会 `abstain` 或要求补充上下文。负向 gate 里也有“证据不足”场景，例如没有代码、没有日志、没有架构图、只凭扫描标题等。

第五，通过 trace 和 eval 复盘。如果发现某类问题经常生成错误修复方案，可以看它当时命中的 advisory、rewrite 和 rerank 结果，调整切块、路由或评测样本。

当前边界：

- 还没有独立的 CVE 编号 validator 或 package version range solver。
- 对依赖修复版本的判断主要依赖 advisory 证据和模型解释，严谨生产场景应增加 ecosystem-specific version parser 和 advisory schema 校验。

面试里可以这样回答：

> 我不是让模型自由生成 CVE 或修复版本，而是要求它先拿到本地证据，回答时引用 source；query rewrite 也禁止引入原问题没有的具体 CVE/包名。没有证据时走 abstain 或追问。当前短板是还没有把 CVE validator 和版本区间求解器做成硬规则。

## 20. 如果检索到多条安全资料互相冲突，比如 CVSS 严重性不同，你怎么处理？

当前系统会保留冲突证据，而不是在检索阶段强行合并。

具体做法：

- chunk 里保留 source、title、metadata、severity、published、modified、references 等信息。
- trace 里记录 final hits，方便看到冲突来自哪些来源。
- complex / multihop / conflict 类问题会被 `_is_complex_or_multihop()` 识别，触发拆解和多路检索，尽量拿到多个证据。
- 最终回答应显式说明“不同来源的严重性不一致”，并分别引用来源、更新时间、生态和适用范围。

但要诚实说：当前代码还没有一个自动 authority resolver，比如“官方 vendor advisory 优先于第三方规则”“modified 更新者优先”“CVSS v3.1 优先于旧评分”这样的硬编码决策器。

我现在的处理原则是：

1. 不把冲突资料压成一个结论。
2. 优先报告可验证字段，例如受影响版本、修复版本、漏洞条件。
3. 对严重性冲突，用“来源 A 标为 HIGH，来源 B 标为 CRITICAL，建议按更保守级别评估并结合暴露面确认”。
4. 如果缺关键上下文，比如具体版本、暴露面、可达性，就要求补充，而不是断言。

后续最值得补的是 `source authority + freshness + ecosystem version range` 三个维度的冲突仲裁器，把现在偏模型层的判断下沉成可测试的规则。

## 21. 最容易被追问的工程短板

这部分不是题目原文，但面试时很容易被继续追问，可以主动收束：

- 自动上下文注入能力已经实现，但当前 `.env` 默认关闭；现在主要通过 `security_rag_search` 工具使用。
- `security_rag_search` 工具还没有暴露 `source_type/severity/language/package/cve` 等过滤参数。
- `language` filter 和 Semgrep 的 `languages` 列表还没有完全打通。
- Java / Maven / npm / PyPI 版本范围判断还没有做成独立求解器。
- PDF / docx / 图片类安全资料没有默认入库，除非先转换成文本。
- 冲突资料目前保留证据并由回答层解释，还没有 authority resolver。

可以用一句话结尾：

> 我这套安全 RAG 已经把本地资料入库、结构化切块、hybrid 检索、路由决策、重排、trace 和评测串起来了；下一步最想补的是 metadata filter 外露、版本区间求解和冲突仲裁，把“能查到证据”进一步升级成“能做更硬的安全判断”。
