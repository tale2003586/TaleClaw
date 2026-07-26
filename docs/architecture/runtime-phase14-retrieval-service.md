# Phase 14：Context Retrieval/Trace 服务提取

## 结果

History Vector Retrieval、Security RAG 路由、知识检索、RAG Trace 和运行事件构造已从
`ContextBuilder` 移至独立的 `ContextRetrievalService`。

`ContextBuilder` 从 1736 行降至 1506 行，并且不再包含：

- Security RAG 事件常量；
- RAG Trace payload 构造；
- Security Router/Index 调用算法；
- History Vector 查询与结果渲染算法。

完整产品由 `runtime.bootstrap` 显式构造并注入 Retrieval Service。最小
`ContextBuilder()` 不创建该服务，也不加载 Knowledge、RAG 或 Trace 实现。

## 兼容边界

为避免一次修改大量外部构造代码，Builder 本阶段仍接受旧 Retrieval 构造参数，并
仅在这些参数实际出现时延迟适配成 `ContextRetrievalService`。具体执行逻辑已经
全部移出 Builder；后续阶段将迁移剩余调用者并删除这些兼容参数。

## Gate

- Phase 14 定向回归：65 passed，1 skipped；
- 完整回归：432 passed，39 skipped；
- 安全/Workspace/Tool：77 passed，1 skipped；
- Chat/Coding Token：730/2347，无变化；
- 快照：无变化；
- Runtime facade：0.705ms；
- Chat/Coding Context：0.184/0.241ms，未发现明显退化。
