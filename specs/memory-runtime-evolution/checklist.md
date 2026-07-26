# Agent Memory Runtime Evolution Checklist

- [ ] C01 基线分支、提交、工作区和测试结果已记录。
- [ ] C02 hierarchy 文档区分当前实现、缺口和目标。
- [ ] C03 MemoryNote/Link 可验证、序列化、兼容投影且无共享默认列表。
- [ ] C04 relation candidate 与 accepted link 明确区分。
- [ ] C05 governance 对 explicit/inferred/tool/task/sensitive/injection 产生保守 decision 与 audit。
- [ ] C06 feature flag 关闭时 candidate/promotion 行为不变。
- [ ] C07 Tool metadata 有安全默认，旧 schema、权限和注册调用不变。
- [ ] C08 Context Pressure 四级与异常输入测试通过，evaluator 无副作用。
- [ ] C09 pressure observation 关闭时 prompt 快照不变。
- [ ] C10 relation/evolution 只产生 pending proposal，不改 active memory。
- [ ] C11 enrichment 限制 metadata、keywords/tags、scope 和 secret，失败回退。
- [ ] C12 injection trace 可解释 selected/filtered，不含完整敏感 content。
- [ ] C13 Trace summary 可汇总 governance/pressure/proposal/injection。
- [ ] C14 所有新增公共接口有真实调用者或集成测试。
- [ ] C15 完整 pytest、compileall、diff check 通过。
- [ ] C16 最终报告包含 commit、风险、跳过阶段、真实测试与明日人工清单。
