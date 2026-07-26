# Phase 17：关闭 Context 服务兼容构造入口

`ContextBuilder` 已删除以下兼容参数：

- `memory_store`；
- `instruction_root`；
- `instruction_limit`；
- `skill_loader`；
- `working_memory_renderer`。

生产 Bootstrap、Coding Application、Subagent、Evaluation、测试和基准现在显式构造
并注入：

```text
PromptAssetsService
ContextMemoryService
ContextRetrievalService
```

`ContextBuilder` 不再知道 Memory store、Instruction 文件系统或 Skill loader 的具体
构造方式。若未注入服务，则使用无 Skill、默认工作目录 Prompt Assets 和无 Memory
的最小实现。

验证结果：

- Phase 17 定向及 Evaluation 回归：51 passed，1 skipped；
- 完整回归：439 passed，39 skipped；
- 安全/Workspace/Tool 组合测试：61 passed，1 skipped；
- Token：Chat 730、Coding 2347；
- Runtime facade：0.716ms；
- Chat/Coding Context：0.189/0.224ms；
- `ContextBuilder` 为 1264 行；
- 快照无变化，未发现行为或性能退化。
