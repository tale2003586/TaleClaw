# 整体设计改进：实施交付报告

日期：2026-09-27。**[07-master-plan.md §8.2](../07-master-plan.md) 推荐精简主线已实施并通过适用验收。最终项目虚拟环境完整测试 896 项通过，0 失败、0 跳过、0 警告；独立对抗审查中成立的问题已修复并复验。** 没有提交、推送、发布或部署。

本结论覆盖可信本地、个人/小团队的最小可靠性闭环，以及必要记忆资格修复、路由观测和原创离线研究。**不表示 §8.3 全部扩展架构、真实模型效果评测或语言模型微调完成。** 本轮没有安装依赖、下载模型、使用GPU训练、调用付费模型、访问既有数据库或上传私有代码/轨迹。

## 1. 版本、授权与证据

- 基线/当前HEAD：5bc28e8d883a6ab4daf7e8d734ca3a1bcfbe24a2；分支 feat/memory-runtime-evolution。全部实现位于未提交工作区。
- 开始时 tracked 无修改；原未跟踪 docs/agent-design-review/、docs/system-design (2)/、os 保留。六份专项及07原报告未改；当前用户附件明确授权实施，替代前轮只读约束。
- 已读README、CONTRIBUTING、pyproject、原方案及核心调用链；未发现适用AGENTS.md。
- 最终测试快照为802条文件记录，聚合SHA-256：301896082a2bf0bc9c30c7715296941ddf20ad52f54bec45829f50deae046f02。见[逐文件清单](evidence/final/source-manifest.json)及[交付完整性检查](evidence/final-integrity.json)；这不是新Git commit，也不是用户整个目录的内容备份。
- 作者、独立审查者、验证者轮换且文件归属分开；root检查实际代码、差异、反例及外部协议依据。详细记录见[状态](STATUS.md)、[决策](DECISIONS.md)、[审查](REVIEWS.md)、[验证](VALIDATION.md)。

## 2. 实际交付的功能和行为变化

| 模块 | 已实现行为 | 使用者能观察到的变化／主要代码 |
|---|---|---|
| 工具结果 | status、error、exit_code、effect_state贯通；schema在执行前校验；宿主参数不可由模型覆盖；保留字符串adapter | 无输出exit 7也返回失败；非法调用不进入handler。tools/executor.py L33/L125，tool_registry.py L346，handlers.py |
| 本地命令 | 有界输出、截止时间、取消检查；终止受管进程组并确认状态 | 超时/取消不冒充成功；无法证明效果时保留unknown；tools/command.py L80 |
| 根运行预算 | 根作用域贯穿分类、主模型、压缩、结论、fallback；线程继承同一预算，结束后禁新增attempt | 超时worker醒来不能再申请模型；缺usage明确unknown/inflight。runtime/execution/budget.py L29，Runtime.run、TurnCoordinator.run_inbound |
| Provider协议 | 最终完整请求gate含schema/output reserve；坏JSON、缺结构、截断终态、迟到delta明确失败；SDK隐藏重试关闭 | 不把不完整工具参数释放执行；不可重试故障不走备用；models/provider.py L119/L482、model_pool.py |
| 工作区与委派 | canonical workspace绑定工具和根规则；跨进程单writer；explore/plan去shell；并行只读；禁未统一scope的BG/TEAM启动路径 | 选B仓库使用B根规则；重叠目录不能同时由受管Coding写；runtime/workspace_lease.py L18、applications/coding/runner.py L109、tools/policy.py |
| 任务状态 | Coding总有TaskState；执行结束、任务语义和验证状态分离；模型更新工具不能伪造completed | 最终回复后默认execution=completed、verification=not_run、展示awaiting_verification；runtime/task_state/service.py L33，tools/handlers.py L2547 |
| 上下文 | 宿主origin判定；真实目标/请求与TaskState硬约束优先；前代lineage、观察预览和原事件refs连续；快照激活先验证 | 三代压缩仍能保约束与证据；状态问句/伪user不覆盖目标；撑不下时明确失败。events.py L34，snapshots.py L181/L377，coding/context_state.py |
| 缓存及有限恢复 | 写后及跨回合读缓存失效；unknown/partial副作用不自动重试；只有not_started且worker终止才可有限重试 | 外部改文件后下一轮读新值；取消同一批次后续工具不再执行。tools/hooks.py L268，agents/subagent/retry.py |
| 轨迹 | trace.v2/event_id；默认元数据；详细内容显式opt-in；总usage未知不填0；根已知消耗与主调用分列 | 常规TraceStore不复制正文/完整diff；辅助调用/失败fallback进入根统计；trace_store.py L23/L185/L295 |
| 评测 | execution/verification分开；grader异常保留逐题行；官方结果绑定run/model/instance/patch SHA；完整允许patch导出 | generate-only不能算solved；staged/new/deleted/binary可在干净repo重放；evaluation/outcomes.py L36/L46、swebench_adapter.py L541 |
| 记忆资格 | 模型只提candidate；宿主显式确认决定active；候选不污染active；过期不复活；同源多候选证据ID不冲突 | memorize结果提示待人工确认；关闭记忆不调用可选结论模型；memory/command_service.py L41/L187/L297、coding/promotion.py L120 |
| 路由观测与研究 | 单次不可变RouteDecision，无共享last_result；记录门控、可见前缀、fallback和usage；原创分组数据与CPU基线可重跑 | 原权限/模式/gate保留；非法输出安全回bot，取消/budget继续停止；hybrid_classifier.py L20/L102、research/hybrid_routing/ |

