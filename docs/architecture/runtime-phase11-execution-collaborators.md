# TaleClaw Phase 11：Execution 无状态协作者（历史）

> 本文记录 Phase 11 当时的实现。Phase 2 aggressive pruning 已删除
> `tool_batch.py` 及 ReasoningLoop 的隐式 Task/Read 批处理；显式批量工具仍可按需启用。

Phase 11 将 ReasoningLoop 中可以独立验证的纯逻辑提取为：

- `message_sanitizer.py`：清洗空 Assistant、孤立 Tool result 和不完整 Tool group；
- `model_invocation.py`：选择 streaming/non-streaming 模型调用；
- `tool_batch.py`：批量 Task/Read 参数与结果投影。

ReasoningLoop 从 1989 行降至 1765 行。提取后的模块不依赖 Application、Memory、
RAG、Plugin 或 Trace Store，没有新增状态、Adapter 或兼容路径。

尚未外移的 Tool 执行编排、Termination、Trace 投影和可选策略继续列入后续阶段，
不以本阶段的行数下降宣称总目标完成。

Gate：定向测试 46 passed/2 skipped，完整回归 422 passed/39 skipped，安全测试
64 passed/1 skipped，Context token 730/2347，性能无明显退化，快照无变化。
