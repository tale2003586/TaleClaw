# 独立对抗审查与关闭证据

日期：2026-09-27。基线 HEAD 5bc28e8d883a6ab4daf7e8d734ca3a1bcfbe24a2，加本轮未提交改动。最终源码清单见 [source-manifest.json](evidence/final/source-manifest.json)，聚合 SHA-256：301896082a2bf0bc9c30c7715296941ddf20ad52f54bec45829f50deae046f02。

**结论：选中范围内已确认的问题全部有处理结论，没有未解决的本次引入 P0/P1。** 独立审查由实际子 Agent 读取代码、运行反例完成；不是作者自证。以下“已复现”仅指安全本地测试/内存桩/临时repo，均不是线上事故。脚本化模型仅验证协议，不能证明模型质量。永久回归在项目 tests 内；临时原反例路径便于本次追踪，可能随 /tmp 清理而消失。

## 分工与最终门槛

| 实施者 | 修改范围 | 独立审查者 |
|---|---|---|
| root | 预算/取消/作用域/任务投影/trace/路由研究 | audit_eval_finetune；最终清理由 audit_memory_reliability |
| audit_arch_context | tools及记忆资格 | audit_memory_reliability |
| audit_memory_reliability | context及provider | audit_arch_context |
| audit_eval_finetune | evaluation与SWE导出 | root读取diff/调用链，audit_memory_reliability独立全量验证 |

审查按缺陷性质分 P0–P3，不把原设计中的高优先级当事故严重度。最终集成使用项目 .venv 和仅私有 Unix socket 的临时 PostgreSQL：896 passed / 0 skipped / 0 warnings，见 [完整日志](evidence/final/full-suite.txt)。

## 工具与根作用域

路径相对项目根；L 为最终工作区行号，关键符号便于后续代码变化后定位。

| ID／级别 | 位置、触发 → 失败路径 → 后果 | 证据等级及修复 | 关闭方法 |
|---|---|---|---|
| R01-1 P1 | tools/tool_registry.py L362 execute；schema=True提前返回 → 调用者_session/_mode进入handler → 宿主参数边界失效 | 独立probe已复现；宿主参数检查前置schema | test_tool_result_contract.py L198；原审查者复验handler调用0 |
| R01-2 P1 | tool_registry.py L412；取消/budget异常落broad except → 普通error → 停止语义丢失 | 已复现；typed控制异常交executor映射cancelled/timeout | test_tool_result_contract.py L207；独立复验 |
| R01-3 P1 | tools/hooks.py L268 reset_turn；外部改文件后新回合 → session缓存仍旧 → 错称文件未变 | 已复现；新回合及受管写后同时失效两层缓存 | test_tool_result_contract.py L215、L152；真实临时文件读改读 |
| R01-4 P2 | tool_registry.py L893 _validate_tool_arguments；1000位整数 → float溢出 → 未执行却effect_unknown | 已复现；整数不强转浮点 | test_tool_result_contract.py L228；正常大整数及maximum越界均测 |
| R02-1 P1 | runtime/workspace_lease.py L18；父/子workspace分别锁 → 可同时改同文件 | 独立实际复现；改每OS用户全局writer；增加不同repo串行成本 | test_execution_budget_scope.py L130；原审查者复验 |
| R02-2 P1 | agents/subagent/parallel.py L98、budget.py L61；future超时但线程仍活 → root退出后再调模型 → 继续花费 | 独立可控线程已复现；timeout.stop、owner.close、claim锁内复查 | test_execution_budget_scope.py L153、L176；原反例闭合，在途用量仍unknown |
| R02-3 P1 | applications/coding/runner.py L122、trace_store.py L295；直接Coding入口有失败fallback/辅助调用 → 根用量未保存 → 看似只主调用成本 | 独立已复现；独立入口finally写根snapshot，嵌套不提前close | test_execution_budget_scope.py L187、L210；原probe和最终PG全套 |
| R02-4 P1 | tools/handlers.py L2547 _update_task_state；模型清pending+伪basis+completed → 未验收任务变完成 | 四mode实测；coding_application在ensure/reduce/save前拒绝 | test_tool_result_contract.py L240起6例，独立PG全部通过；普通bot兼容保留 |
| R02-5 P2 | applications/turn_coordinator.py L92 finally；最终trace写OSError → 跳过取消token释放 → 留下已结束scope | root实际复现；清理进入内层finally，不吞OSError；独立者读取diff并复验 | test_execution_budget_scope.py L222；修前1failed、作者15passed、独立17passed及最终896全套 |
| I02-1 P1 | Coding runner前置trace取尚未生成execution_status → KeyError → Coding无法开始 | 中途集成已复现；移至结束投影 | 中途失败保留；后续Coding数据库集成与最终全套通过 |
| I02-2 P1 | runner.py L524 _format_parent_reply；候选非空 → 未定义sections → 交付NameError | 独立已复现；改用实际lines并测试待确认文案 | test_execution_budget_scope.py L139；原probe闭合 |
| I01-1 P2 | tool_registry.py L824 _with_tool_facts；只读path被拒 → unknown → 主循环误当写副作用停止 | coding-path-escape-006真实fixture复现；已声明只读错误no_effect，写unknown不变 | path escape恢复fixture、test_tool_result_contract.py L183/L189 |