核心入口链接：[工具执行](../../../tools/executor.py)、[根预算](../../../runtime/execution/budget.py)、[Coding应用](../../../applications/coding/runner.py)、[上下文快照](../../../runtime/context/snapshots.py)、[Provider](../../../models/provider.py)、[评测结果](../../../evaluation/outcomes.py)、[记忆服务](../../../memory/command_service.py)、[路由研究](../../../research/hybrid_routing/README.md)。行号对应交付快照，以符号定位为长期依据。

### 已连接的职责边界

    入口/AgentRouter → CodingApplication 或 Runtime → AgentRunner/ReasoningLoop
            │                 │                              │
            └────── 共用根预算、取消、可信会话来源 ─────────────┘
                              │
            TaskStateCore → 上下文视图/连续快照 → Provider
                              │                    │
                    原事件及artifact引用       工具调用结果
                                                   │
                                  Registry/Policy/Executor
                                                   │
                                  单writer工作区/受管命令
                              │
                    TraceStore元数据 → evaluation独立验收
                              │
                    模型结论 → candidate → 可信宿主确认

TaskState不被摘要、trace、记忆或UI另建一套进度覆盖。独立评测的verification是证据维度；普通Coding应用本轮仍默认not_run，没有偷偷自动把本地“测试成功”文字写回verified。

### 具体合同例子

| 输入/触发 | 交付后结果 |
|---|---|
| bash无输出且退出码7 | error、exit_code=7；不再把“无输出”当success |
| 同次回复有两个写工具，第一个后收到取消 | 第二项不调用handler，产生cancelled/not_started工具记录，避免半截协议再触发新写 |
| 模型说“完成，已验证”，没有宿主验收 | Coding执行可结束，但verification=not_run，任务不由该句完成 |
| Responses中间出现write_file、最终completed.output=[] | ModelProtocolError，不从流缓存恢复未经最终确认的工具 |
| 第二/三代压缩遇到“不能改测试” | 原真实请求/TaskState约束继续投影，摘要无权取消；不足预算则拒绝压缩/请求 |
| 官方报告instance相同但patch不同 | inconclusive，不能借用旧报告算pass |
| provider缺usage或返回矛盾usage | 总tokens保持未知；已知下界单列，不宣传免费/节费 |
| 最终trace写盘抛OSError | 异常仍可见，取消令牌和根context仍释放 |

这些行为由本地反例和回归验证；没有据此声称实际线上故障已消失或真实任务解决率上升。

