# 📁 mytry 项目结构优化建议（重写版）

> 基于 **2025-05-08 最新目录** 重新分析，之前那版是本天才瞎了，已删除 🙇‍♀️💥

---

## ✅ 当前真实结构一览

```
mytry/
│
├── 🧩 核心模块
│   ├── AgentLoop.py          # Agent 主循环
│   ├── pipeline.py           # TurnPipeline ✅ 已有
│   ├── runtime.py            # 运行时管理 ✅
│   ├── bootstrap.py          # 装配 ✅
│   ├── session.py            # SessionManager ✅
│   └── protocols.py          # 协议定义
│
├── 🧰 工具系统
│   └── tools/
│       ├── __init__.py
│       ├── tools.py          # 工具 handler
│       ├── schema.py         # 工具 schema
│       └── tool_registry.py  # ✅ 已经有 ToolRegistry 了！
│
├── 🧠 模型 / 模式
│   └── models/
│       ├── __init__.py
│       ├── base.py           # ModeProfile 基类 ✅
│       ├── bot.py            # Nanobot 模式 ✅
│       ├── coding.py         # Coding 模式 ✅
│       └── router.py         # 模式路由 ✅
│
├── 📨 消息总线
│   └── bus/
│       ├── message_bus.py    # 队友通信
│       └── user_bus.py       # 用户消息
│
├── 💾 记忆系统
│   └── memory/
│       ├── __init__.py
│       ├── store.py
│       ├── MEMORY.md
│       ├── NOW.md
│       └── SELF.md
│
├── 🛠️ 技能系统
│   └── skills/
│       ├── agent-builder/
│       ├── code-review/
│       ├── mcp-builder/
│       └── pdf/
│
├── 📝 杂项
│   ├── config.py             # 配置
│   ├── compact.py            # 上下文压缩
│   ├── background_task.py    # 后台任务
│   ├── task.py               # 任务系统
│   ├── teammate.py           # 队友管理
│   ├── cli.py                # CLI 入口
│   ├── skills.py             # 技能加载器
│   └── todo.py               # 待办
│
├── 📂 数据目录
│   ├── .agents/
│   ├── .codex/
│   ├── .tasks/
│   ├── .team/ ─── inbox/
│   ├── .transcripts/
│   └── scratch/              # 杂记（alice-note, bob-note, summary）
│
├── 🗑️ 脏数据
│   ├── __pycache__/          # 到处散落
│   └── .vscode/
│
├── 📄 文档
│   ├── __init__.py           # ❌ 空的
│   ├── REFACTOR_PLAN.md
│   ├── AKASHIC_AGENT_LEARNING_PLAN.md
│   └── STRUCTURE_SUGGESTION.md  ← 就这个文件
│
└── .git/
```

---

## 📊 现状评分

| 维度 | 评分 | 说明 |
|------|:---:|------|
| 模块化程度 | ⭐⭐⭐⭐ | tools/ models/ bus/ memory/ 已拆得不错 |
| 入口清晰度 | ⭐⭐⭐ | cli.py 是入口但还有 bootstrap/runtime 并存 |
| 根目录整洁度 | ⭐⭐ | 14 个 .py 文件堆在根目录 |
| pycache 控制 | ⭐ | 完全没管 |
| 数据目录 | ⭐⭐⭐ | 隐藏目录已有但散落各处 |
| 与 REFACTOR_PLAN 对齐度 | ⭐⭐⭐⭐ | models/ tools/ 已对齐，就差整合 |

---

## 🎯 优化建议

### 1️⃣ 根目录瘦身（最重要）

现在的根目录有 **14 个 .py 文件**，太吵了。建议：

| 文件 | 归属 | 理由 |
|-----|------|------|
| `compact.py` | → `core/compact.py` | 核心工具 |
| `protocols.py` | → `core/protocols.py` | 协议定义 |
| `background_task.py` | → `core/background_task.py` | 核心工具 |
| `task.py` | → `core/task.py` | 核心工具 |
| `skills.py` | → `skills/__init__.py` | 跟 skills 目录合一 |
| `todo.py` | → 删除或归档 | 看起来是个人待办 |
| `config.py` | → 保留根目录 | 配置常驻根目录合理 |

**目标根目录结构：**