原工具反例：/tmp/wp01_independent_probe.py；进程对抗：/tmp/wp01_command_probe.py。忽略SIGTERM的受管子进程被KILL，无延迟写；取消查询异常也清理组。**这不覆盖setsid逃逸，不等于OS文件/网络沙箱。**

## 上下文与模型协议

| ID／级别 | 位置、触发 → 后果 | 修复及证据 | 关闭方法 |
|---|---|---|---|
| R03-1 P1 | applications/coding/context_state.py _source_requests；长补充消息预览后半含新约束 → 原请求未投影 → 模型看不到 | /tmp/wp03_adversarial_probe.py已复现；真实请求保全，原事件可达 | test_context_continuity.py L283；独立原probe闭合 |
| R03-2 P1 | runtime/context/events.py L34；subagent/reflection伪user标objective_change → 改目标 | 同probe已复现；explicit origin仅user/human，旧无origin兼容，runtime来源排除 | test_context_continuity.py L299、L154；独立闭合 |
| R03-3 P2 | runtime/context/snapshots.py EventCompactor；普通assistant全部protected累加 → 20条撑爆压缩 | 同probe已复现；普通叙述不永久保护，硬约束/refs保留 | test_context_continuity.py L308；独立闭合 |
| R03-4 P1 | snapshots.py ContextSnapshotManager.activate L377；checkpoint-only旧快照 → 先换active后归档失败 | 同probe已复现；父指针/版本/归档边界在任何变更前检查 | test_context_continuity.py L318；失败后active与边界不动 |
| R04-1 P1 | models/provider.py L259 stream_chat；finish_reason=stop后又工具delta → 释放不合法动作 | /tmp/provider_independent_probe.py已复现；终态后choice拒绝并close | test_provider_contract.py L251；closure_probe与90项独立定向通过 |
| R04-2 P1 | provider.py L728；Responses中间write_file，最终output=[] → 从缓存复活写调用 | 已复现；最终对象必须确认中间tool，终态后delta拒绝 | test_provider_contract.py L272/L283；原反例闭合 |
| R04-3 P2 | provider.py L766、model_pool.py L472；缺结构字段 → AttributeError当传输故障 → fallback且污染健康 | 已复现；required_field抛ModelProtocolError，分类不重试 | test_provider_contract.py L300；备用调用0/健康失败0 |
| R04-4 P2 | provider.py L628 _usage_from_response；total=0但in+out=1000 → 记精确零usage | 已复现；矛盾usage拒绝，unknown记一次，不复用先前片段 | test_provider_contract.py L315/L323；独立闭合 |

R04编号来自provider审查，不等于工作包WP04。模型协议严格化保留明确失败，不能用任意mock终态当真实服务兼容性证明；本轮未访问真实provider。