## 3. 与原方案的取舍及兼容性

1. **选择精简主线而非全量架构。** 07 §8.2明确允许先收敛主shell/文件路径并关闭不受控分支。本轮沿可信session、规范化workspace与根budget连接边界，没有新建统一大平台、第二TaskManager或工作流框架。
2. **单写者比原按路径锁更保守。** 独立反例证明父子目录可重叠，采用每OS用户的一个writer；不同仓库也被串行。这是小团队可维护的权衡，不是高并发调度设计。
3. **验证首版保守。** 修正工具/评测事实、阻止Coding自报完成；未建设适用于所有任务的宿主VerificationPolicy、内容依赖失效及TaskState验收写回UI。该扩展需要确定产品验收合同，不能声称已具备。
4. **记忆做资格修复，未默认自动学习。** 现有基础查询/确认/撤销接口保留，无向量不成为基础读取前提；没有新增Coding自动检索装配、审核UI或共享owner迁移。旧record_verified_conclusion不安全捷径现在明确报错；第三方调用方须改用propose/可信确认。
5. **日志默认内容最小化。** TraceStore新增capture_content=False；原详细诊断用例显式True并保留文件断言。Session数据库和应用task artifacts仍可能存内容，不能把此变更理解为全系统自动去敏。
6. **Provider与工具更严格。** 未支持schema、宿主参数注入、坏终态/usage会显式失败；部分“OpenAI兼容”服务需要适配。模型pool重试现在受可观察attempt控制；工具字符串返回仍兼容。
7. **微调停在No-go关卡。** 主方案明确允许原创离线研究后不训练。缺真实授权分布、独立标注与收益证据，不用一次合成LoRA训练填展示栏，也未加不可验证的部署模块。
8. **无数据库schema迁移。** 新字段使用既有元数据/结果结构，记忆证据使用不同ID；没有重写或删除已有记忆。既有active内容真实性不在本轮自动追溯保证内。

所有重要决策与代价见 [DECISIONS.md](DECISIONS.md)。禁用的新后台/TEAM路径不会因回退配置自动恢复；保留idle/shutdown_response只用于清理。

## 4. 测试、独立审查和实验成绩

| 验证 | 真实结果 | 可以说明／不能说明 |
|---|---|---|
| 纯HEAD基线 | 607通过、1历史mock错误、59 DB跳过 | 记录已有失败；不能声称基线数据库通过 |
| 最终项目.venv + 临时PG16 | **896通过、0跳过、0警告**，pytest20.12秒；最后定向17通过 | 覆盖既有测试及新增工程行为；不是896道真实模型任务 |
| 独立对抗 | 参数越权、取消吞没、陈旧缓存、父子锁、迟到线程、流终态、快照失效、记忆污染等成立反例均闭合 | 有具体输入/失败路径/修复/回归；不是全仓无缺陷证明 |
| 24个Coding scripted fixture | 本地harness回归通过，包含文件操作和verifier | 预设模型动作的系统回归；没有真实模型作答成绩 |
| 原创路由数据 | 64条/32家族；train40、dev12、test12；无跨家族划分泄漏 | 流程和对照可复现；单作者合成不能代表真实流量 |
| CPU规则与字符线性 | dev/test规则macro-F1均0.9161，线性均0.8333；独立逐题预测重现 | 线性在此pilot未胜规则；不是规则上线有效性、LoRA失败或E2E结论 |
| 真实LLM、LoRA、官方SWE Docker、上线 | **未运行** | 不填写解决率提升、GPU训练成绩、生产P95或费用节省 |

独立审查不是形式签字：例如重叠工作区锁、超时线程继续调模型、独立Coding入口遗漏fallback用量、候选文案NameError均由非作者实际复现，再交作者修复；上下文与provider各四项原反例复验；最后PG旧夹具变更由独立者确认未降低断言。