```
mytry/
├── main.py              # 统一入口（整合 cli.py + bootstrap + runtime）
├── config.py            # 配置
├── pipeline.py          # TurnPipeline
├── session.py           # SessionManager
├── runtime.py           # Runtime
├── bootstrap.py         # 装配
├── AgentLoop.py         # Agent 主循环
├── teammate.py          # 队友管理
├── cli.py               # CLI 入口（可保留或并入 main.py）
│
├── core/                # 🆕 核心能力目录
│   ├── __init__.py
│   ├── compact.py
│   ├── protocols.py
│   ├── background_task.py
│   └── task.py
│
├── tools/               # ✅ 不动
├── models/              # ✅ 不动
├── bus/                 # ✅ 不动
├── memory/              # ✅ 不动
├── skills/              # ✅ 不动
│
├── .runtime/            # 🆕 统一隐藏数据目录
│   ├── agents/
│   ├── tasks/
│   ├── team/
│   │   └── inbox/
│   ├── transcripts/
│   └── codex/
│
├── notes/               # 🔄 scratch/ → notes/ 改名
│
├── __init__.py          # 🔄 补充导出
├── .gitignore           # 🆕 添加
└── .git/
```

---

### 2️⃣ 统一隐藏数据目录

| 当前 | 建议 |
|------|------|
| `.agents/` | → `.runtime/agents/` |
| `.tasks/` | → `.runtime/tasks/` |
| `.team/` | → `.runtime/team/` |
| `.transcripts/` | → `.runtime/transcripts/` |
| `.codex/` | → `.runtime/codex/` |

改 `config.py` 里的 `TEAM_DIR` 等路径指向 `.runtime/` 即可。

---

### 3️⃣ 清理 `__pycache__` + 加 `.gitignore`

```bash
find . -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null
```

`.gitignore` 建议内容：

```gitignore
__pycache__/
*.pyc
.runtime/
.vscode/
scratch/
.env
*.egg-info/
dist/
build/
```

---

### 4️⃣ 补充 `__init__.py`

```python
# mytry/__init__.py
from . import tools
from . import models
from . import bus
from . import memory
from . import skills
```

---

### 5️⃣ 善用已有的 models/ 目录

你已经有 `models/base.py`、`models/bot.py`、`models/coding.py`、`models/router.py` 了，这跟 `REFACTOR_PLAN.md` 里说的 `modes/` 概念完全一致！🎉

**建议**：如果 `models/` 的语义就是 mode（模式），可以保留名字；如果想跟计划完全对齐，可以改名为 `modes/`。但改名有成本，不急着改。

---

## 📋 实施优先级

| 优先级 | 事项 | 预估工时 |
|:-----:|------|:-------:|
| 🅿️0 | 加 `.gitignore` + 清 `__pycache__` | 5 分钟 |
| 🅿️1 | 创建 `core/` 目录，搬 `compact.py` `protocols.py` `background_task.py` `task.py` | 10 分钟 |
| 🅿️2 | 创建 `.runtime/`，迁隐藏目录 | 15 分钟 |
| 🅿️3 | 改 `config.py` 路径指向 `.runtime/` | 5 分钟 |
| 🅿️4 | `scratch/` → `notes/` | 5 分钟 |
| 🅿️5 | 补充 `__init__.py` | 2 分钟 |
| 🅿️6 | 考虑 `models/` 是否改名 `modes/` | 想清楚再动 |

---

## 🏁 最终愿景

```
mytry/
├── main.py              # 统一入口
├── config.py
├── pipeline.py / session.py / runtime.py / bootstrap.py
├── AgentLoop.py / teammate.py / cli.py
│
├── core/                # 核心工具
├── tools/               # 工具系统
├── models/              # 模式系统
├── bus/                 # 消息总线
├── memory/              # 记忆系统
├── skills/              # 技能系统
│
├── .runtime/            # 统一运行时数据
├── notes/               # 笔记
│
├── __init__.py
├── .gitignore
└── REFACTOR_PLAN.md
```

> 💡 **本天才的真心话**：你的项目骨架其实已经搭得不错了，`models/` `tools/` `pipeline.py` 都就位了。现在最大的问题就是**根目录太乱 + 没管 pycache**。先把这两刀砍了，项目颜值直接提升 50%！😤✨

---

*重写于 2025-05-08，这次是真的看了最新目录写的！再错我就...我就...哼！咬你！🦷*
