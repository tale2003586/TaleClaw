# Agent Memory Runtime Evolution Spec

## 目标

- 显式记录 TaleClaw 现有 memory hierarchy 与真实调用链。
- 在现有 `MemoryItem`、command/promotion、ContextBuilder、ToolRegistry 和 TraceStore 上增加可治理、可观测的演进能力，不创建平行 memory runtime。
- 增加 MemoryNote、MemoryLink、write governance、relation/evolution proposal、pending enrichment、context pressure 与 injection explanation。
- 默认不重写 active/stable memory，不自动应用 evolution，不改变 prompt、retrieval、tool 权限或 promotion。

## 功能需求

- F1：文档区分现状、缺口、目标与迁移路线，并提供 CONFIRMED/INFERRED 事实表。
- F2：MemoryNote/MemoryLink 可验证、序列化并与现有 MemoryItem 双向兼容投影。
- F3：治理 pipeline 对来源、scope、置信度、敏感模式和 prompt injection 给出保守 decision 与 audit。
- F4：ToolSpec 支持安全默认的治理 metadata，旧注册调用与模型 tool schema 不变。
- F5：Context Pressure evaluator 是确定性纯函数，可产生 level 与 policy hint；观测不改变 prompt。
- F6：relation decider 只生成 link/evolution proposal，默认不应用、不改旧 memory。
- F7：pending enrichment 有长度/数量/scope/secret 校验，失败回退，默认关闭真实接入。
- F8：memory retrieval/injection trace 记录 ID、scope、score、decision、representation、token 和 pressure，不记录完整敏感内容。
- F9：诊断汇总复用现有 Trace summary，可关联 run/session/task。

## 非功能与边界

- N1：新行为通过 feature flag 控制，flag 关闭时兼容现有测试与 prompt 快照。
- N2：不新增数据库 migration 或第三方依赖，不修改 `.env`，不自动 push。
- N3：task scope 不得提升为 user/global；LLM 输出必须经过 Runtime validation。
- N4：所有阶段有独立测试、diff 检查和 Git commit。
- N5：动态 Context 策略仅在调用链与不变性测试充分时实施，否则只输出 hint。

## 验收标准

- AC1：新增模型和 adapter 单元测试覆盖默认值、边界、序列化与非法输入。
- AC2：明显 secret/prompt injection/inferred/tool-result 不可直接 stable，decision 有 audit。
- AC3：旧工具注册、schema、权限和执行测试完全不变。
- AC4：LOW/MEDIUM/HIGH/CRITICAL 与异常输入均可确定计算，观测开关关闭时 Context 输出逐字不变。
- AC5：relation/evolution 只产生 pending proposal；active content/status 不变。
- AC6：enrichment 关闭或失败时原 candidate 行为不变。
- AC7：injection trace 不含完整 content/secret，并能解释 selected/filtered。
- AC8：全量回归、compileall、diff check 通过；报告包含真实结果与人工清单。
