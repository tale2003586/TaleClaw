# Runtime Phase 0 性能基准

运行命令：

```bash
python benchmarks/runtime_phase0.py --iterations 50
```

机器可读结果写入：

```text
benchmarks/results/runtime_phase0.json
```

## 测量原则

- 全部使用 ScriptedModel 和 RecordingTool；
- 不访问网络、真实模型、用户 Memory 或真实数据库；
- 预热后重复测量；
- 输出 median 和 p95；
- 不设置跨机器绝对失败阈值；
- Token 数使用项目当前 `runtime.token_estimator.estimate_tokens`。

## 当前覆盖

- Pipeline/AgentRunner 测试装配时间；
-无工具 Chat 的模型外路径；
-一次 Tool Call；
-连续三次 Tool Call；
- ToolExecutor wrapper；
- Chat Context 构建；
- Coding Context 构建；
- Chat/Coding 估算 Token；
-候选调用链组件数量。

## 限制

`runtime.bootstrap.build_runtime()` 会解析真实 provider 配置、Session Store、Memory、
插件和可选 RAG，因此离线微基准不直接执行完整 Bootstrap。该脚本测量的是当前
`Pipeline + AgentRunner + ReasoningLoop` 的受控等价装配。

Memory、Trace、Streaming、Coding Task Session、Subagent 和 Cancellation 的完整
wall-time 对比尚未进入此微基准。Phase 0 先用行为测试固定其语义；若 Phase 1 会修改
对应路径，应在修改前增加针对性计时场景。

## 结果

2026-07-23、Python 3.14.6、macOS arm64、50 次迭代：

| 场景 | 迭代 | Median | P95 |
|---|---:|---:|---:|
| Pipeline 测试装配 | 50 | 0.041 ms | 0.044 ms |
| 无工具 Chat Runtime | 50 | 0.712 ms | 0.753 ms |
| 单 Tool Call Runtime | 50 | 1.294 ms | 1.447 ms |
| 连续三 Tool Call Runtime | 50 | 2.663 ms | 2.747 ms |
| ToolExecutor wrapper | 50 | 0.0015 ms | 0.0016 ms |
| Chat Context build | 50 | 0.177 ms | 0.186 ms |
| Coding Context build | 50 | 0.218 ms | 0.229 ms |
| Chat Context + Memory | 50 | 0.176 ms | 0.188 ms |
| Chat + Null Trace | 50 | 0.767 ms | 0.785 ms |
| Chat Streaming | 50 | 0.671 ms | 0.680 ms |
| Model 前 Cancellation | 50 | 0.181 ms | 0.185 ms |
| Task Session Factory | 50 | 0.490 ms | 0.524 ms |
| Subagent 创建并返回 | 50 | 2.477 ms | 2.703 ms |

该场景的 Chat/Coding Context 当前分别估算为 730/2347 tokens。实际机器可读结果
以同机生成的 `benchmarks/results/runtime_phase0.json` 为准。不同机器或 Python
版本之间只比较趋势，不比较微秒级绝对值。

## 闭环扩展结果

| 场景 | 迭代 | Median | P95 | 边界 |
|---|---:|---:|---:|---|
| Trace 本地磁盘写入 | 50 | 4.918 ms | 5.705 ms | 真实 JSONL/report，关闭 PostgreSQL Index，含临时目录 |
| Vector Memory Search | 50 | 0.0078 ms | 0.0080 ms | 确定性本地 VectorIndex Fake，4 条记录 |
| Security RAG Search | 50 | 0.0023 ms | 0.0025 ms | 确定性本地 RAG Fake，3 条文档 |

Vector/RAG 数字衡量 Runtime 接口与本地排序开销，不代表 Qdrant、Embedding 或
Reranker 的吞吐。外部基础设施吞吐必须作为独立集成基准运行，不能混入本轮
Runtime 自身基线。
