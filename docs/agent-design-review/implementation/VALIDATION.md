# 验证与实验记录

日期：2026-09-27。**最终：项目 .venv 完整 896 passed / 0 failed / 0 skipped / 0 warnings。** pytest报告20.12秒；外部进程计时20.804秒（含启动），不能把两种计时混算。最后定向17项通过，随后完整套件覆盖同一最终源码。

## 环境与代码身份

| 项 | 实际值／边界 |
|---|---|
| HEAD / 分支 | 5bc28e8d883a6ab4daf7e8d734ca3a1bcfbe24a2 / feat/memory-runtime-evolution；源码未提交 |
| 最终源码 | [802条文件清单](evidence/final/source-manifest.json)，聚合SHA-256 301896082a2bf0bc9c30c7715296941ddf20ad52f54bec45829f50deae046f02；包括测试与研究，非整个用户工作目录 |
| 解释器 | /home/tale/kaggle/mytry/.venv/bin/python，Python3.12.4；最终guard记录sys.prefix为项目.venv，base_prefix为anaconda3 |
| 已有依赖 | pytest9.1.1、psycopg；研究sklearn1.9.0。原pyproject已有openai2.38.0等；本轮未安装依赖 |
| 临时数据库 | PostgreSQL16；新initdb；仅私有Unix socket；listen_addresses为空；shared_buffers=32MB、max_connections=20；独立schema随测试创建 |
| 模型环境盘点 | 已有torch2.12.1、transformers4.57.3、peft0.19.1、accelerate1.14，TRL缺失；RTX3060 Laptop6GiB，检查时约4.8GiB空闲；Qwen3-0.6B未缓存 |
| 实际资源使用 | 未下载模型、未用GPU训练/推理、未调用真实或付费provider；CPU线性训练是唯一模型拟合试验 |
| 数据保护 | git tracked源码+选定模块新增文件复制到/tmp，无真实.env；清空凭据环境、离线HF、Python socket audit拦网络、psycopg仅许可本轮socket；guard反例确认拒绝其他地址 |
| 隔离限制 | 不是OS沙箱，不能据Python审计声称隔离任意子进程网络/文件访问；本轮只跑已审查本地测试 |
| 清理 | finally pg_ctl stop=0；随后status=3（未运行），socket/pidfile不存在，见[清理证明](evidence/final/cleanup-verification.json) |
| 沙箱工具 | 默认bubblewrap报mountinfo path非绝对，使用权限审核批准的同范围读写/测试；没有审批拒绝或绕过 |

自制dotenv loader原不遵守PYTHON_DOTENV_DISABLED，现在runtime/env_loader.py与runtime/db.py均支持。tests/postgres_utils仍可找.env，因此隔离副本排除真实.env；测试自造临时.env的用例单独控制标志。没有连接既有DB或读取真实凭据文件。

## 基线、中途失败与最终结果

| 时点／证据 | 结果 | 解释与处置 |
|---|---|---|
| 纯HEAD git archive基线，[归档](evidence/baseline/full.txt) | 607通过、1失败、59跳过，13.82秒 | test_web_session_deletion误mock read_sessions，真实调用read_sessions_page；属历史测试隔离缺陷，修mock。59项当时缺DB；不当作通过 |
| 首次基线定向 | 文件名拼错退出4；更正后56通过 | 原/tmp日志保留，未将执行失败隐去 |
| root-targeted /tmp/taleclaw-implementation-7eowz8wz | 35通过、3失败、2跳过 | 旧详细trace测试与新默认metadata合同冲突；显式opt-in并新增默认隐私断言 |
| 中途 /tmp/taleclaw-integration-sdc0776s | 761通过、30失败、59跳过，17.02秒 | 未冻结并行实现；含memory夹具、schema空定义、完成/重试旧合同及真实root实现缺陷；分发修复，不算验收 |
| 中途 /tmp/taleclaw-integration-v8s49hnm | 799通过、1失败、59跳过，16.81秒 | 剩余旧详细trace失败摘要夹具，改显式capture_content |
| 独立WP04 /tmp/taleclaw-wp04-test-ythti0rz | 81通过、2DB跳过，9.91秒 | 评测、完整patch、根作用域原反例修复；后续PG全量覆盖跳过项 |
| 首轮临时PG /tmp/taleclaw-db-validation-ml8acclq | 893通过、2失败、0跳过，20.60秒 | 两个test_run_trace详细工件用例未opt-in；另加强execution/task/verification持久化分离断言；独立复核保留文件验收 |
| 第二PG轮 /tmp/taleclaw-db-final-s9tpm7cm | 895通过、0跳过、2第三方弃用警告 | 运行器resolve了venv符号链接，实际底层conda解释器；单列环境证据，不作为最终项目venv验收 |
| 最后清理P2 | 修前1失败，作者修后15通过 | trace落盘异常留下cancel token；修finally并保留OSError可见，独立复验 |
| 最终 /tmp/taleclaw-db-final-zsalome3，[日志](evidence/final/full-suite.txt) | **定向17通过；全量896通过，无失败/跳过/警告** | 保留.venv路径、guard证明prefix；最后两文件变更在[delta-manifest](evidence/final/delta-manifest.json)；完整套件重新覆盖 |
| 最终静态检查 | git diff --check、修改/新增Python的AST解析、源码摘要比对 | 无安装/编译副作用；最终记录见[完整性检查](evidence/final-integrity.json) |

