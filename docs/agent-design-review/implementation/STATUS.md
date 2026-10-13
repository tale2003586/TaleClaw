# 实施状态

更新：2026-09-27，Asia/Shanghai。**当前：已完成。** 推荐精简主线、独立审查及最终项目虚拟环境全量数据库验收均通过。没有未解决的本次引入 P0/P1，没有需要用户立即授权的阻塞。

## 基线、范围与所有权

- 分支 feat/memory-runtime-evolution，HEAD 仍为 5bc28e8d883a6ab4daf7e8d734ca3a1bcfbe24a2；本轮全部改动未提交、未推送、未部署。
- 开始时 tracked 无修改；既有 untracked 为 docs/agent-design-review/、docs/system-design (2)/、os。后两者未动，01–07 原报告摘要保持不变。没有适用 AGENTS.md。
- 按 [07 §8.2](../07-master-plan.md) 的**推荐精简主线**实施，包含必要记忆资格修复及 T08/T09 原创离线试验。不是声称完成 §8.3 全部扩展任务。LoRA 依 §9 判定 No-go，未训练、未部署替换器。
- 用户本轮附件明确授权实施，替代前轮只读限制；未安装依赖、下载模型、使用付费推理、访问既有数据库或上传私有数据。

## 工作包与验收合同

状态词：待开始、实施中、待审查、待修复、待验收、已完成、被阻塞。代码写完不自动进入已完成。

| 包／原方案 | 目标、范围与输入输出 | 前置 | 行为、验收与测试 | 风险／回退 | 状态 |
|---|---|---|---|---|---|
| WP00／T00 | HEAD 隔离基线、运行环境、结果合同 | 无 | 历史失败独立记录；离线、临时源码及数据库 | 保留基线日志，不触既有服务 | 已完成 |
| WP01／T01 | tools executor/registry/handlers/hooks/command；工具调用→结构化结果 | WP00 | 非零退出、非法schema、取消不报成功；写后/跨轮读新值；真实子进程退出检查 | str兼容，第三方schema可能被拒；按包回退但不能恢复假成功 | 已完成 |
| WP02／T02–T04 最小项 | runtime/provider/coding/task_state；根预算、scope、取消→停止事实 | WP01 | final≠verified；单写、根指令来自选中workspace；晚线程不能再开调用；模型完成保护 | 用户级锁串行不同repo，禁BG/TEAM新启动；可调整显式预算，不解除无控制写路径 | 已完成 |
| WP03／T05 | context_state/events/snapshots；来源+前代+新事件→有界视图 | WP00/02 | 三代保留约束/证据；伪user不换目标；失效快照不激活；超窗显式拒绝 | 真用户文本累积可触上限；保留原事件，按包回退 | 已完成 |
| WP04／T06 | trace/evaluation/scripts；运行事实→元数据、manifest、独立验收、完整patch | WP01/02 | 未知用量保持null，已知下界单列，不将unknown当零费用；生成非通过；grader异常保留行；patch干净重放；默认隐私测试 | 字段语义变化，详细trace显式开启；消费端一起回退 | 已完成 |
| WP05／T07 必要边界 | memory、Coding promotion；模型建议→candidate，宿主确认→active | WP01/02 | 自报verified无效；候选不污染active；过期不可复活；证据PK不冲突；基础无向量读取 | 没有新增审核UI；旧不安全晋升接口显式报错，不自动降回直写 | 已完成 |
| WP06／T08+T09前置 | routing、research；门内请求→单次RouteDecision；合成数据→CPU基线 | WP00/04 | 权限gate不变；分组无泄漏；真实预测可重跑；未训练/未测收益分列 | 单作者合成不能代表流量；不启用研究分类器 | 已完成（关卡No-go） |
| WP07／T11 | 独立反例、修复、集成验收、五份交付记录 | 所选包 | 无未关P0/P1；最终源码与测试摘要绑定；不冒称模型质量收益 | 只回退本轮特定文件，先保存diff | 已完成 |

职责轮换：root 实施主运行时/作用域/trace/路由研究与集成；audit_arch_context 实施工具/记忆、独立审上下文/provider；audit_memory_reliability 实施上下文/provider、独立审工具/记忆并做最终数据库验证；audit_eval_finetune 实施评测、独立审 root 运行时及研究。审查者只读作者文件，成立问题交回作者修复；主 Agent 另外检查实际 diff、调用链与证据。

## 最近证据与下一步

- HEAD 基线：607 passed / 1 历史mock缺陷 / 59 DB skipped。
- 冻结后独立 PostgreSQL 首轮：893 passed / 2 旧trace夹具失败 / 0 skipped；两处已改显式诊断且增加状态断言，独立认可未降低验收。
- 第二轮：895 passed / 0 skipped，但运行器解析了虚拟环境符号链接，实际使用底层 conda 解释器，不能混为项目 .venv 结果。
- 最后一处 P2 清理反例：最终trace写失败令取消token残留，修前1失败、修后15通过；独立者复验17通过，并纳入最后完整套件。
- 研究：64条原创合成/32家族；规则test macro-F1=0.9161，CPU字符线性=0.8333（各12条/6家族），独立重现；不证明泛化与E2E收益。
- 最终项目.venv + 临时PG16：**896 passed / 0 skipped / 0 warnings**，pytest20.12秒；最后定向17通过。临时库已停，socket/pidfile不存在，证据已归档。
- 最终源码聚合SHA-256为301896082a2bf0bc9c30c7715296941ddf20ad52f54bec45829f50deae046f02。已选范围无剩余实施任务；未来扩展条件见FINAL-REPORT。不在会话结束后后台运行。

环境、所有历史失败及命令见 [VALIDATION.md](VALIDATION.md)；成立反例及关闭方法见 [REVIEWS.md](REVIEWS.md)。默认执行沙箱 mountinfo 故障时使用权限审核批准的同范围操作，未绕过审批。
