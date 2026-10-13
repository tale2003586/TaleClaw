# 技术决策

日期：2026-09-27。适用基线与工作区见 [STATUS.md](STATUS.md)。以下是本轮实际选择及兼容性说明，不将原方案全部草案接口宣称为已实现。

| ID | 决策与原因 | 成本、兼容性及未来触发条件 |
|---|---|---|
| D01 | 实施07 §8.2推荐精简主线；沿用Runtime、TaskState、PG与工具注册体系 | §8.3完整VerificationPolicy、通用恢复、UI、插件全面迁移未做；有具体需求再选包 |
| D02 | 扩展已有ToolExecutionResult/ToolHandlerOutput：status/error/exit_code/effect_state；保留str适配 | 错误字符串只在边界适配；显式success中的“Error”是数据。非法参数和不支持schema现在拒绝，插件需适配 |
| D03 | TaskStateCore仍唯一任务语义来源；RunExecutionState、执行生命周期、verification分开 | Coding final只表示执行结束，task仍active且展示awaiting_verification。通用聊天原完成语义保留；没有假装实现通用自动验收写回 |
| D04 | 当前用户附件授权代码/测试/低成本实验，未授权付费、私有上传、部署或push | 不新增依赖、不下载模型、不接现有DB；临时PG验证属本地授权范围 |
| D05 | 用户定义的P0–P3用于缺陷严重度 | 07中的P0优先次序不是事故分级；静态疑点不写已发生线上事故 |
| D06 | 按完整workspace路径锁不足以拦父子目录并写，改每OS用户一个跨进程Coding writer，flock fail-fast | 不同repo也串行；锁不阻止其他用户/编辑器。暂不建范围锁、worktree调度、多租户沙箱；不同TMPDIR/主机不作全局保证 |
| D07 | TraceStore默认metadata-only；诊断显式capture_content=True，隔离评测开启完整diff | 旧全文报告消费者需适配；Session DB/应用task artifact仍是独立内容存储，此改动不等于全局脱敏/清除 |
| D08 | 每个Coding任务有TaskState；模型verified与memorize只提候选；无memory service不调用结论模型 | 模型工具无法伪造completed；确认依赖可信宿主API，没有新增用户审核UI；旧record_verified_conclusion明确报错 |
| D09 | 超时或未知/部分副作用不自动重试；只有not_started且worker已终止才可有限重试 | explore/plan去shell；parallel只读；禁未统一scope的BG/TEAM新启动/消息，保留idle/shutdown_response清理 |
| D10 | T09当前停在原创离线研究；小样本CPU规则/线性已执行，LoRA与部署No-go | 64合成/32家族单作者无真实流量与独立标签，尚不能通过§9；不写未执行的训练脚本/adapter来冒充闭环 |
| D11 | 一个根ExecutionBudget跨所有purpose和继承线程，provider实际attempt处记账 | 默认900秒/64模型attempt/128工具attempt/500000已知token；unknown和inflight单列。远端在途请求不能即时杀死，不把未知记零 |
| D12 | 禁SDK隐藏重试，路由fallback显式受根预算；协议、取消、budget和不可重试4xx不触备用 | 不合严格终态/结构/usage协议的兼容服务会显式失败；这是防止截断动作释放的代价，不声称测试所有服务商 |
| D13 | 摘要是未验证视图，硬约束/真实请求与TaskState覆盖模型摘要；保留前代lineage及原事件 | 真用户信息无法塞入预算时明确拒绝，不悄悄删约束；不引入语义判官或知识图谱 |
| D14 | 官方SWE结果必须绑定run目录/model/instance/patch SHA；异常、缺结果、畸形verification都不算pass | 本轮只测离线合同，不跑官方Docker/真实模型；provenance只锁18个关键源码及输入/config，不是完整依赖/提示词锁 |
| D15 | 不做数据库schema迁移；新证据ID按memory_id+source_id派生，兼容已有表 | 既有active记忆不被批量清除或重写；其真实性仍需用户将来审阅，未宣称历史数据已净化 |
| D16 | 保留基线失败与每次中途失败，按实现/夹具/环境分类；详细trace测试显式opt-in | 不关闭用例、不降低grader。原web删除mock改到真实read_sessions_page；Coding测试增加执行/验证/持久态分离断言 |
| D17 | 最终trace落盘可失败，但取消scope清理必须在finally执行 | 不吞掉OSError，不假装日志已成功保存；只保证本轮资源清理，不建立通用磁盘故障恢复系统 |

回退原则：按包同时回退生产者、消费者和测试；先保存本轮tracked diff及新文件清单，保留用户已有文档与untracked。不要使用全仓reset/clean，也不要为了回退恢复“错误当成功”、不受控后台写或模型自报验证。结构化trace与旧状态可读，但语义消费端必须按版本区分；不把新旧统计口径混算。
