# Phase 13：最小 Context Kernel 与显式能力组合

## 结果

`ContextBuilder()` 现在默认只组合 Prompt、会话 History 和三个无副作用的空能力
Provider。它在导入、构造和普通 Chat Context 构建时不会加载 Memory、Knowledge/RAG、
Coding Application、Working Memory 或 Skill Runtime。

完整产品能力没有删除。`runtime.bootstrap` 和 `CodingApplication` 作为 Composition
Root，显式注入 `DEFAULT_CONTEXT_PROVIDERS`、Working Memory renderer、Skill
loader 与 Coding Context view builder。

专项测试也必须显式选择完整组合。这让测试表达其真实依赖，而不是依赖
`ContextBuilder` 的隐藏默认值。

## 边界

最小默认组合：

```text
Prompt → History → Empty Memory → Empty Retrieval → No Coding
```

完整产品组合：

```text
Prompt → History → Memory → Retrieval/Security RAG → Coding
```

本阶段保留 `ContextBuilder` 内部的 Security RAG 延迟实现，以避免跨阶段改写
Prompt、Trace 和检索语义。它已从模块导入路径移除，后续可进一步提取为独立
Retriever/Trace collaborator。

## Gate

- 完整回归：429 passed，39 skipped；
- 安全/Workspace/Tool 定向回归：77 passed，1 skipped；
- Prompt/Token：Chat 730，Coding 2347，未变化；
- 快照：无变化；
- 性能：Chat Context 0.183ms、Coding Context 0.224ms；未发现明显退化；
- 依赖审计：导入 `runtime.context` 不加载可选能力包。
