# 代码安全 RAG 知识库与检索路由

这篇文档讲当前代码安全 RAG 是怎么构建、索引、检索，并接入 agent 的。

## 这层解决什么问题

项目里有两类向量检索：

- 历史会话向量库：存完整对话 turn，服务记忆召回。
- 代码安全 RAG 知识库：存 `/home/tale/kaggle/code-security-kb` 这类安全资料，服务安全问答和 coding 安全分析。

两者不是同一个 collection，也不是同一种数据生命周期。

代码安全 RAG 的核心实现位于：

- `knowledge/security_rag.py`
- `knowledge/chunking/`
- `knowledge/incremental.py`
- `knowledge/caching.py`
- `knowledge/reranker.py`
- `knowledge/tracing.py`
- `retrieval/security_router.py`
- `plugins/security_rag/plugin.py`
- `scripts/ingest_security_kb.py`
- `scripts/search_security_kb.py`
- `scripts/security_rag_ask.py`
- `scripts/eval_security_rag.py`

## 数据源

默认数据源由环境变量指定：

```text
SECURITY_RAG_SOURCE_ROOT=/home/tale/kaggle/code-security-kb
```

`iter_source_files()` 会遍历常见文本知识文件，包括 markdown、rst、txt、yaml、json、python 等。文件会先被切片成 `KnowledgeChunk`，再写入 Qdrant。

## ChunkingRouter

当前切片不再是所有文件统一字符硬切，而是按文件类型和路径路由到不同 strategy。

核心协议在 `knowledge/chunking/base.py`：

```python
class ChunkingStrategy(Protocol):
    def supports(self, path: Path, text: str) -> bool: ...
    def chunk(self, path: Path, text: str) -> list[KnowledgeChunk]: ...
```

默认 router 包含：

- `JsonAdvisoryChunking`
- `SemgrepYamlChunking`
- `MarkdownDocChunking`

## JSON Advisory 切片

实现位于：

```text
knowledge/chunking/advisory.py
```

它主要处理 GitHub Advisory / CVE / GHSA 这类 JSON。

切片特点：

- 一个 advisory 会先解析成结构化对象。
- 每个 chunk 都带 advisory header，例如 GHSA ID、CVE aliases、severity、affected packages、CWE。
- summary、details、affected、references 等字段按语义切片。
- chunk metadata 会写入 `advisory_id`、`aliases`、`severity`、`packages`、`cwes` 等字段。

这样单个 chunk 被召回时也能知道“这是哪个漏洞、影响什么包、严重度是什么”。

## Semgrep YAML Rule 切片

实现位于：

```text
knowledge/chunking/semgrep.py
```

它主要处理 Semgrep rule 文件。

切片特点：

- 解析 YAML 文档。
- 按 rule 边界切片，而不是按字符硬切。
- 每个 chunk 保留 rule id、message、severity、languages、patterns、metadata。
- chunk metadata 会写入 `rule_id`、`severity`、`languages`、`cwe` 等字段。

这样检索结果不会只返回一段无头无尾的 pattern。

## Markdown / 文本文档切片

实现位于：

```text
knowledge/chunking/markdown.py
```

它负责 markdown、rst、txt 以及一些普通文本。

切片特点：

- 按 heading section 初步分段。
- 记录 heading path。
- 避免在代码块中间切断。
- 超长 section 再按语义边界切分。
- 使用 semantic overlap，而不是固定从任意字符位置截断。

## Embedding Provider

embedding provider 在 `knowledge/security_rag.py` 中由环境变量构造。

当前支持：

- `hash`：本地烟测用，不需要下载模型。
- `fastembed`：使用 fastembed 支持的模型。
- `bge_m3` / `flagembedding`：使用 FlagEmbedding 的 BGE-M3。

常见配置：

```text
SECURITY_RAG_EMBEDDING_PROVIDER=bge_m3
SECURITY_RAG_EMBEDDING_MODEL=/home/tale/kaggle/mytry/models/bge-m3
SECURITY_RAG_EMBEDDING_DEVICE=cuda:0
```

如果本地已经下载模型，应把 `SECURITY_RAG_EMBEDDING_MODEL` 指向本地目录，避免启动 agent 或 ingest 时重新访问 Hugging Face。

## Qdrant 索引

代码安全知识库使用 Qdrant collection：

```text
SECURITY_RAG_COLLECTION=code_security_kb_bge_m3
SECURITY_RAG_QDRANT_URL=http://127.0.0.1:6333
```

`SecurityKnowledgeIndex` 负责：

- 创建 collection。
- upsert chunk。
- 删除 source file 对应旧 chunk。
- dense search。
- hybrid search。
- payload 返回和 score 归一。

如果 collection 是旧 dense-only collection，它只能做 dense search。要真正使用 hybrid，需要 collection schema 有 sparse vector 字段，并重新 upsert 带 sparse 向量的点。

## Hybrid Search

当前检索可以走 dense-only，也可以走 hybrid。

hybrid 的核心思想是：

- dense vector 负责语义相似。
- sparse vector 负责关键词、CVE、CWE、函数名、rule id 等精确线索。
- 两路结果用 RRF 或类似方式融合。