最后清理P2在root本地先复现失败，再修复并交独立者验收；最终完整896包含该补丁。中途761/30、799/1、893/2等失败及底层解释器895结果均保留分列，不能把不同环境结果拼成一个“全绿”。见 [REVIEWS.md](REVIEWS.md) 和 [VALIDATION.md](VALIDATION.md)。

## 5. 小模型关卡的最终结论

**No-go：当前不运行LoRA、不部署规则或线性替换器，保留现行路由。**

选择路由的理由仍是窄接口、无工具权限、可冻结输入输出且成本可比较；它不意味着当前瓶颈已经被证实。实际补齐了门内观测、可见前缀/截断信息、不可变结果、家族划分、授权标识、去重与便宜基线；没有把业务规则/gate和新模型一起改掉。

当前数据仅原创合成，test只有6家族；连是否有足够门内流量和可学误差都未知。CPU线性输给规则，不能推出LoRA也会输，但足以否定“先训练就有收益”的默认假设。v1含metrics的预测耗时作废且保留；v2仅修计时，未按holdout重新调模型。未做同基座zero/few-shot、LoRA smoke、正式训练、影子接入或完整入口配对E2E，也没有权重或adapter产物。

下一次进入训练前，必须有明确授权的真实分布、独立标签和固定holdout，测门内调用量/失败类型/全费用，并完成规则、线性、提示词、同基座未微调对照。错误若来自gate、截断、缺历史，先修信息；若便宜方案足够或总成本无空间，继续停止。只有离线相对最佳便宜基线可接受、权限合同通过、完整入口质量和净收益达预登记门槛，才考虑影子及小范围启用。详见[研究说明及命令](../../../research/hybrid_routing/README.md)。

## 6. 运行、验证、回退和后续入口

应用启动方式未改：README已有CLI的python cli.py，以及Web的python web/server.py --host 127.0.0.1 --port 8000，使用已有本地配置。**本轮未启动对外服务或验证真实渠道/provider**。无需本轮新迁移或新运行依赖。

推荐验收命令（仓库根，已有环境，普通非root账号）：

    .venv/bin/python docs/agent-design-review/implementation/evidence/final/validate_isolated_pg.py --source . --python .venv/bin/python
    OMP_NUM_THREADS=1 OPENBLAS_NUM_THREADS=1 .venv/bin/python research/hybrid_routing/baselines.py --output /tmp/routing-pilot-new.json

第一条使用PG16现有二进制、全新/tmp与Unix socket，清空配置环境且finally停库；第二条不联网、拒绝覆盖已有结果。原测试环境、命令、JUnit、脚本、源摘要和停库回执均已[归档](evidence/final/validation-result.json)。归档不含完整临时数据库/源码副本；/tmp原目录仅留作本次排查，不是永久依赖。

默认根限额为900秒、64模型attempt、128工具attempt、500000已知token。宿主可显式构造ExecutionBudget后使用execution_scope；这不是费用上限承诺，usage未知及在途请求另列。Provider单次timeout至多60秒且不超过根剩余deadline；受管shell默认120秒并受根deadline限制。详细trace仅在可信诊断/隔离评测中显式开启capture_content=True。

回退不自动执行：先保存本轮tracked diff及新文件清单，再按WP同时回退接口生产者/消费者/对应测试，保留原报告及用户untracked。没有schema迁移需要反向数据操作；不要运行全仓reset/clean，不要把旧“completed=verified”、未知费用=0、未受控后台写重新开放来获得兼容。回退后仍运行相应合同测试；研究目录可独立保留，未接入线上默认。

后续只有以下明确需求才扩展：

| 条件 | 下一实施包与前置 | 当前状态 |
|---|---|---|
| 需要在产品内完成宿主验收 | 定义任务合同、受信检查及编辑后失效；接TaskState只读投影/确认UI | 扩展路线未做，不阻塞本轮最小状态分离 |
| 需要跨会话实际复用 | 用户审核UI、共享scope授权、Coding自动读取开关；先测有/无记忆消融 | 默认不自动晋升；无收益不启用 |
| 有足够真实路由流量/标签 | 固定数据和模型环境后再做同基座对照/一次LoRA smoke与正式实验 | No-go，尚无进入条件 |
| 需要多用户/并行写/不可信代码 | 明确OS隔离、用户联合owner、CAS/冲突检测与审计，再开放对应路径 | 不在可信本地单writer保证内 |
| 需要中断后自动续跑 | 工具效果账本、人工核对unknown、外部服务幂等合同 | 没有通用恢复承诺 |

