# TaleClaw Runtime Phase 0 遗留事项闭环报告

> 执行日期：2026-07-23
> 代码基线：`bb4e845571428069a78a05526e71da8e41ddbcb7`
> 原则：只增加测试、测试辅助代码、基准和文档；生产 Runtime 零修改

## 结论

上一轮报告列出的 7 项 Phase 0 遗留均已形成可重复的测试、数据或明确契约，
不再阻塞后续 Runtime 重构准备。生产 PostgreSQL、Qdrant 和真实模型的吞吐不被
伪装成离线 Runtime 基准。

## 闭环矩阵

| 原遗留项 | 处理 | 验证结果 |
|---|---|---|
| 完整 Coding Runner 依赖 PostgreSQL | 新增兼容 SessionManager 接口的内存测试替身 | 完整生命周期测试通过 |
| Artifact/Diff/Conclusion/Promotion 缺完整集成 | 单测试贯穿全部阶段并检查产物 | TASK_LOG、CONCLUSIONS、PENDING、workspace_diff 全部验证 |
| Trace 磁盘性能未测 | 使用真实 TraceStore，关闭可选数据库 Index | Median 4.918 ms，P95 5.705 ms |
| Vector Memory/RAG 性能未测 | 增加确定性本地接口基准 | 0.0078/0.0080 ms；0.0023/0.0025 ms |
| Tool 无抢占式取消接口 | 固化“Handler 完成后观察取消”的真实契约 | cancellation-during-tool 测试通过 |
| Gateway delivery failure 无快照 | 增加 Telegram/Feishu 双通道失败状态快照 | 首次 pending，第二次 terminal failed |
| 测试环境依赖不完整 | 全量收集并执行 `pip check`，修正 mistune 漂移 | 431 tests 可收集，依赖检查通过 |

## 新增测试资产

- `tests/fakes/in_memory_sessions.py`
- `tests/conftest.py`
- `tests/test_runtime_phase0_closure_baseline.py`
- `tests/snapshots/runtime_phase0_coding_lifecycle_events.json`
- `tests/snapshots/runtime_phase0_gateway_delivery_failures.json`

测试态替换仅作用于 `test_coding_benchmark.py`。生产 `SessionManager` 和
`SessionStore` 仍严格要求 PostgreSQL，没有新增静默降级。

## 验证结果

```text
Phase 0：20 passed in 0.23s
Scripted Coding Benchmark：6 passed in 6.99s
Trace 相关（TRACE_INDEX_ENABLED=0）：35 passed, 2 skipped in 0.51s
全量收集：431 tests collected
```

全套测试审计执行一次，结果为 `374 passed, 29 skipped, 28 failed`。失败不来自
本轮 Phase 0 资产，主要是未提供 PostgreSQL 服务、macOS `/var` 与 `/private/var`
路径字符串差异、subprocess evaluation 不继承 pytest monkeypatch，以及 Telegram
setup 在 PostgreSQL skip 前切换 cwd 的既有测试隔离问题。

这些全仓库环境问题不属于上一轮列出的 Runtime Phase 0 遗留项。本轮没有借机修改
生产存储策略或无关测试。

## 性能结果

机器可读完整结果：`benchmarks/results/runtime_phase0.json`

| 场景 | Median | P95 |
|---|---:|---:|
| Trace 本地磁盘写入 | 4.918 ms | 5.705 ms |
| Vector Memory Search（本地 Fake） | 0.0078 ms | 0.0080 ms |
| Security RAG Search（本地 Fake） | 0.0023 ms | 0.0025 ms |

## 边界说明

Tool 抢占式取消、生产 Store 的内存降级、真实 Qdrant 吞吐和 PostgreSQL 部署属于
生产设计或外部基础设施工作。Phase 0 的闭环是固定现状、提供回归保护并准确标注
测量边界，而不是违反约束新增接口或改变运行语义。

截至本报告，上一轮“仍未完成事项”列表已清零；外部基础设施集成验证应作为独立
CI profile 执行。
