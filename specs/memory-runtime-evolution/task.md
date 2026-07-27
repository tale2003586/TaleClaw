# Agent Memory Runtime Evolution Tasks

1. T01：完成 Stage 0 事实表、基线记录和 hierarchy 文档；验证文档无虚构能力。
2. T02：实现 MemoryNote/MemoryLink/adapter；运行模型单测并独立提交。
3. T03：实现 governance pipeline、secret/prompt-injection 保守规则与 audit；运行单测并独立提交。
4. T04：扩展 ToolSpec governance metadata，验证 catalog/schema/权限不变并提交。
5. T05：实现 Context Pressure 纯 evaluator 与只观测 adapter，验证 prompt 不变并提交。
6. T06：实现 relation/evolution pending proposal，验证不修改 active memory 并提交。
7. T07：实现 pending enrichment 与 flag-off 兼容接入，验证失败回退并提交。
8. T08：实现 injection explanation Trace 与 summary diagnostics，验证隐私边界并提交。
9. T09：评估动态 pressure 接入门禁；不满足则记录跳过。
10. T10：运行模块/全量回归、自审、报告和人工测试清单并提交。

每个任务执行“分析→修改→定向测试→diff check→commit→下一阶段”。
