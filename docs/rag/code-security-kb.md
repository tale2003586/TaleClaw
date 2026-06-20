# 代码安全 RAG 知识库

这是一个最小可用版 RAG：

- 知识源：`/home/tale/kaggle/code-security-kb`
- 向量库：Qdrant
- collection：`code_security_kb`
- 默认 embedding：`fastembed` + `BAAI/bge-small-zh-v1.5`

## 启动 Qdrant

```bash
docker compose up -d qdrant
```

## 配置

`.env` 中的关键配置：

```env
SECURITY_RAG_SOURCE_ROOT=/home/tale/kaggle/code-security-kb
SECURITY_RAG_COLLECTION=code_security_kb
SECURITY_RAG_QDRANT_URL=http://127.0.0.1:6333
SECURITY_RAG_EMBEDDING_PROVIDER=fastembed
SECURITY_RAG_EMBEDDING_MODEL=BAAI/bge-small-zh-v1.5
```

如果只是本地快速烟测，不想下载 embedding 模型，可以改成：

```env
SECURITY_RAG_EMBEDDING_PROVIDER=hash
```

## 构建索引

第一次建议先小规模 smoke：

```bash
/home/tale/anaconda3/bin/python scripts/ingest_security_kb.py \
  --source /home/tale/kaggle/code-security-kb \
  --collection code_security_kb_smoke \
  --recreate \
  --limit-files 20
```

确认可搜索后，构建完整索引：

```bash
/home/tale/anaconda3/bin/python scripts/ingest_security_kb.py \
  --source /home/tale/kaggle/code-security-kb \
  --collection code_security_kb \
  --recreate
```

如果完整源库太大，可以先限制文件大小或文件数：

```bash
/home/tale/anaconda3/bin/python scripts/ingest_security_kb.py \
  --max-file-bytes 500000 \
  --limit-files 1000
```

## 查询

```bash
/home/tale/anaconda3/bin/python scripts/search_security_kb.py \
  "SQL injection prevention prepared statements"
```

示例：

```bash
/home/tale/anaconda3/bin/python scripts/search_security_kb.py \
  "JWT token storage XSS CSRF best practices" \
  --top-k 5
```

输出包含：

- `score`：向量相似度
- `source`：原始文件相对路径
- `title`：Markdown 标题或文件名
- 文本片段预览

## 切片策略

当前索引构建不再把所有文件都当纯文本硬切，而是按文件类型路由：

```text
chunks_from_file
  ├─ JSON advisory：按 GHSA/CVE 字段切
  ├─ Semgrep YAML：按 rule 边界切
  ├─ Markdown/RST：按标题路径切，再做语义边界切分
  └─ 其他文本：按段落/行/句号边界切分
```

### JSON advisory

针对 GitHub Advisory / CVE / GHSA JSON：

- `summary` 独立成 chunk。
- `details` / `description` 按段落边界切。
- `affected` 独立成 chunk。
- `references` 独立成 chunk。
- 每个 chunk 都带完整 header：`ID`、`Aliases`、`Severity`、`Published`、`Package`、`CWEs`。
- payload metadata 会写入：`corpus_type=advisory`、`advisory_id`、`aliases`、`severity`、`packages`、`cwes`、`field`。

这样即使只召回其中一个详情片段，也能知道它属于哪个 CVE/GHSA、影响什么包、对应哪些 CWE。

### Semgrep YAML

针对 `rules:` 格式的 Semgrep 规则文件：

- 一个 rule 默认生成一个 chunk。
- 如果 patterns 太大，会拆成多个 part，但每个 part 都带完整 rule header。
- chunk header 包含：`Rule ID`、`Severity`、`Languages`、`CWE/OWASP`、`Category`、`Message`。
- payload metadata 会写入：`corpus_type=semgrep_rule`、`rule_id`、`severity`、`languages`、`cwe`、`category`、`technology`。

这样检索结果不会再出现“半条规则、无头无尾”的片段。

### Markdown/RST

