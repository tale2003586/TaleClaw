# Runtime Context 目录结构评估

日期：2026-07-23

## 结论

`runtime/` 当前存在同一子域文件平铺过多的问题。以下六个模块共同组成
Context 构建子系统，适合收敛到 `runtime/context/`：

- `context.py`（1776 行）
- `context_history.py`（755 行）
- `context_budget.py`（277 行）
- `context_providers.py`（206 行）
- `context_sections.py`（84 行）
- `context_build_state.py`（50 行）

目录迁移是合理的，但不应只做机械搬运。`context.py` 已经承担入口类型、构建
流程、缓存、检索、Memory、Coding Context 和安全 RAG 等多种职责；如果仅改
路径，复杂度只是被藏进文件夹。

## 建议结构

```text
runtime/
  context/
    __init__.py       # 稳定公开 API：ContextBuilder、ContextBundle、ContextPrefix
    builder.py        # ContextBuilder 主编排
    budget.py         # ContextBudgeter 与预算类型
    build_state.py    # 单次构建的内部状态
    history.py        # 历史消息裁剪、压缩和活动轮次
    providers.py      # Context Provider 实现
    sections.py       # Section 与 BuildReport 数据结构
```

现有公开导入 `from runtime.context import ContextBuilder` 可以通过
`runtime/context/__init__.py` 保持不变，因此无需建立额外的旧路径兼容包。
子模块导入则统一迁移到 `runtime.context.*`。

## 不建议同时迁入的文件

- `working_memory.py`：它代表运行期状态能力，不只是 Prompt Context 的内部实现。
- `tool_result_compression.py`：同时服务 Reasoning Loop 和历史处理。
- `token_estimator.py`：属于模型调用预算的通用基础能力。
- `trace/context_metrics.py`：应继续属于 Trace 子系统。
- `applications/coding/context_state.py`：属于 Coding Application，不应倒退回 Core。

## 需要额外解决的边界

当前 `runtime/context.py` 直接导入
`applications.coding.context_state`。这意味着 Core Runtime 反向依赖具体
Application。目录迁移时应优先保持行为，随后通过已存在的 Context Provider
或扩展注入边界消除该反向依赖；不应为了整理目录把 Coding 文件重新搬回
Runtime。

## 风险与验证

主要风险是模块改为 package 后的导入解析、测试 patch 路径、循环依赖和打包
清单变化。实施时至少需要验证：

1. `runtime.context` 的公开符号保持不变；
2. Context、Coding Context、Runtime Facade 和 Subagent 定向测试；
3. 完整回归与 package import smoke；
4. Prompt、Token 和 Streaming 快照无变化；
5. Context 构建 benchmark 无明显退化；
6. 仓库不存在旧的 `runtime.context_*` 导入。

## 额外观察

本地 `runtime/__pycache__/` 中仍有已经迁走模块的旧 `.pyc` 文件，它们不是源码，
但会让目录检查结果显得更杂乱。清理缓存应作为独立、可恢复性明确的维护动作，
不应混入源码目录重构提交。
