# Security Review RAG Persona Plan

## 目标

给 taleclaw 增加一个可演示、可解释、可引用来源的安全审查能力：

```text
Coding agent 写代码或修改代码
-> Security Review persona 检查代码风险
-> 检索 OWASP / CWE 等安全知识库
-> 输出问题、依据、引用来源和修复建议
```

这个方向适合当前项目，因为它和 coding agent 天然相关，不需要额外解释为什么 agent 要懂这个领域。安全知识库也有权威开源来源，适合展示 RAG、persona 隔离、工具调用、引用溯源和工程闭环。

## MVP 范围

第一版只做 3 个知识源：

| 数据源 | 作用 | 获取方式 |
| --- | --- | --- |
| OWASP CheatSheetSeries | 安全实践核心知识库，覆盖认证、输入校验、密码存储、加密、日志等主题 | GitHub 仓库，读取 `cheatsheets/*.md` |
| OWASP Top10 | Web 应用风险归类，适合做审查报告的风险分类 | GitHub 仓库 |
| CWE | 标准弱点编号，例如 CWE-89、CWE-22、CWE-798 | MITRE 官方 CSV/XML 下载 |

暂时不把 CVE 作为第一版核心。CVE 更适合做漏洞情报和依赖风险，不适合作为代码审查 RAG 的起点。

## 推荐数据源

### OWASP CheatSheetSeries

链接：

```text
https://github.com/OWASP/CheatSheetSeries
```

推荐入库内容：

```text
cheatsheets/*.md
```

特点：

- Markdown 文件，结构清晰。
- 主题非常贴近开发者安全实践。
- 适合按标题切 chunk。
- 许可证为 CC-BY-SA-4.0，需要保留来源和引用。

### OWASP Top10

链接：

```text
https://github.com/OWASP/Top10
```

推荐入库内容：

```text
2025/
2021/
```

用途：

- 把审查结果映射到 OWASP Top 10 风险分类。
- 输出更容易被面试官理解的报告结构。

### CWE

链接：

```text
https://cwe.mitre.org/data/downloads
```

推荐入库格式：

```text
CSV.zip 或 XML.zip
```

用途：

- 给问题补充标准弱点编号。
- 例如：
  - SQL 注入：CWE-89
  - 路径穿越：CWE-22
  - 硬编码凭证：CWE-798
  - 明文存储敏感信息：CWE-312

## 不建议第一版接入的数据源

### CVEProject/cvelistV5

链接：

```text
https://github.com/CVEProject/cvelistV5
```

原因：

- 数据量大。
- 更适合“某个产品/依赖是否有已知漏洞”的场景。
- 和静态代码审查的直接关系较弱。

建议放到第二阶段，用于依赖漏洞查询。

### Common Crawl

不建议使用。数据太大、太脏、版权和质量控制都不适合作为项目演示的第一批安全知识库。

## 推荐目录结构

```text
knowledge/
  security/
    sources.yml
    raw/
      owasp-cheatsheets/
      owasp-top10/
      cwe/
    processed/
      chunks.jsonl
      cwe_records.jsonl
    index_meta.json

rag/
  markdown_chunker.py
  security_ingest.py
  security_retriever.py
  vector_store.py

plugins/
  security_review/
    __init__.py
    plugin.py

docs/
  SECURITY_REVIEW_RAG_PERSONA_PLAN.md
```

如果希望保持项目目录更收敛，也可以先把 `rag/` 改成 `knowledge/` 内部模块。第一版重点是跑通闭环，不必过早抽象太多。

## 数据下载流程

```bash
mkdir -p knowledge/security/raw

git clone --depth 1 https://github.com/OWASP/CheatSheetSeries.git knowledge/security/raw/owasp-cheatsheets
git clone --depth 1 https://github.com/OWASP/Top10.git knowledge/security/raw/owasp-top10
```

CWE 从官方页面下载：

```text
https://cwe.mitre.org/data/downloads
```

下载后放到：

```text
knowledge/security/raw/cwe/
```

## Chunk 设计

### Markdown chunk

对 OWASP Markdown 文件按标题切分：

```text
# title
## section
### subsection
```

每个 chunk 保留：

```json
{
  "id": "owasp_cheatsheet:SQL_Injection_Prevention_Cheat_Sheet:primary_defenses",
  "source": "OWASP CheatSheetSeries",
  "title": "SQL Injection Prevention Cheat Sheet",
  "section": "Primary Defenses",
  "text": "...",
  "path": "cheatsheets/SQL_Injection_Prevention_Cheat_Sheet.md",
  "url": "https://github.com/OWASP/CheatSheetSeries/blob/master/cheatsheets/SQL_Injection_Prevention_Cheat_Sheet.md",
  "license": "CC-BY-SA-4.0",
  "tags": ["sql injection", "input validation", "database"]
}
```