## 记忆、评测和研究

| ID／级别 | 位置、触发 → 失败后果 | 修复／证据等级 | 关闭方法 |
|---|---|---|---|
| R05-1 P1 | conclusions.py L152、promotion.py L120；模型自报verified或memorize → 未经宿主确认的active | 静态调用链确认，行为回归验证；parse强制false、仅propose；run_memorize使用INFERRED | test_memory_verification_boundary.py L40/L147；独立probe和最终PG |
| R05-2 P1 | memory/command_service.py L187；candidate exact merge/semantic supersede → 污染active证据/替换active | 候选重复与冲突反例已测；候选只能合并同scope候选 | test_memory_verification_boundary.py L77/L90/L100；独立复核 |
| R05-3 P2 | command_service.py L80；expired条目重复提议或confirm → 过期记录复活 | 回归已测；过滤expired exact，拒过期确认 | test_memory_verification_boundary.py L109/L123 |
| R05-4 P2 | command_service.py L297 _bind_evidence；确认后同源重提 → 全局evidence PK ON CONFLICT → 新候选无证据 | 严格fake表和真实_insert_evidence函数组合修前复现；按memory_id+source_id派生PK | test_memory_verification_boundary.py L178；独立/tmp/wp05_independent_probe.py，fakeSQL不冒称真实DB复现 |
| R06-1 P2 | research/hybrid_routing/baselines.py L90；计时到metrics之后 → F1计算被当预测耗时 | 独立实测/读码；计时在predict后立即截取，v1保留并标无效时延 | v2预测与v1逐项相同；独立复跑；不当P95 |
| R06-2 P2 | hybrid_classifier.py L64；mode=[]参与set成员检查 → TypeError → 误归provider_error | 已复现；先校验str，invalid_output回退 | test_routing_decisions.py L23；独立5passed/15deselected，最终全套 |
| E04-1 P1防线 | evaluation/outcomes.py L36、matrix汇总；畸形或未知verification → 可能被漏计并使矩阵误绿 | 最终静态审查发现缺口、补离线反例；不合法结果一律inconclusive | test_evaluation_outcomes.py L244；3参数例及最终全套 |
| I04-1 P2测试 | tests/test_run_trace.py L725/L1028；默认metadata却读workspace_diff、旧completed含混断言 → 集成失败 | 临时PG真实2失败；两个详细诊断用例opt-in，新增active/not_run/持久一致性，原文件断言保留 | 独立认可非降断言；最终两项定向及896全套通过 |

WP04另外验证：空patch/生成完成不计pass；官方同instance不同model/patch拒绝；verifier异常ledger不断行；临时Git index导出staged/new/deleted/binary并在干净repo重放；未知usage为null。核心回归见 test_evaluation_outcomes.py、test_swebench_patch_export.py。主Agent实际检查实现及官方源码，不仅转述作者结论。

## 保留的限制，不伪装成已关闭功能

- 远端已在途请求无法由本地context即时中断；结束时unknown/inflight明确保留。按attempt/deadline阻止新增工作，不承诺精确账单。
- 通用shell的OS隔离、外部副作用恰好一次、崩溃自动恢复、用户编辑冲突检测、跨实例TaskState CAS没有本轮实现。
- 详细诊断显式开启后可能含私有内容；Session和task artifacts不在TraceStore脱敏保证内。
- 记忆审核UI、既有active数据重审、跨用户共享产品策略没有顺带改造；候选资格修复不证明历史记忆正确。
- 小数据、单作者标签、没有真实provider/正式SWE成绩；No-go不是“微调失败”或“规则已适合生产”的证明。

这些是本轮范围/证据边界，不是用延期掩盖新增P0/P1。后续只有真实需求和明确验收才扩展。

最终文档独立核对：audit_eval_finetune直接核对JUnit、guard和研究v2，确认896/17项计数、895底层环境区分及LoRA/SWE/E2E未执行表述；STATUS中“unknown非零费用”已纠正为未知保持null和已知下界单列，避免把未知断言为已知非零。