当前没有阻止已选主线完成的外部阻塞。远端在途请求无法即时终止、进程组不构成OS沙箱、外部编辑不受writer锁控制、旧active记忆未审计、评测manifest仅有限源码/配置锁，均为明确边界。

## 7. 可诚实展示的成果

可用表述：

> 为TaleClaw现有Agent运行时补齐结构化工具结果、根预算与取消控制、工作区单写、连续上下文、记忆候选资格及独立评测证据链；通过独立对抗审查修复终态工具释放、父子目录并写、超时后继续调用等反例，最终在隔离PostgreSQL16和项目虚拟环境中通过896项本地测试。

> 构建64条原创合成、32个意图家族的离线路由试验与可重跑基线；固定12条测试集上规则macro-F1为0.9161、字符线性为0.8333，依据数据规模与收益证据选择不部署、不开展LoRA。

面试展示可以按“实际调用链→一个已复现反例→修复后回归→代价和限制”讲解，配source manifest与测试日志。不得写“微调提升Agent成功率”“降低总费用20%”“生产服务百万请求”“已上线”，也不能把已有896项全部称为本轮新增测试。

若以后获得真实实验，另填：授权任务集[名称/版本/样本量]、独立家族数[N]、基线/候选验证率[值及区间]、每千总请求净费用[值]、入口P95[值]、错误/回退率[值]、运行环境/模型版本[值]。当前这些指标均未测，不填数字。

## 8. 资料与证据所支持的结论

- [07主方案 §8.2/§9](../07-master-plan.md)（本地2026-09-27）：支持选择缩减主线、条件性路由实验、允许No-go；它不是本轮测试证据。
- [最终源码清单](evidence/final/source-manifest.json)、[JUnit](evidence/final/full-suite.xml)、[环境与清理](evidence/final/validation-result.json)（2026-09-27）：绑定本轮源码、896项本地通过和临时DB已停；不支持真实模型质量结论。
- [SWE-bench官方run_evaluation.py](https://github.com/SWE-bench/SWE-bench/blob/main/swebench/harness/run_evaluation.py)、[grading.py](https://github.com/SWE-bench/SWE-bench/blob/main/swebench/harness/grading.py)（访问2026-09-27）：公开代码中run/model/instance目录、report.json、patch.diff及resolved口径支持本地官方结果导入合同。访问main时未取得可靠固定SHA，不把动态页面当已锁定版本；本轮未运行官方harness。
- [scikit-learn官方TF-IDF文档](https://scikit-learn.org/stable/modules/generated/sklearn.feature_extraction.text.TfidfVectorizer.html)（访问2026-09-27）：支持字符特征及fit/transform接口；实际安装1.9.0和参数以研究脚本/结果为准，文档当前版本不替代运行锁。
- [路由manifest](../../../research/hybrid_routing/manifest.json)、[v1](../../../research/hybrid_routing/results/pilot-v1.json)、[v2](../../../research/hybrid_routing/results/pilot-v2.json)：支持原创合成来源、固定划分、负向比较及计时纠错，不支持泛化/上线/微调效果。

尚未确认：真实Hybrid流量和收益空间、真实provider兼容性、实际长期记忆价值、多用户部署要求、正式SWE成绩。与后续任务的接口：扩展验收消费verification、路由研究消费RouteDecision和授权数据、记忆UI消费candidate/confirm、恢复消费effect_state和根停止事实。需要未来产品裁决的争议：是否接受用户级串行成本、是否建设验收/记忆UI、何种真实收益足以开启训练。本轮已有明确决定，不以这些未来问题阻塞已完成的精简主线。