### CWE record

CWE 适合结构化入库：

```json
{
  "id": "CWE-89",
  "source": "MITRE CWE",
  "name": "Improper Neutralization of Special Elements used in an SQL Command",
  "description": "...",
  "mitigations": ["..."],
  "related_weaknesses": ["..."],
  "url": "https://cwe.mitre.org/data/definitions/89.html"
}
```

## 向量数据库选择

建议第一版用 Qdrant。

原因：

- 容易 Docker 启动。
- 面试时可以明确说“我自己搭了向量数据库”。
- 支持 metadata filter。
- 后续 persona 隔离可以通过 collection 或 payload 过滤实现。

启动：

```bash
docker run -p 6333:6333 -v qdrant_storage:/qdrant/storage qdrant/qdrant
```

第一版 collection：

```text
taleclaw_security_knowledge
```

payload 建议：

```json
{
  "domain": "security",
  "persona_id": "security_review",
  "source": "OWASP CheatSheetSeries",
  "title": "...",
  "section": "...",
  "url": "...",
  "license": "CC-BY-SA-4.0"
}
```

## Embedding 策略

第一版不要纠结模型。使用当前项目已有 provider pool 最好。

推荐新增一个模型用途：

```text
embedding
```

如果暂时没有 embedding provider，可以先用以下两种降级方案：

1. SQLite FTS 或关键词检索
2. 简单 BM25 检索

更推荐：

```text
先做关键词/BM25 MVP
再接 Qdrant + embedding
```

这样不会被 embedding API 卡住进度。

## 工具设计

### security_knowledge_search

用途：

检索安全知识库。

输入：

```json
{
  "query": "Flask SQL injection password authentication",
  "top_k": 5,
  "sources": ["owasp_cheatsheet", "owasp_top10", "cwe"]
}
```

输出：

```json
{
  "results": [
    {
      "title": "SQL Injection Prevention Cheat Sheet",
      "section": "Primary Defenses",
      "excerpt": "...",
      "url": "...",
      "source": "OWASP CheatSheetSeries",
      "license": "CC-BY-SA-4.0",
      "score": 0.82
    }
  ]
}
```

### security_review_code

用途：

对代码进行安全审查。

输入：

```json
{
  "code": "...",
  "language": "python",
  "framework": "flask",
  "focus": ["authentication", "input validation", "session"]
}
```

内部流程：

```text
1. LLM 或规则提取风险关键词
2. 调用 security_knowledge_search
3. 结合代码和检索结果生成审查报告
4. 输出引用来源
```

输出格式：

```markdown
## 安全审查结果

### 1. SQL 注入风险

- 位置：
- 风险等级：
- 问题说明：
- 依据：
- 引用：
- 修复建议：

### 2. 密码处理风险

- 位置：
- 风险等级：
- 问题说明：
- 依据：
- 引用：
- 修复建议：
```

### security_review_file

第二步再做文件审查工具：

```json
{
  "path": "app.py",
  "focus": ["authentication", "injection"]
}
```

它先读取 sandbox/storage 中允许访问的文件，再复用 `security_review_code`。

## Persona 设计

新增 persona：

```text
security_review
```

系统提示词核心规则：

```text
你是安全代码审查 agent。
你必须优先基于 OWASP CheatSheet、OWASP Top10、CWE 等知识库进行判断。
你需要输出风险点、证据、引用来源和修复建议。
没有检索依据时，必须说明“未找到足够知识库依据”，不能编造标准编号。
你可以指出潜在风险，但要区分“确认问题”和“需要进一步确认的问题”。
```

## 和现有架构的接入点

### PluginManager

新增插件：

```text
plugins/security_review/plugin.py
```

插件注册：

```text
security_knowledge_search
security_review_code
security_review_file
```

### ToolRegistry

建议模式：

| 工具 | bot | coding | scheduled_agent |
| --- | --- | --- | --- |
| security_knowledge_search | 可见 | 可见 | 可按审批开放 |
| security_review_code | 可见 | 可见 | 可按审批开放 |
| security_review_file | 可见 | 可见 | 可按审批开放 |

`security_review_file` 必须走现有文件访问边界，不能直接读任意服务器路径。

### ContextBuilder

如果后续加入 persona 隔离：

