# Phase 15：删除 Context 检索兼容构造路径

`ContextBuilder` 现在只通过单一 `retrieval_service` 边界接收检索能力。以下旧参数
已全部删除：

- History/Memory Vector Index 与 scope resolver；
- Retrieval top-k 与 min-score；
- Security Router、Classifier、Knowledge Index；
- Security auto-context 开关。

生产 Bootstrap、专项测试和基准均直接构造 `ContextRetrievalService`，或明确使用
无检索的最小 Builder。核心 Context 构造器不再理解任何具体检索配置。

验证结果：

- Phase 15 定向测试：54 passed；
- 完整回归：434 passed，39 skipped；
- 安全/Workspace/Tool：77 passed，1 skipped；
- Token：Chat 730、Coding 2347；
- Runtime facade：0.709ms；
- Chat/Coding Context：0.181/0.222ms；
- 快照无变化，未发现性能退化。