Markdown/RST 会保留标题层级路径：

```text
Web Security > XSS > HTML Context
```

切分时会尽量按段落、列表、表格行、句号边界切；遇到代码块时避免在 ``` fenced block 中间断开。

## RAG 触发路由

当前还有一个最小可用的 `RetrievalRouter`，用于判断用户问题是否应该查询安全知识库。

判断流程：

```text
用户 query
  ↓
关键词规则强命中？
  ├─ 是：直接查 security RAG
  └─ 否：
       ↓
     embedding 语义相似度
       ├─ >= high_threshold：直接查
       ├─ <= low_threshold：不查
       └─ 中间段：需要 LLM 分类
```

配置项：

```env
SECURITY_RAG_ROUTE_HIGH_THRESHOLD=0.72
SECURITY_RAG_ROUTE_LOW_THRESHOLD=0.45
SECURITY_RAG_ROUTE_LLM_ACCEPT_THRESHOLD=0.60
SECURITY_RAG_ROUTE_TOP_K=5
SECURITY_RAG_ROUTE_MIN_SCORE=0.0
```

只看路由决策：

```bash
/home/tale/anaconda3/bin/python scripts/route_security_rag.py \
  "这个接口能不能被人绕过权限？"
```

如果落在中间段，并希望真的调用 LLM 分类：

```bash
/home/tale/anaconda3/bin/python scripts/route_security_rag.py \
  "这个参数这样传安全吗？" \
  --llm
```

路由后如果需要 RAG，就立刻检索：

```bash
/home/tale/anaconda3/bin/python scripts/security_rag_ask.py \
  "这个接口能不能被人绕过权限？"
```

Agent 工具也已经注册为：

```text
security_rag_search
```

它不是 always-on 工具。模型需要时可以先调用：

```text
tool_search select:security_rag_search
```

然后再调用 `security_rag_search` 检索本地安全知识库。

同时，主 agent 的上下文构造阶段已经接入自动安全 RAG。注意：自动路由只在每个用户 turn 的第一次模型调用前运行一次。

```text
当前用户请求
  ↓
RetrievalRouter
  ↓
需要安全 RAG？
  ├─ 否：不注入
  └─ 是：查询 Qdrant，把结果作为 <security_knowledge> 注入 context_frame
  ↓
后续 reasoning step 如果还需要安全资料
  ↓
模型调用 security_rag_search 工具主动检索
```

开关和预算：

```env
SECURITY_RAG_AUTO_CONTEXT_ENABLED=1
CONTEXT_SECURITY_KNOWLEDGE_BUDGET=4000
CONTEXT_SECURITY_KNOWLEDGE_FLOOR=800
```

在 trace 的 `context.build.completed.payload.context_report.sections.security_knowledge`
里可以看到：

- `decision`：路由结果，包括 `route`、`confidence`、`query`
- `hit_count`：命中数量
- `hits`：来源路径、标题、分数、chunk index
- `rendered_chars` / `truncated`：最终注入上下文的长度和裁剪状态

返回的 `route` 字段说明：

- `keyword`：显式安全关键词命中，直接查。
- `embedding_high`：没有关键词，但语义相似度高，直接查。
- `embedding_low`：语义相似度低，不查。
- `embedding_middle`：语义相似度处于中间段，需要 LLM 判断。
- `llm`：已由 LLM 分类器判断。

## 当前边界

第一版只做“检索”：

- 支持 Markdown / YAML / JSON / TXT / Python 文本文件。
- JSON advisory / Semgrep YAML / Markdown 已经有专门的结构化切片策略。
- 检索结果已经可以在主 agent 上下文构造阶段自动注入。
- 还没有做 BM25/关键词混合召回。
- 还没有做引用答案生成。

后续可以加：

- Web UI 查询页。
- 和 coding/security review persona 打通。
- 混合检索：向量召回 + 关键词召回 + rerank。
- 将检索结果作为 `<security_knowledge>` section 注入上下文。