```text
session.metadata.persona_id = "security_review"
```

ContextBuilder 根据 persona 注入：

```text
security persona prompt
security memory
security knowledge retriever hints
```

## 演示样例

准备一段漏洞代码：

```python
@app.route("/login", methods=["POST"])
def login():
    username = request.form["username"]
    password = request.form["password"]
    sql = f"SELECT * FROM users WHERE username='{username}' AND password='{password}'"
    user = db.execute(sql).fetchone()
    if user:
        session["user"] = username
        return "ok"
    return "failed"
```

期望检出：

- SQL 注入风险。
- 明文密码比对。
- 缺少密码哈希。
- 缺少 CSRF 防护。
- 会话安全配置不足。

期望输出：

```text
该代码把 username/password 直接拼接进 SQL，存在 SQL 注入风险。
知识库依据：OWASP SQL Injection Prevention Cheat Sheet，Primary Defenses。
可能对应 CWE-89。
建议使用参数化查询，并对密码使用安全哈希算法。
```

## 开发阶段安排

### 第一阶段：知识库入库闭环

目标：

```text
OWASP Markdown -> chunks.jsonl -> 检索可返回来源
```

任务：

- 创建 `knowledge/security` 目录。
- 拉取 OWASP CheatSheetSeries 和 Top10。
- 编写 Markdown chunker。
- 输出 `processed/chunks.jsonl`。
- 做一个 CLI 测试命令，输入 query 返回 top chunks。

验收：

```text
输入 "SQL injection password login"
能返回 SQL Injection Prevention Cheat Sheet 等相关内容。
```

### 第二阶段：工具插件

目标：

```text
agent 可以调用 security_knowledge_search
```

任务：

- 新增 `plugins/security_review`。
- 注册 `security_knowledge_search`。
- 接入 bot/coding 模式。
- 输出结果带 url/license/source。

验收：

```text
用户问 “查一下 SQL 注入相关 OWASP 规范”
agent 能调用工具并返回来源。
```

### 第三阶段：代码审查

目标：

```text
security_review_code 能输出结构化审查报告
```

任务：

- 实现风险关键词提取。
- 调用 RAG 检索。
- 用 LLM 生成报告。
- 报告中强制带引用。

验收：

```text
输入 Flask 漏洞代码，报告能列出 SQL 注入和密码处理风险。
```

### 第四阶段：Persona 隔离

目标：

```text
security_review persona 有独立 prompt、memory、knowledge scope
```

任务：

- session metadata 增加 `persona_id`。
- memory scope 加上 persona 维度。
- storage/knowledge metadata 加上 persona 维度。
- 前端增加 persona selector。

验收：

```text
切换到 security persona 后，回答风格和工具偏好明显不同。
```

### 第五阶段：依赖漏洞查询

目标：

```text
检查 requirements.txt / package-lock.json 中是否存在已知漏洞
```

数据源：

- GitHub Advisory Database。
- OSV.dev。
- CVEProject/cvelistV5。
- CISA KEV。

验收：

```text
用户上传依赖文件后，agent 可以输出高风险依赖和修复建议。
```

## 风险和注意事项

### 不要让 agent 编造引用

报告必须区分：

```text
已检索到依据
未检索到足够依据
模型根据代码模式推测
```

### 不要把安全审查说成绝对结论

建议表述：

```text
发现潜在风险
建议进一步确认
基于当前代码片段无法判断
```

### 文件读取必须受限

`security_review_file` 只能读取用户 sandbox/storage 中允许访问的路径。

### 许可证要保留

OWASP CheatSheetSeries 是 CC-BY-SA-4.0，输出引用时要保留来源。

## 面试叙事

可以这样讲：

```text
我给 taleclaw 加了一个 Security Review persona。
它不是靠 prompt 假装懂安全，而是接入了 OWASP CheatSheet、OWASP Top10 和 CWE 作为可溯源知识库。
用户让 coding agent 写完代码后，可以让 security persona 做审查。
审查时 agent 会检索相关规范，输出风险点、引用来源和修复建议。
这个功能展示了 persona 隔离、RAG 检索、工具调用、引用溯源和代码理解的完整闭环。
```

## 明天优先完成的最小任务

建议只做这 5 件事：

1. 拉取 OWASP CheatSheetSeries。
2. 写 Markdown chunker。
3. 生成 `chunks.jsonl`。
4. 写一个本地 `security_knowledge_search`。
5. 用 Flask 漏洞代码跑出一份带引用的审查报告。

完成这 5 件事后，这个方向就已经可以演示。