安全知识库中很多查询带有强标识符，例如：

- `CVE-2023-...`
- `GHSA-...`
- `CWE-89`
- `SQL injection`
- `Semgrep rule id`

这类场景只靠 dense embedding 容易漂移，所以 hybrid 对安全资料更合适。

## 检索缓存和 Reranker

`build_security_index_from_env()` 默认会在 `SecurityKnowledgeIndex` 外面包一层 `CachedSecurityIndex`：

```text
SECURITY_RAG_CACHE_ENABLED=1
SECURITY_RAG_CACHE_MAX_SIZE=512
SECURITY_RAG_CACHE_TTL_SECONDS=3600
```

这是进程内 TTL cache，key 由 query 和检索参数生成。cache 命中时仍会通过 trace callback 标记 `cache_hit`，但它不是持久缓存，进程重启后会清空。

如果开启：

```text
SECURITY_RAG_RERANKER_ENABLED=1
```

系统会从 `knowledge/reranker.py` 构造 `RerankerProvider`，默认模型是 `BAAI/bge-reranker-v2-m3`。reranker 会先对更多候选打分，再返回 top_k；它是可选增强，不影响 dense/hybrid 检索主链路。

## Incremental Ingest

增量索引状态由 `knowledge/incremental.py` 维护。

它会记录文件的：

- path
- mtime
- size
- digest / chunk ids

增量 ingest 时：

- 新文件：切片并写入。
- 修改文件：删除旧 chunks，再写入新 chunks。
- 删除文件：删除 collection 中对应 source_path 的 chunks。

如果使用 `--recreate`，会重建 collection，之前索引全部清空。

## 检索路由

`retrieval/security_router.py` 决定“当前用户请求是否需要安全 RAG”。

路由过程是三层：

1. keyword 命中：直接查。
2. keyword 未命中：把 query 与安全意图样例做 embedding 相似度。
3. 高于 high threshold 直接查，低于 low threshold 不查，中间段可交给 LLM classifier。

相关环境变量：

```text
SECURITY_RAG_ROUTE_HIGH_THRESHOLD=0.72
SECURITY_RAG_ROUTE_LOW_THRESHOLD=0.45
SECURITY_RAG_ROUTE_LLM_ACCEPT_THRESHOLD=0.60
```

LLM classifier 是辅助判断，不替代检索本身。它只在中间灰区使用，避免所有请求都额外调用模型。

## Agent 接入方式

agent 有两种使用 RAG 的路径。

第一种是自动上下文：

- `ContextBuilder` 在当前用户请求进入模型前调用 `SecurityRetrievalRouter`。
- 如果判定需要，自动检索并注入 `<security_knowledge>`。
- `Pipeline` 保证每个用户 turn 只自动注入一次。

第二种是工具：

- `plugins/security_rag/plugin.py` 提供 `security_rag_search`。
- 如果后续 reasoning step 还需要更多安全知识，模型应显式调用该工具。

这个边界很重要：自动检索只负责第一次判断；后续深入检索交给工具，避免每个 step 重复 RAG。

## Trace

RAG 有两类 trace。

主 run trace 中会记录：

- 自动安全 RAG 是否注入。
- route decision。
- 检索命中数量。
- `security_rag_search` 工具调用。

RAG 独立 trace 由 `knowledge/tracing.py` 写入，默认目录来自：

```text
SECURITY_RAG_TRACE_DIR
```

它会记录 chunk/search/router 的耗时和命中信息，方便单独调 RAG 质量。

## CLI

常用命令：

```bash
python scripts/ingest_security_kb.py \
  --source /home/tale/kaggle/code-security-kb \
  --collection code_security_kb_bge_m3
```

```bash
python scripts/search_security_kb.py \
  "SQL injection prevention prepared statements"
```

```bash
python scripts/eval_security_rag.py \
  --dataset security_rag_eval.jsonl
```

`security_rag_ask.py` 则用于把检索结果交给模型生成回答，适合端到端调试。

## 当前边界

当前安全 RAG 已经支持结构化切片、Qdrant、BGE-M3、hybrid 检索、路由和 agent 工具接入，但仍有边界：

- hybrid 需要 collection schema 支持 sparse vectors，旧 collection 不能自动变成 hybrid。
- cache 是进程内 TTL cache，不是跨进程共享缓存。
- reranker 是可选增强，不是强依赖。
- RAG 只覆盖导入的安全知识源，不等于互联网搜索。
- 自动上下文每个用户 turn 只触发一次，后续需要模型主动调用工具。
- 检索质量需要通过 `scripts/eval_security_rag.py` 的测试集持续评估。

## 总结

代码安全 RAG 的核心是把安全资料变成可检索的结构化知识，而不是把文档直接塞给模型。

切片层保证 chunk 可读且可追溯，embedding/Qdrant 层负责召回，router 决定什么时候该查，plugin 和 ContextBuilder 负责把结果接进 agent。这个设计让 RAG 能既用于自动补充上下文，也能作为工具被 agent 主动调用。