历史计数不会合并成“累计成功率”。参数化单元/集成测试数量不是独立Agent任务数，更不是线上解决率。作者工具119通过、provider114通过等阶段结果仅作定位；最终验收以完整896为准。关键对抗反例及永久测试入口见 [REVIEWS.md](REVIEWS.md)。

## 可重复验证命令

在项目根目录、普通非root本地账号、已有Python环境与PG16二进制可用时运行：

    .venv/bin/python docs/agent-design-review/implementation/evidence/final/validate_isolated_pg.py --source . --python .venv/bin/python

此归档脚本会创建新的/tmp源码副本/私有数据库、执行guard smoke、17项定向与完整tests，并在finally停止数据库；不会安装依赖。每个测试phase超时上限180秒。源码快照不含未跟踪的旧设计目录、os、真实.env或本报告归档。PG路径可用--pg-bin指定；解释器路径必须保留.venv，不对其执行resolve。日志副本以.txt归档，避免项目忽略规则排除.log；原执行结果JSON和/tmp命令路径不改。旧路径如被清理，归档脚本仍可重新创建环境；实际执行命令、env与结果分别见：

- [运行环境](evidence/final/validation-environment.json)、[guard证明](evidence/final/guard-smoke.txt)；
- [定向结果](evidence/final/targeted-result.json)、[全套结果](evidence/final/full-suite-result.json)、[JUnit](evidence/final/full-suite.xml)；
- [整体与清理回执](evidence/final/validation-result.json)。

项目原开发方式仍是CONTRIBUTING.md中的uv环境及pytest；本轮没有执行依赖安装，也没有启动对外服务。不要直接拿真实DATABASE_URL跑测试来替代上面的隔离验收。

## 路由探索性实验

真实执行命令（输出必须为新路径，脚本拒绝覆盖）：

    OMP_NUM_THREADS=1 OPENBLAS_NUM_THREADS=1 .venv/bin/python research/hybrid_routing/baselines.py --output /tmp/routing-pilot-new.json

数据为本轮原创合成64条、中英两种语言变体、32个家族，单作者标签；train40/20家族、dev12/6、test12/6。家族不跨split，输入/ID去重，词表仅fit train，数据SHA为920d95b3748a6ced1110097ff7614e48d897216384ea3104d474e4d7f1973068。含常见敏感模式检查，不能视为完备脱敏；默认拒收未经审核真实来源。规则及TF-IDF字符2–4gram+LogisticRegression(C=1、seed=17、max_iter=500)在首次holdout前冻结，未按test调参。

| 集合（各12条/6家族） | 规则 macro-F1 | 线性 macro-F1 | 能说明什么 |
|---|---:|---:|---|
| dev | 0.9161 | 0.8333 | 此合成小样本上线性未胜规则 |
| test | 0.9161 | 0.8333 | 独立执行者复现相同逐题预测，不能推断真实分布 |

v1计时错误包含metrics，保留并明确不能使用其延迟；v2立即在predict后截表，四组预测与v1一致。v2 CPU线性fit约0.00648秒，test整批规则约0.0245ms、线性约0.7983ms；这包含输入/输出列表处理，是单次微小批次，不是生产P95。详见 [pilot-v2.json](../../../research/hybrid_routing/results/pilot-v2.json) 及 [研究说明](../../../research/hybrid_routing/README.md)。

**完成的是数据/校验/CPU线性流程及负向比较；未执行LoRA smoke、语言模型训练、同基座zero/few-shot、影子部署或真实入口配对E2E。** 依07 §9保持No-go，没有adapter，不报告节费、生产效果或语言模型微调成果。

## 尚未执行的验证

- 官方SWE-bench Docker与真实模型作答、真实路由流量/模型费用、服务商在线兼容性和真实渠道发送均未执行。
- 24个既有Coding scripted fixture在本地harness回归中验证确定性工具/验收行为；不替代24道真实模型成绩。
- 没有进行部署、迁移演练、长时间负载、多主机并发、跨进程崩溃自动续跑、任意shell沙箱逃逸或存储隐私全链审计。
- 所选精简主线适用的本地验收均完成；上述事项不作为本轮虚假通过项，有真实产品需求再立独立合同。
