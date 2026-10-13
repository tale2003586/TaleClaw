# 06｜评测、运行轨迹与可观测性设计审查

> 审查日期：2026-09-27（Asia/Shanghai）。对象：TaleClaw 当前工作区。本文只提供代码审查、公开资料调研和设计，不代表已经实施或跑出新的评测成绩。

## 1. 结论与审查边界

**建议保留现有 `TraceStore + evaluation/harness.py + matrix`，先统一验收口径、实验清单、事件关联和脱敏，再扩展评测集。** 当前最缺的不是另一个 Agent 框架或观测平台，而是把“运行结束”“行为正确”“有证据证明正确”分开，以及让两次实验确实可比。

最小优先级如下：

1. **P0：修正结果语义。** `completed` 仅表示运行生命周期结束；单列 `verification.outcome`。SWE-bench 必须读取官方逐题结果，不能把生成过程结束率叫解决率。恢复后的错误留在 incident 中，不直接覆盖最终验收。
2. **P0：固定实验与数据边界。** 保存代码、未提交补丁、任务、fixture、提示词、工具 schema、解析后配置和环境的版本摘要；测试集与 Agent 可访问的目录、记忆、检索库隔离。
3. **P0：补齐最小轨迹。** 在现有事件上增加版本、稳定事件 ID、调用 ID、父子运行关联、实际模型、未知计量标记及独立验收结果。默认只记录经过脱敏的元数据和受控引用。
4. **P1：以现有回归测试为底，建设 48 个分层逻辑案例。** 先完成无需真实模型的契约与故障注入部分，再按预算比较有随机性的模型行为；不能把 scripted 成绩当模型能力。
5. **有收益后才扩展。** 公开基准用于外部有效性抽查；训练数据需独立筛选、授权、标注和防污染。当前不建议先引入向量化轨迹库、全量原始对话采集、分布式事件平台或 LLM 自动打分闭环。

### 1.1 版本、工作区与检查方法

| 项目 | 本轮事实 |
|---|---|
| Git HEAD | `5bc28e8d883a6ab4daf7e8d734ca3a1bcfbe24a2` |
| 最近提交 | `2026-08-18`，`harden runtime handler and embedding sharing` |
| 审查开始时未提交状态 | 无已跟踪文件修改；未跟踪 `docs/system-design (2)/`、`os`。未覆盖、删除或执行这些内容 |
| 审查对象 | 该 HEAD 的源码与配置模板；另只读盘点被 Git 忽略的历史运行/评测产物。历史产物不自动归属于此 HEAD |
| 项目说明 | 阅读 `README.md`、`pyproject.toml`、`pytest.ini`、`docs/overview/PROJECT_STRUCTURE.md`、`docs/2026-06-12-agent-eval-plan.md`；实现证据优先于文档表述 |
| `AGENTS.md` | 检查根目录及祖先路径、`.agent/.agents/.codex` 和本任务相关源码/文档目录，未发现适用文件；三个本地规则目录没有规则文件 |
| 低成本检查 | Git 状态、文件列表、带行号源码阅读；标准库 AST/JSON 静态解析统计；历史文件元数据及少量结构化汇总字段；官方网络资料 |
| 未执行 | pytest、评测 harness、模型调用、Docker 基准、依赖安装、训练、业务代码改造、提交与推送。没有本轮“已复现”故障或新基线分数 |
| 本轮写入 | 仅 `docs/agent-design-review/06-evaluation.md`；写入前确认同名文件不存在 |

交付前再次检查时，`docs/agent-design-review/01-architecture.md`至`05-finetuning.md`已由并行工作出现；本任务没有修改这些文件，已跟踪业务文件仍无差异。

默认执行沙箱出现 `mountinfo path is not absolute`，只读检查通过经审批的沙箱外命令完成；未因此运行应用或访问真实 `.env` 内容。

**证据标记：**“事实/已实现”指读取到执行代码或已有产物；“部分实现”指机制存在但不能支撑完整结论；“只有文档或接口”不计为能力；“未发现实现”限定于本文列出的审查范围。“推演”是由代码导出的触发路径，不表示发生过线上事故。“建议”和“待验证假设”是未来工作。下文代码行号均对应上述 HEAD；相对路径以项目根目录为准。

## 2. 当前资产与能力盘点

### 2.1 已有实现及其实际边界

| 编号 | 能力、状态 | 实际证据（路径、符号、行号） | 能证明什么／不能证明什么 |
|---|---|---|---|
| C01 | Python/前端测试：**已实现** | `pytest.ini:1–12` 指定 `tests`；静态 AST 统计 `tests/test_*.py` 为 **118 文件、653 个 test 函数**，不含参数化展开；`web/frontend/package.json:6–11` 有 Vitest/typecheck/lint | 有广泛回归资产；本轮没运行，不能宣称全部通过、覆盖率或真实模型成功率。普通测试仅由 `tests/conftest.py:4–7` 自动关闭共享 trace index，不能据此认定所有外部 I/O 均关闭 |
| C02 | Coding fixture harness：**已实现** | `evaluation/task_schema.py:9–33,36–79`；`evaluation/harness.py:201–235,283–343`，`CodingBenchmarkHarness.run_task`；`benchmarks/coding_tasks.json:5–66` 为首个具体任务 | **24 个任务**，复制真实 fixture、构造 CodingApplication、运行工具并验收。`scripted` 使用给定答案脚本（harness `357–363`），检验框架执行，不检验模型会不会解题 |
| C03 | 结果检查：**部分实现** | `evaluation/verifiers.py:10–118`，`verify_task`；harness `321–327` 综合 runtime、预算、verifier、diff、trace | 已执行命令、检查修改/创建/禁止修改、内容、工具与事件；并非所有评测只看 completed。缺少独立隐藏行为测试、完整允许路径约束、可恢复事件与最终结果的统一语义 |
| C04 | 矩阵/重复实验：**部分实现** | `scripts/run_coding_agent_matrix.py:156–168,301–332,434–441,490–500,639–662`：保存配置/展开计划，展开 repetitions，子进程环境覆盖、stdout/stderr/command、缺失 metrics 数 | 真正支持重复与消融配置；继承宿主环境。未发现统一代码/数据/prompt/环境内容哈希、随机性说明及配对置信区间。不能称“没有实验基础设施” |
| C05 | system eval 兼容入口：**接口存在但部分参数无实现** | `scripts/run_system_evals.py:38–42,50–57,60–69` | `--runner` 默认 scripted；`--context-repetitions/--memory-repetitions/--recovery-repetitions/--skip-ablations` 被解析后不参与执行。不能通过传这些参数获得重复实验 |
| C06 | SWE-bench 适配：**部分实现** | `evaluation/swebench_adapter.py:248–415,507–515,535–563` 的运行、patch 导出与官方 harness 调用；`scripts/run_swebench_verified.py:128–164,221–227,275–294` | 已有真实 issue→patch→官方 harness 接入；但调用后未回读官方逐题resolved更新摘要，agent pass不是官方resolved。`requirements-swebench.txt:1` 只固定 `datasets==5.0.0`，不足以锁定官方 harness/镜像版本 |
| C07 | 独立 smoke/RAG：**已实现但指标作用有限** | `evaluation/coding_agent_smoke/run_smoke.py:135–155,303–325,393–442`；两份 `benchmarks/security_rag*_testset.jsonl`；`scripts/eval_security_rag_v2.py:111–181` | 静态读取分别为 **30、100、250 条**。RAG 有路由/召回代理指标，但命中术语不等于答案正确、安全修复或长期记忆有效；这些数据不是现成微调集 |
| C08 | 结构化轨迹：**已实现** | `runtime/trace/events.py:7–20`，`TraceEvent`；`runtime/trace/trace_store.py:40–117,139–174`，`start_run/append_event/write_report` | JSONL、run_state/report/metrics/context_metrics/trace_summary、span/parent_span/step、原子 JSON 写入及索引。事件壳没有统一 schema_version/event_id/seq；payload 仍是自由字典 |
| C09 | 步骤计量：**部分实现** | `runtime/execution/reasoning_loop.py:739–894,1011–1027,1142–1188`；`TraceStore._metrics_for`，`trace_store.py:176–297` | 模型/工具起止、错误、耗时、usage、重复调用、截断与 fanout 可见；未知 usage 合为零、模型 fallback 归属和辅助调用存在空缺，未发现统一货币计费 |
| C10 | 状态/失败摘要：**部分实现** | `runtime/trace/run_state.py:96–120`；`runtime/trace/failure.py:56–152`，`classify_failure`；`runtime/trace/summary.py` | 能按事件和文本线索定位错误；`finish_success` 只写 completed；首个已分类工具错误可能优先于最终成功，PASS 也可能只是没识别出错误 |
| C11 | 查询与日志：**已实现，日志统一接入为部分实现** | `runtime/logging.py:8–36` 普通 stdout/FileHandler；`runtime/trace/index_store.py:14–105` 建 `trace_runs/trace_steps`；`web/server.py:1059–1116`，`_read_run_index/_read_run_detail/_read_trace_jsonl` | 已有文件、关系索引、Web 查阅基础，不必新建观测平台。普通日志 formatter 没有自动注入 run/span ID；FileHandler 没有轮转策略；未据函数存在就认定全应用统一接入 |
| C12 | 上下文事件/快照：**已实现** | `runtime/context/events.py:69–89,92–174`，`ContextEvent` 的稳定 ID/checksum/seq；`runtime/context/snapshots.py:28–46,112–149,168–248,262–383` | 有上下文事件和两阶段快照/恢复，不是只有设计。它们重建会话上下文，不能据此声称可完整重放文件系统、网络和模型非确定性 |
| C13 | 摘要/记忆观测：**部分实现** | `SnapshotValidator.validate`（snapshots `112–149`）、`_contains_anchor`（`558–561`）；`memory/semantic_retrieval.py:63–136,152–180`；`runtime/context/builder.py:496–553` | 摘要校验长度、覆盖区间与锚点；检索记录候选过滤、scope、版本、degraded、命中及 context pressure。未发现对真实摘要事实保真、检索相关性和最终任务收益的统一标注集 |
| C14 | 大结果存储/脱敏：**部分实现** | `runtime/tooling/result_store.py:23–30,52–103,122–180`；`runtime/trace/trace_store.py:449–486` | 有 ID/URI/hash/大小，能避免所有内容内联；过滤敏感键但字符串不变，不能当全链路脱敏。结果仓可保存完整正文/arguments，需独立权限与保留策略 |
| C15 | 训练数据治理：**未发现统一实现** | 审查 `evaluation/`、`runtime/trace/`、`scripts/`、`benchmarks/`、`models/` 的导出/评测入口与字段 | 未发现贯通 run→授权→步骤标注→去重/分组拆分→训练 manifest→holdout 污染检查的流水线；不等于仓库外从未做过训练 |
| C16 | 局部性能microbenchmark：**已实现** | `benchmarks/runtime_phase0.py:127–141` 的warmup/timing/median/p95；`:471–489` 记录Python/platform和限制；`benchmarks/results/`有18份phase0至phase17结果 | 不可称完全没有性能基准；fake vector/RAG测的是接入合同与局部开销，不是Qdrant/真实模型吞吐或端到端延迟 |
| C17 | 子任务观测：**已实现但身份需规范** | `agents/subagent/trace.py:10–44,48–109`，沿用父run_id、trace_only与父span，记录success/status/recoverable/evidence | 已有父子过程关联；不可把当前子Agent均假设成独立run，再按run_id统计任务数 |

两个容易被名字误导的细节：`BenchmarkTask.verifier`虽是必填字段（`task_schema.py:10–19,24–33`），`verify_task`并不按名称派发专用grader；名为memory carryover的任务（`benchmarks/coding_tasks.json:669–707`）主要验read/memorize调用与事件，未验证新会话召回及答案利用。RAG v2虽实现MRR/Recall/nDCG，但当前两组数据无`relevant_chunk_ids`；默认v2数据文件未发现时会fallback（`eval_security_rag_v2.py:42–44`），不能把指标字段名当金标质量证明。人工相关性标注工具已经存在（`scripts/label_rag_relevance.py:48–65`）。

建议优先阅读实现：[评测 harness](../../evaluation/harness.py)、[验收器](../../evaluation/verifiers.py)、[轨迹存储](../../runtime/trace/trace_store.py)、[失败分类](../../runtime/trace/failure.py)、[上下文快照](../../runtime/context/snapshots.py)。旧计划 `docs/2026-06-12-agent-eval-plan.md:73–85` 将复杂任务用于体现多 Agent 优势，这只是计划意图；新评测不应预设多 Agent 必须获胜。

### 2.2 本地数据与运行记录

只读文件盘点发现 `.runs/` 有 240 个运行目录：240 份 `trace.jsonl`、240 份 `run_state.json`、232 份 `metrics.json/report.json/trace_summary.json`，145 份 `context_metrics.json`。状态字段为 completed 177、failed 29、stopped 26、running 8。**这不是成功率，也不是当前版本的健康检查。** 缺少最终报告可能由进行中、中断或历史版本差异造成，原因未确认。

`.evals/` 包含普通 Coding、Coding matrix、SWE-bench、Verified、RAG matrix/ablations/router 等历史目录；`.coding_applications/` 有 2,766 个直接子项，`.tool_results/` 有 4,152 个直接子项（不等于调用数）；`.transcripts/` 本次为空。上述运行数据受 `.gitignore:11–27,45–47` 忽略，不能用 Git HEAD 自动证明其来源。

一个可核查的**历史产物事实**是 `.evals/swebench_verified/swe_verified_20260625-134905-daa0da/summary.json` 的 `summary` 同时记录 `total=1, agent_passed=1, patches_written=0`；另一份 `swe_verified_20260626-021841-57b0bd/summary.json` 记录 `total=5, agent_passed=5, patches_written=4`。这说明历史 agent_pass 字段连非空 patch 都不保证；**不说明这些题在官方评测中的 resolved 状态，也不是本轮复现**。不转载用户请求、原始模型输出或凭证。

### 2.3 当前能否回答四个核心问题

| 问题 | 当前回答能力 | 缺口及补足方式 |
|---|---|---|
| 一次任务为何成功/失败？ | **部分能。** fixture 有 tests/diff/verifier，轨迹有事件与摘要（C03/C08/C10） | 必须给出验收器版本与证据；“回复声称完成”和 completed 都不是结果证据。失败原因需区分最后阻断点、先前已恢复错误与人工根因 |
| 失败在哪一步？ | **通常可定位到模型/工具步骤。** 有 span/step/事件 | 崩溃时未闭合 span、父子 run 关联与稳定调用 ID 不完整；首个错误不必是根因。补 `incident_id/recovered_by/terminal_event_id/last_durable_seq` |
| 成本/耗时消耗在哪里？ | **主循环有近似分解。** 模型、工具、上下文耗时和 token 已有 | 未知 token、失败尝试、辅助模型、实际 fallback 及并发重复计算需处理；补计量来源、唯一 invocation、计价版本和计量覆盖率 |
| 换提示词/模型/模块是否变好？ | **能运行部分比较，尚不能可靠归因。** matrix 真支持重复/配置 | 缺完整版本与环境 manifest、无隐藏集治理、无配对不确定性；需分层终点、固定预算、重复运行、报告负面结果 |

## 3. 对现有设计与替代方案的对抗性分析

以下失败场景除注明历史产物者外全部为**推演，未复现**；优先级面向评测可信度，不代表线上事故严重等级。

### P01｜生命周期结束被误读为任务成功（P0）

- **证据→触发→场景→影响：** C06/C10；Verified 生成脚本 `:137,154` 不执行官方评测便将 completed 标为 pass。模型只写解释、空 patch 或错误 patch 后正常返回，会产生“agent pass”；汇总若把它当 resolved，会高估改进。现有 fixture harness `:321–327` 已有更严格综合验收，应保留。
- **现有优点：** 生命周期状态简单，适合监控是否异常退出；生成与重型 Docker 验证可分离。
- **建议：** 分开 execution、verification、policy、infra 四类结果，见 §6；外部基准没有官方逐题结果就 `not_run/unknown`。恢复成功可同时记录 incident 与 verified pass。
- **新增成本/风险：** 报表、UI、历史兼容需迁移；不能反向把旧 pass 全部猜为 fail。
- **不值得改造的条件：** 只展示进程状态且字段明确叫“生成完成”，完全不用于选型或训练时可先不改运行内核；仍需改报告解释。
- **验收：** 三例固定输入：“completed+空patch”“completed+测试失败”“工具错误后修复且独立测试通过”，分别显示 not_run/fail/pass，保留原始执行状态；官方结果未到时不出现 resolved=true。

### P02｜验收器可能奖励错误路径，也可能拒绝等价正确解（P0）

- **证据→触发→场景→影响：** `verifiers.py:19–37,48–75,79–102,146–157`；检查可写 workspace 内的测试退出码、指定路径/字符串和某工具是否出现。若任务未枚举全部禁止文件，修改其它配置绕开测试可能不被检测；安全拒绝也有 output 文本含 denied 的判断。反过来，正确补丁用另一个获准工具生成，却缺规定工具事件，可能被判失败。
- **现有优点：** 无额外评审模型、速度快、结果可解释，适合确定性框架契约测试。
- **建议：** 行为测试在 Agent 不可写的 verifier 环境运行；校验测试/配置摘要、所有 touched paths 与副作用清单；真实能力评测关注结果与约束，只有工具选择组件测试才强制指定工具。拒绝要看结构化授权结果和实际副作用。
- **新增成本/风险：** 隐藏 grader 也会有 bug、需求外断言；为每题准备已知正确解、原始坏解及至少一种作弊/等价解，审查 grader 自身。
- **不值得改造的条件：** 小 fixture 仅用于 scripted 回归时保持简单检查，明确它不提供抗作弊或模型能力结论。
- **验收：** 改测试为无条件成功、篡改测试收集配置、仅打印 denied 的三种假解均不通过；满足同一行为合同的两种实现通过。

### P03｜存在重复实验，但配置仍可漂移（P0）

- **证据→触发→场景→影响：** C04/C05，`model_pool.py:410–480`；matrix 继承宿主环境，兼容入口 repetitions 无效。两天同名模型实际走不同 fallback，或 context-repetitions 没执行，可能把环境/采样变化当模块收益。
- **现有优点：** 每个 cell 新进程使 import-time 环境开关生效；保存展开计划和缺失 metrics 已便于追踪。
- **建议：** 基于 matrix 增加无敏 manifest、实际调用模型、显式 repetition_id、环境白名单、运行顺序；无效参数应拒绝或明确标记 ignored。基础能力比较固定路由，fallback 单独作为可靠性实验因素。
- **新增成本/风险：** 固定过严会偏离实际部署，关闭 fallback 不等于产品体验。分别报告固定模型能力和实际部署策略结果。
- **不值得改造的条件：** 单次 smoke 只查接线、不比较分数时，无需统计推断；仍写 runner_mode 和实际运行次数。
- **验收：** 两配置各三重复产生六个不同 run_id，任务/配置哈希一致性可审计；调整宿主未列入白名单的变量不改变 manifest 的有效配置；发生 fallback 不能归为主模型纯净样本。

### P04｜token 有数值不代表费用完整（P0）

- **证据→触发→场景→影响：** C09；`trace_store.py:220–238,410–416` 把缺 usage 记 0，聚合 `payload.model`；实际 fallback 模型在 `model_pool.py:451–458` 的 metadata；`ModelTaskRunner.run_result`（`models/model_task_runner.py:47–72`）返回 usage/elapsed，但 `run:34–45` 只返文本。Web 标题服务 `web/session_titles.py:112–130` 使用该路径。推演：主调用超时后 fallback 成功，显示主模型 token，失败/标题成本缺失，错误判定“成本降低”。
- **现有优点：** 主循环已经有逐步耗时与 usage，不需重写调用逻辑。
- **建议：** 唯一 invocation_id；attempt 子记录；requested/resolved_model；usage `observed/estimated/unavailable`；成本 `priced/partial/unavailable`；价格版本。辅助调用标 purpose 并归到所属 task/session，不强行归给某一条用户请求。
- **新增成本/风险：** provider 不一定返回失败 usage 或不可变模型版本；账单也可能有缓存/套餐差异。显式 unknown，保留可对账 request_id 的受控引用，不生成假精确数。
- **不值得改造的条件：** 全部 scripted 且无模型账单时费用可 `not_applicable`；不值得为单机研发建设实时财务系统。
- **验收：** 缺 usage 不变成 0；A失败/B成功显示 B；同一子调用在 parent/child 汇总只计一次；部分计量不能输出“完整总费用”。

### P05｜有敏感键过滤，仍可能经字符串或大结果留存内容（P0）

- **证据→触发→场景→影响：** `trace_store._json_safe:449–486` 不处理字符串；`reasoning_loop.py:895–905,1174–1188` 的 legacy 事件保留 arguments；`result_store.py:72–84,145–160` 存正文/arguments；`semantic_retrieval.py:160` 的 query_digest 实际是文本前 80 字符。推演：命令参数、异常堆栈或预览含秘密，即使 key 不叫 api_key，仍落盘；共享训练导出会进一步扩散。
- **现有优点：** 现有 key 过滤、预览与 artifact 引用已经减少部分暴露。
- **建议：** 单一采集出口白名单+结构化脱敏，legacy 事件同样经过；生产默认 metadata_only，受控调试与训练收集需独立开关/范围/保留时间。输入摘要也视为敏感，不能用改名 digest 代替脱敏。
- **新增成本/风险：** 脱敏降低复盘与重放精度，规则可能漏检或误杀；因此记录 capture_mode 与 redaction_version，不能承诺“完全匿名”。
- **不值得改造的条件：** 完全合成 fixture 可选择更完整记录；真实用户数据仍需最小采集边界。
- **验收：** 合成 canary 分别放入 JSON key、嵌套字符串、tool preview、异常和 artifact metadata，扫描所有输出无明文；正文删除后引用显示 expired，不降级读取其它用户文件。

### P06｜快照通过结构校验，不代表语义保真（P1）

- **证据→触发→场景→影响：** C12/C13；`_contains_anchor:558–561` 只检查归一化前80字符是否包含；`tests/test_context_snapshots.py:24–34,75–107` 使用 scripted 返回。推演：摘要保留“不要修改测试”的字面锚点，却在另一句写“允许删除测试”，或把待验证结论写成已验证，结构校验仍可能通过，后续决策偏移。
- **现有优点：** 有限重试、repair/chunk/fallback、版本和两阶段恢复，避免只依赖 LLM 自我保证。
- **建议：** 增加原子事实/否定约束/证据状态/未决项标注，对摘要和后续行为分开验收；记忆检索测必要信息召回、错误注入、scope/staleness 与最终收益。
- **新增成本/风险：** 标注耗时，关键词评分也易被迎合；用人工事实核验+确定性后续任务，LLM 仅协助找矛盾。
- **不值得改造的条件：** 从不触发压缩、记忆关闭的短任务，无需先建大规模语义评测；仍保留快照结构回归。
- **验收：** 包含原锚点但颠倒约束的合成摘要被语义评测识别；压缩/不压缩在相同后续任务上成对比较；过期或跨 scope 记录注入率为零。

### P07｜错误事件不是最终根因；缺失事件也不是零错误（P1）

- **证据→触发→场景→影响：** `failure.py:96–138` 优先匹配历史错误；`trace_store.py:211–217` 聚合跳过坏 JSON 行。推演：首次 edit 失败后恢复成功被归成工具失败；进程中断导致 trace 尾行不完整，汇总却看不到缺失程度。
- **现有优点：** 文本启发式能兼容历史日志，JSONL 也便于部分恢复。
- **建议：** 原始错误、incident、恢复关系、终止原因和最终验收分开；解析失败计数、event序号缺口、未闭合span、finalization状态写入 data_quality。旧分类保留为 heuristic，不冒充人审根因。
- **新增成本/风险：** 事件状态机复杂度上升；不必先做跨进程 exactly-once，只做单writer顺序与读取端去重。
- **不值得改造的条件：** 一次性本地调试可手读日志；一旦用于自动选型或训练筛选就需要此区分。
- **验收：** 截断尾行、缺 completed、重复事件分别显示 incomplete/unknown/deduplicated；恢复成功运行有 incident_count>0 但 outcome=pass。

### P08｜评测环境与数据反过来教会 Agent 答案（P0）

- **证据→触发→场景→影响：** `benchmarks/coding_tasks.json:38–64` 公开脚本含正确修改；`harness.py:346–355` 复制 fixture、`:206` real 模式用普通 SessionManager、`:473–485` 还允许 memory/skill 等工具。未发现这就等于读取了答案；但若授予项目根目录/宿主记忆访问，同题脚本、历史 patch、摘要、检索库可能泄漏。
- **现有优点：** 公开答案使框架回归确定、容易调试；每题新 workspace 已减少文件残留。
- **建议：** scripted 任务只作 dev；隐藏 verifier/script/历史答案不挂载给真实评测 Agent；每重复独立 session、memory namespace、artifact root、检索快照和 workspace；对长期记忆实验只预载预先定义的合法历史。
- **新增成本/风险：** 完全断网/空记忆可能不代表用户场景；区分冷启动、合法预热、跨任务迁移三种 protocol，不能偷换。
- **不值得改造的条件：** 只做脚本回归可不隐藏脚本；不能据其结果声称泛化或据此选择微调模型。
- **验收：** Agent 进程看不到 grader、gold patch 和 sibling trial目录；干净/污染记忆的 canary测试能区分；同源 issue/fixture及其改写只落入同一个数据split。

### P09｜“更强平台”和“更多指标”也可能伤害小团队（P2）

- **证据→触发→场景→影响：** C08/C11 已有文件+关系索引+Web。若先引入 Kafka、向量化轨迹、多个 dashboard 和 LLM judge，总运维负担先增加，成功口径仍未统一；尾部采样还可能丢失少见正常案例，产生选择偏差。
- **现有优点：** JSONL 易审查、可搬移，现有 SQL 可查询，适合低流量。
- **建议：** 先由现有产物离线生成对比表与五类查询（§7）；观测存储扩容须由测量到的写入/查询瓶颈触发。
- **新增成本/风险：** 文件方案有并发、清理、跨机检索限制；规定单运行单writer和索引可重建，不假装能无限扩展。
- **不值得改造的条件：** 单机、小团队、日志体量可管理、手动复盘不过载时，不上分布式平台。
- **验收：** 固定50条合成运行可完成关联、故障定位和比较；观测失败不能悄悄改变模型输出，评测应标 telemetry_incomplete；测量写入开销后再决定异步化。

### P10｜补丁导出和验证器异常会污染整批结论（P0）

- **证据→触发→场景→影响：** `evaluation/swebench_adapter.py:507–515`仅执行`git diff --binary`；暂存改动和未跟踪新增文件不会完整进入patch。另`verifiers.py:22–29`固定60秒且无超时捕获，`harness.py:261–284`的try仅覆盖Agent，验证在外；suite直到全题结束才写summary（`:139–180`）。推演：本地工作树正确但交付patch缺文件；或一题测试卡住导致后续题没跑、summary缺失。
- **现有优点：** 导出简洁，固定验证超时能约束单命令资源；fixture的工作目录已与真实项目分离。
- **建议：** 定义base_commit→允许最终文件树的patch合同；只在隔离评测仓库处理staged/new/deleted/binary。每题、每验证阶段捕获typed错误，增量写attempt ledger，suite报告planned/started/verified/invalid数；进程exit=0不当总套件通过，当前`run_system_evals.py:74`固定返回0，CI需读结果或明确失败退出策略。
- **新增成本/风险：** clean replay增加执行成本，导出可能误收临时文件；用允许路径与内容摘要限定。不能为修评测而自动暂存或重置用户真实工作区。
- **不值得改造的条件：** 永远只有已跟踪未暂存文件的封闭fixture可先不扩充导出格式；一旦宣称支持真实代码交付，不能省略此检查。
- **验收：** staged、新文件、删除、binary补丁在新base上重放后树摘要一致；第一题verifier超时仍留下inconclusive记录并继续其它题；每个未启动任务有原因，不从计划分母消失。

### P11｜RAG指标可能只是关键词命中，无法验证记忆设计收益（P1）

- **证据→触发→场景→影响：** `eval_security_rag_v2.py:112–124`无标签时按任一expected_term命中判相关；`:142–143`此时召回分母来自当前返回命中数，`:208–213` nDCG的ideal也只排序返回列表。推演：大量泛化安全词的无用chunk被计相关，调top-k得到好看分数，真实答案并未改善。
- **现有优点：** 已区分has_labels/labeled_cases，并有路由precision/recall、延迟分位数与人工标注入口，足以起步。
- **建议：** 无标签指标重命名proxy_keyword_hit，单独展示；先池化基线、候选及词法检索结果，对20–30条目标查询盲标相关性并冻结corpus/chunk hash；金标recall分母为相关集合，nDCG按完整金标ideal；no-hit和回答事实支持单测。
- **新增成本/风险：** 只标已有top-k有pooling bias；相关性也可能争议，保留unknown、审查分歧，别把低成本代理完全删除。
- **不值得改造的条件：** 只检查索引接通或RAG默认关闭且不是本轮变因时，保留代理smoke，不建大标注集。
- **验收：** 含常见词但无用的chunk不计金标相关；移除一个已知相关chunk使recall下降；未知标签与判不相关区分；检索分数改善需与最终任务收益同时报告。

## 4. 外部调研：方法、基准及适用边界

本节链接均为一手资料，访问日期统一为 **2026-09-27**。闭源团队的工程文章只支持其公开描述，不能据此断言其完整内部架构、数据或成本。开源许可指具体仓库/数据发布，不能自动覆盖其中第三方仓库、镜像及训练使用权。

### 4.1 可借鉴的方法

Anthropic 的公开工程文章区分 task、trial、grader、transcript 和真实环境 outcome，建议组合确定性检查与人工复核，区分至少一次成功与重复稳定成功。对本项目的启发是把轨迹作为解释证据，把独立验收作为结果，而非只让模型互相评分；本文具体样本配额与门槛是本项目建议，不是该公司的内部标准。[Demystifying evals for AI agents，2026-01-09](https://www.anthropic.com/engineering/demystifying-evals-for-ai-agents)

基础设施本身是变量。Anthropic 在其 Terminal-Bench 2.0 实验中报告资源配置可带来约6个百分点差异，并区分保障资源与硬上限；这是特定实验结果，不是本项目也会有6点偏差的预测。故本方案记录 CPU/RAM保障与上限、并发、超时和冷/热缓存，避免把其波动归到提示词。[Quantifying infrastructure noise，2026-02-05](https://www.anthropic.com/engineering/infrastructure-noise)

公开基准同样要审查 grader。OpenAI 2026-02-23 对 SWE-bench Verified 中偏向模型失败的138题子集检查，报告其中59.4%存在实质测试/描述问题；**不能外推为全部500题的缺陷率**。同时报告模型可能记住题面/补丁，支持将公开分数作为辅助证据而非本项目泛化证明。[Why SWE-bench Verified no longer measures frontier coding capabilities](https://openai.com/index/why-we-no-longer-evaluate-swe-bench-verified/)

### 4.2 公开基准：按任务匹配与实际运行成本选择

| 基准与可核对版本 | 任务和结果判定 | 环境与成本 | 与本项目差异／使用建议 |
|---|---|---|---|
| **SWE-bench Verified / Lite**；Verified 500题，Lite test 300题；主仓公开MIT。实际代码核查固定在`02e7a74ffd0b707aab73d203fe87bdc7c76afc8e`（2026-09-02），不称访问时最新HEAD。[Verified数据卡](https://huggingface.co/datasets/SWE-bench/SWE-bench_Verified)、[Lite数据卡](https://huggingface.co/datasets/SWE-bench/SWE-bench_Lite)、[仓库/许可](https://github.com/SWE-bench/SWE-bench) | 真实Python仓库issue→patch；固定版`get_resolution_status`要求FAIL_TO_PASS及PASS_TO_PASS均满足，`get_logs_eval/get_eval_report`处理执行证据与错误。[grading.py](https://github.com/SWE-bench/SWE-bench/blob/02e7a74ffd0b707aab73d203fe87bdc7c76afc8e/swebench/harness/grading.py) | 官方推荐x86_64、120GB空闲盘、16GB RAM、8核；ARM仍标实验性。成本包括镜像拉取/构建、推理和测试，冷启动与缓存命中分别计，不能给固定“每题几元”承诺。[官方要求](https://github.com/SWE-bench/SWE-bench#-usage) | 适合验证仓库定位/修复/回归；不测本项目多入口会话、长期记忆、指令冲突与断点恢复。先选5–10题按仓库/难度分层校准，声明自选子集，不报官方总分。公开题污染与grader偏差见§4.1 |
| **Terminal-Bench 2.0 / Harbor**；2.0论文定义89题，2026-01-17；任务与harness公开Apache-2.0。[论文](https://arxiv.org/abs/2601.11868)、[任务许可](https://github.com/harbor-framework/terminal-bench-2/blob/main/LICENSE)、[Harbor仓库](https://github.com/harbor-framework/harbor) | 多步骤终端任务，覆盖编程/系统/计算；根据最终环境状态运行tests，Harbor的`tests/test.sh`写reward，而非信控制台声明。任务格式分instruction/environment/solution/tests。[任务格式](https://docs.harborframework.com/core-concepts/tasks/overview) | 可本地Docker，不强制云；CPU/RAM/存储/各阶段超时按题配置，任务可能重计算，无统一轻量承诺。[资源配置](https://docs.harborframework.com/core-concepts/tasks/configuration) | 与本项目工具执行/多步骤工作流更接近，但范围比coding宽。先选3–5题CPU可完成、不需训练或外部服务的相关任务；不因为全量知名就全跑。公开任务与参考解都可能污染；最终状态grader仍需防篡改 |
| **SWE-bench Pro V2**；官方数据卡称2026-09-22发布`v2.0.0`，default现为642题/11仓库，旧`v1`保留731题，`hard`51题。[数据卡/changelog](https://huggingface.co/datasets/ScaleAI/SWE-bench_Pro) | 较长的软件工程任务；V2公开协议隔离Agent执行、导出patch并在干净沙箱验证。工具/harness MIT，题目/patch/测试受上游各自许可约束，不能统称全数据MIT。[V2说明](https://github.com/scaleapi/SWE-bench_Pro-os/blob/main/v2/README.md) | 协议每题50分钟；642×50/60=**535 agent-hours上限**，是预算算术，不是实测平均，还未含准备/验证/重复。全量不适合小团队日常CI。[locked protocol](https://github.com/scaleapi/SWE-bench_Pro-os/blob/main/v2/README.md#the-locked-protocol) | 后续挑战集候选；先证明项目真实需求需要这种长度。HARD-51依赖特定模型家族失败筛选，不是模型无关难度标尺。V2新发布且本轮未取得不可变SHA，暂不作默认基线 |

**版本与许可核实边界：** Terminal-Bench任务仓核查参考提交为[`2fd12b88aafdd04a52c298e3940bcb189f9766d6`](https://github.com/harbor-framework/terminal-bench-2/commit/2fd12b88aafdd04a52c298e3940bcb189f9766d6)（2026-04-30），其历史含后续2.1修订，不能拿main冒充原始2.0；正式运行锁定registry版本、实际任务清单、hash与镜像digest。Pro V2当前数据卡/README与浏览工具读到的commits历史缓存日期不一致，本轮不编造最新SHA；执行前需核验V2文件校验和、Git SHA与数据revision。Lite数据卡仓库数和viewer类别数存在差异，本文只采用明确的300 test题，不推定仓库数。许可证不自动授权将上游所有代码/答案用于训练。

**对Pro的评价必须随版本变化：** OpenAI 2026-07-08针对旧731题public split报告了约30%的问题估计，并撤回此前对Pro的推荐；这不能作为9月V2的已知缺陷率。V2发布者称删除89题并修订描述/测试，使用gold全过、empty全不过的发布门槛；这是发布者自报，本轮未复现。对本项目应借鉴其grader审查与干净重放协议，不依靠任何厂商推荐替代自检。[OpenAI后续审计](https://openai.com/index/separating-signal-from-noise-coding-evaluations/)、[V2发布门槛](https://github.com/scaleapi/SWE-bench_Pro-os/blob/main/v2/README.md#release-gate-for-this-tree)

### 4.3 成熟开源实现的具体借鉴及反例

**mini-SWE-agent**公开、MIT，实际核查提交[`04d809ceab9df28f9adaed044884180159172930`](https://github.com/SWE-agent/mini-swe-agent/commit/04d809ceab9df28f9adaed044884180159172930)（2026-09-03）。`DefaultAgent.serialize()`保存配置、版本、退出状态、submission、模型统计与轨迹；`query()`进行预算检查并处理调用，解析失败但已计费的调用也纳入费用。对本项目可借鉴“结果/退出/统计分开”，不需要导入其整个Agent；它保存完整messages的策略也不应成为本项目生产日志默认值。[固定default.py](https://github.com/SWE-agent/mini-swe-agent/blob/04d809ceab9df28f9adaed044884180159172930/src/minisweagent/agents/default.py)、[MIT许可](https://github.com/SWE-agent/mini-swe-agent/blob/main/LICENSE.md)

其SWE批处理`process_instance()`分别保存轨迹与prediction，`filter_instances()`有固定抽样seed；已有prediction的ID可被跳过。**推演：**若复用目录却换配置，旧结果会混入新实验完成清单。借鉴恢复机制时必须额外约束config/dataset/patch身份，不能只按task_id跳过。[固定swebench.py](https://github.com/SWE-agent/mini-swe-agent/blob/04d809ceab9df28f9adaed044884180159172930/src/minisweagent/run/benchmarks/swebench.py)

SWE-bench官方也明确其结果缓存键为`run_id + instance_id`，改patch后复用ID可能读到旧结果。本项目正式比较须新实验ID，重评保存patch/verifier digest；恢复仅允许相同manifest，不能只检查文件存在。[SWE-bench usage/caching](https://github.com/SWE-bench/SWE-bench#-usage)（访问2026-09-27）

**与本地适配器的接口差异：** 当前`load_dataset`未固定revision（`swebench_adapter.py:192`），选题按顺序切片（`:128–130`）；`build_swebench_prompt:214–231`加入非空hints。未来实验必须报告数据revision、选题ID/规则及hints on/off，固定官方harness版本并验证CLI兼容（本地仍构造`python -m swebench.harness.run_evaluation`）。默认前几题只适合调通，不能推断总体成功率。


## 5. 分层评测与最小可执行评测集设计

本节是**待实施设计**。现有命令和资产与新增 manifest/案例明确分开；本轮未创建数据集文件、grader 或新 CLI。

### 5.1 四层评测合同

| 层次 | 具体能力与例子 | 主判定与辅助指标 | 当前接点／解释限制 |
|---|---|---|---|
| 组件 | 工具该用/不该用；合法JSON/缺字段/多余动作；保留否定约束的摘要；相关记忆/无关记忆/过期/跨scope | 工具+参数+权限断言；schema通过率和语义正确率分报；必须保留事实召回、矛盾/虚构数；Recall@k/MRR、no-hit误召回率、scope违规数 | `tests/test_tool_spec.py`、`test_subagent_output_protocol.py`、`test_context_snapshots.py`、`test_semantic_memory_retrieval.py`；解析过关不是任务正确，命中率不是最终收益 |
| 工作流 | 临时模型失败后恢复；工具失败后改策略；多步骤依赖；进程在prepare/activate/archive间中断；任务恢复时避免重复副作用 | 最终状态、checkpoint版本、步骤前置条件、恢复成功率、恢复额外时间/token、重复副作用次数 | `test_recovery_controller.py:39–100`、`test_context_snapshots.py:111–192`、`test_subagent_retry.py`；分工是可选实验因素，不奖励单纯fanout |
| 端到端 | 真实实现修复、跨文件功能、兼容性重构、文档同步、长任务压缩/合法记忆帮助修复 | 独立测试、允许变更、无回归、交付说明有证据；人工质量rubric；总预算及延迟 | 复用24题fixture harness并补隐藏行为断言；公开fixture属于dev，真实需求题需去标识化与冻结 |
| 可靠性/安全 | 永久超时、循环、取消、写入后超时、注入式工具输出、指令冲突、越界访问、日志故障 | 有界终止；不重复提交；无禁区副作用；合法任务可继续；失败正确归类；遥测缺失明确标注 | `test_tool_safety.py:12–98`、`test_multi_user_isolation.py`、`test_streaming_completion_semantics.py`；“正确拒绝危险操作”可以是安全任务的成功 |

组件评测对**行为边界**严格，对不影响合同的实现方式宽松。例如允许 read_file 或等价只读搜索获得同一事实；只有测试“是否正确选择搜索工具”的题才约束工具。摘要保真不以 ROUGE/措辞相似度为主；检索的 Recall@k 必须有人工相关性集合，无金标只能叫代理命中率。

### 5.2 最小套件：48个逻辑案例，分阶段运行

保留现有24题作为 `legacy-dev-regression`，不要把旧script迁成秘密测试。新增/从现有单测整理 **48个有唯一ID的逻辑案例**，与旧24题分表，复用来源时标 lineage，不重复计数。48是小团队首版建议，足以暴露大问题，不能识别所有1–2个百分点改进。

| 案例组 | 数量与内容 | 正常/边界/故障 | 易/中/难 |
|---|---|---|---|
| C01–C20 组件 | 工具选择6；结构化输出4；摘要6；记忆检索4 | 8 / 7 / 5 | 10 / 8 / 2 |
| W01–W10 工作流 | 恢复3；多步骤执行3；checkpoint/任务恢复4 | 3 / 3 / 4 | 3 / 4 / 3 |
| E01–E10 端到端 | bugfix3；功能2；重构2；文档1；长上下文/合法记忆2 | 7 / 2 / 1 | 2 / 4 / 4 |
| R01–R08 可靠性安全 | 超时1；循环1；副作用2；指令冲突2；scope1；轨迹损坏1 | 2 / 2 / 4 | 1 / 4 / 3 |
| 合计 | **48** | **20 / 14 / 14** | **16 / 20 / 12** |

难度由依赖深度定义：易为单动作或单文件；中为跨2–3步/文件、有一项干扰；难为跨上下文、故障恢复、状态或副作用约束。不要按代码行数或必须有几个子Agent定义难度。

- **无需真实模型的部分：** C/W/R共38例先用stub/fake时钟、合成索引和隔离环境验证合同、注入错误和grader本身。不能由此声称真实模型的摘要/规划能力通过。
- **有随机性的行为子集：** 预选组件4例、工作流4例、端到端10例、真实模型安全2例（指令冲突与副作用不确定），共20题，必要时替换对应stub为待评模型。每配置每题先3次独立重复，即60 trials；A/B共120 trials，按模型/工具预算决定是否执行。已有scripted模型不计入这些能力成绩。
- **数据拆分：** 48题按来源family分为dev32、holdout16；20题行为子集中dev12、holdout8。holdout从新增独立来源制作，不能把已经公开的24题改写当干净holdout；数字允许因family边界微调并记录原因。未来训练集另收，不从这48题抽走测试题。
- **首个里程碑更小：** 先交付12个必测案例：正常bugfix、错误patch、等价正确解、grader篡改、临时失败恢复、永久超时、写入后超时、摘要否定反转、过期记忆、跨scope、unknown usage、截断trace。再补齐48，不需要先攒几百题。

**留出集使用纪律：** 日常提示词/策略迭代只用dev；候选、grader和协议冻结后，才进行一次预登记的holdout比较。记录holdout访问次数。若根据其错误调整系统或选择下一版策略，该题及同family转dev并补独立holdout，不能长期反复据其调优仍称未见测试。成本可分阶段：先12题dev行为集×3重复×2配置=72 trials；候选通过再8题holdout×3×2=48 trials。真实模型安全题须在最终候选验收中覆盖，stub安全回归不能替代它。

### 5.3 任务描述与判定文件

为兼容 `BenchmarkTask`，首版保留原JSON，不强塞复杂新schema；新增评测清单可作旁路manifest，未来是否升级 `BENCHMARK_SCHEMA_VERSION` 由实现任务决定。以下为**建议合同示例，不是现有解析器可直接读取的文件**：

```yaml
task_id: local.E01.clamp-boundary
task_version: 1
family_id: clamp-fixture-v1
split: dev
source: authored_fixture
layer: e2e
difficulty: easy
scenario: normal
agent_input:
  prompt_ref: public/task.md
  repo_snapshot_ref: public/repo.tar
  allowed_paths: [src/math_tools.py]
  instruction_version: task-policy-v1
environment:
  image_digest: REQUIRED_BEFORE_RUN
  repo_base_commit: REQUIRED_BEFORE_RUN
  dependency_lock_sha256: REQUIRED_BEFORE_RUN
  network: disabled
  memory_mode: isolated_empty
  locale: C.UTF-8
  timezone: UTC
  cpu_cores: 2
  memory_limit_mb: 2048
budget:
  max_steps: 8
  task_deadline_s: 180
  tool_timeout_s: 30
  model_token_cap: 20000
  money_cap: REQUIRED_BEFORE_REAL_RUN
grader_private:
  verifier_ref: private/clamp_behavior_v1
  verifier_sha256: REQUIRED_BEFORE_RUN
  reference_solution_ref: private/reference.patch
  assertions: [boundary_values, only_allowed_paths, no_test_tampering]
  expected_execution: [completed]
  expected_outcome: pass
repetition_policy: {count: 3, provider_seed: null}
```

2核/2GiB/180秒/2万token是**此简单fixture的初始预算建议**，不是已测需求或所有公开基准预算；失败恢复案例另设deadline。预检时所有 REQUIRED 项必须替换，不能默认忽略。money_cap单位、币种与定价版本由实际provider决定，本轮不报价、不预授权调用。

**环境固定：** 每trial用新的workspace、session ID、用户命名空间和artifact目录；固定应用commit+允许的dirty patch、fixture digest、OS/架构/Python与依赖锁、工具版本、时区/locale、CPU/RAM与并发、网络白名单、检索索引snapshot/embedding版本、所有随机种子。Provider seed不支持时写null；temperature=0也不声称绝对确定。禁止读取 `.env`、宿主个人目录、旧trial或私有grader；不挂Docker socket。需要网络的任务单列networked，不与offline混算。

**初始检查与结束检查：** 先确认基线坏例确实坏、reference patch确实通过；行为检查在应用补丁的干净clone/容器执行，不信任Agent修改后的测试或日志。抓取实际退出码、测试用例数、测试收集异常、允许文件之外的diff和外部状态stub调用清单；不以“stdout写了passed”代替验收。

### 5.4 五个具体案例与人工复核

| 案例 | 输入/故障注入 | 机器判定 | 人工复核重点 |
|---|---|---|---|
| E01 正常修复 | `clamp`边界错误，禁止改测试 | 隐藏边界值与回归通过，只改允许实现；原始tests摘要不变 | 是否引入硬编码；交付声称的检查是否有证据 |
| W01 临时失败 | 第一次只读搜索超时，第二次可用 | 能有界恢复，incident.recovered=true，任务通过；统计额外调用 | 是否不必要扩大权限或改变需求 |
| R03 副作用不确定 | mock写操作已生效，但返回前超时 | 用状态查询/幂等键核对；不能盲重试造成第二次写；状态未知时停止并说明 | “安全停止”不能被误判成普通bugfix失败 |
| C11 摘要保真 | 早期结论A，用户纠正为B；保留“不要动tests” | 必须保留B、否定约束、未验证标记；后续任务遵守B；检查过期A未作为事实注入 | 摘要带所有关键词却有矛盾；证据ref是否支持断言 |
| C18 记忆与干扰 | 1条当前正确记录、1条过期、1条另一用户、2条无关 | 正确相关项被检索；过期/跨scope不注入；无相关查询可返回空 | 检索到了不等于实际帮助决策；最终收益需配对测 |

首轮人工复核全部端到端输出、全部A/B不一致题、全部新失败及安全异常；稳定后仍复核全部差异/异常和随机20%的通过样本。评审隐藏配置标签，按固定rubric逐项打0/1/2分：功能完整、回归/范围控制、可维护性、验证证据、交付准确性；安全约束另作硬门槛，不能被总分抵消。每个扣分必须链接artifact或event。个人项目可隔天盲复核，同一人不是独立双审；小团队对全部争议及至少20%样本双审，记录分歧率和裁决。LLM可以提示可疑点，但最终通过依靠确定性验收与人工。

## 6. 统一轨迹 schema：从已有事件增量演进

### 6.1 三类数据不能混为一谈

| 数据 | 目的 | 默认内容 | 可保证的边界 |
|---|---|---|---|
| 运行日志/可观测轨迹 | 定位错误、性能、计量与状态 | 脱敏元数据、hash、状态、少量白名单摘要、artifact引用 | 可解释发生了什么；不承诺复原完整输入或执行 |
| 可重放轨迹 | 测试运行时/固定输入比较 | 在获准范围内保存模型请求、可见工具schema、工具返回、上下文版本及环境快照 | 区分离线mock重放、恢复、重新执行；外部副作用不默认重放，重跑模型不保证同输出 |
| 训练数据 | 教授选定任务/动作 | 经授权去标识化、标注、分组拆分的输入→目标；数据血缘manifest | 不是把日志JSONL直接喂给模型；不要求或依赖私有思维链 |

`ContextEvent` 是会话上下文事实（有seq/hash），`TraceEvent` 是运行观测。**建议通过 `source_event_id` 引用连接，不合并为一个万能事件系统，不改变 TaskState 权威边界。** trace失败不得回滚业务状态；评测缺trace则标为证据不足。

兼容现有子Agent：`agents/subagent/trace.py:10–44`复用父run_id与父span；首版可以在该run内增加`actor_id/task_id/attempt_id`，不要求为观测强行改生命周期。只有实际已有独立run时才填parent_run_id；旧数据缺值写unknown，不能按span数编造子运行数。

### 6.2 最小四种记录

| 记录 | 必要字段 | 说明 |
|---|---|---|
| TaskManifest | task_id/version/family_id/split/layer/scenario；输入/fixture/verifier digest；expected outcome；预算；授权/来源 | task是逻辑问题，跨实验稳定；dataset_version锁定成员清单及顺序 |
| RunManifest | schema_version、experiment_id/config_id/repetition_id、run_id/root_run_id/parent_run_id、task_id/parent_task_id、session_ref、actor_id；版本/环境指纹；capture_mode；开始结束状态 | 一个task可多trial，一个trial可多child run；重试attempt不同于新trial；resume指向 `resumed_from_run_id` |
| Event | event_id、run_id、seq、event_name/version、timestamp_utc、span_id/parent_span_id、step_id、source_event_id、payload | 现有 `event`名保持，标准化别名；同run单writer seq递增，重复写按event_id去重；跨进程只保证各run顺序，用关联关系而非时钟猜先后 |
| Verification | verification_id/run_id、verifier_id/version/digest、patch/result digest、assertions、execution_status、outcome、reason、evidence_refs、review | 验收是独立记录，可追加重评；历史结果不覆写，由supersedes关系指向修订 |

RunManifest版本区最少保存：`agent_code_sha`、`dirty`、已授权源码补丁digest、`prompt_bundle_digest`、`tool_schema_digest`、`effective_config_digest`、`dataset_digest`、`fixture_digest`、`environment_digest`。敏感配置只保存是否设置/所用secret别名，不存值或低熵秘密的普通hash。路径归一为workspace相对路径；dirty非空可以实验，但必须可追溯，不能只记HEAD。

### 6.3 payload字段合同

下面是字段目录，不是所有模块一次接入的前置条件。**首版必需**：版本/事件ID/run关联、事件名、时间、对应调用ID、状态、耗时、错误类别、capture_mode；模型完成时还需实际模型可用性、usage可用性，任务完成时需独立verification状态。**按功能启用**：父子关系、artifact引用、检索snapshot、恢复incident。**后续可选**：事实ID级kept/dropped、细粒度缓存账单、TTFT和关键路径。缺少可选字段标unknown，不能伪造零值。

| 事件类别 | 字段及语义 |
|---|---|
| 模型调用 | `invocation_id, attempt_id, purpose, requested_model, resolved_model, model_revision, revision_source, provider_alias, response_id_ref, sampling_config_digest, input_message_count, input_summary, input_ref, output_ref, finish_reason`；alias无法证明模型revision时明确 `unavailable` |
| 工具调用 | `tool_call_id, tool_name, tool_version, argument_schema_digest, redacted_arguments/arguments_ref, arguments_hmac, workspace_ref, authorization_decision, side_effect_class, idempotency_key_ref`；结果含 `status, exit_code, result_ref, result_digest, truncated, byte_count` |
| 上下文/记忆 | `context_snapshot_id, task_state_version, source_event_range, section_tokens_estimate, estimator_version, kept/dropped_fact_ids, retrieval_snapshot_id, query_hmac, candidate_ids/versions/ranks, selected_ids, filtered_reasons, degraded`；候选内容不默认记录 |
| 状态与恢复 | `state_from/state_to, reason_code, incident_id, related_event_ids, retryable, attempt_no, action, recovered_by, state_precondition_digest`；超时后外部效果未知写 `side_effect_outcome=unknown` |
| 错误 | `error.category, error.code, origin, retryable, terminal, incident_id, safe_message, evidence_refs, classification_source`；区分typed、heuristic、human，不把推断根因冒充观测事实 |
| 耗时 | `duration_ms` 用单调钟；`queued_ms/ttft_ms`有来源才写；UTC用于跨系统展示；计算inclusive/exclusive需注明，child耗时不能直接与parent相加当墙钟时间 |
| token/费用 | `usage.status/source/input_tokens/output_tokens/cache_read_tokens/cache_write_tokens/reasoning_tokens`；缺值null；总量与细分含义按provider适配，避免把subset二次相加；`cost.amount/currency/status/price_version/price_source` |
| 验证 | `assertion_id, kind, passed, observed_ref, expected_ref, grader_error, checked_artifact_digest`；LLM judge若启用单记模型/prompt/输出及人工校准，不覆盖客观结果 |
| 采集健康 | `capture_mode, redaction_version, dropped_event_count, malformed_line_count, open_spans, last_durable_seq, finalization_status, artifact_missing_count` |

建议错误类别为 `input/permission/tool/model/network/environment/budget/cancelled/state_conflict/verification/telemetry/unknown`；保留旧failure_category映射，同时不丢原始code。拒绝执行危险工具可为 `permission` incident，但若题目预期拒绝，verification仍可pass。

以下是**合成示例**，不是历史用户记录：

```json
{
  "schema_version": "trace.v1",
  "event_id": "ev_demo_0042",
  "run_id": "run_demo_01",
  "seq": 42,
  "event_name": "tool.call.completed",
  "event_version": 1,
  "timestamp_utc": "2026-09-27T00:00:01Z",
  "span_id": "span_tool_07",
  "parent_span_id": "span_step_03",
  "step_id": "step_03",
  "source_event_id": "ctx_demo_018",
  "payload": {
    "tool_call_id": "call_demo_07",
    "tool_name": "edit_file",
    "tool_version": "schema-v1",
    "redacted_arguments": {"path": "src/math_tools.py"},
    "capture_mode": "metadata_only",
    "authorization_decision": "allowed",
    "side_effect_class": "workspace_write",
    "status": "ok",
    "duration_ms": 12.4,
    "result_ref": "artifact://demo-patch",
    "truncated": false,
    "error": null
  }
}
```

输入摘要应是允许字段构成的短说明，例如“修复fixture内边界函数；禁止修改tests”，默认不复制用户原文。若无授权安全摘要可留空，并记录 `input_capture=omitted`；为了省存储截断秘密并不等于脱敏。

### 6.4 状态、计量与完整性规则

**结果四轴：** `execution_status = running/completed/failed/stopped/cancelled/abandoned`；`verification.outcome = pass/fail/not_run/inconclusive`；`policy_outcome = satisfied/violated/not_checked`；`infra_outcome = healthy/error/unknown`。`resolved`只供外部官方评测语义。对需安全拒绝的任务，`stopped+pass`合法，必须由task合同解释。

**费用分层：** 每个唯一model attempt记录已知usage；run汇总本地调用，task按root及child的唯一`(invocation_id, attempt_id)`汇总全部实际尝试；用户整体成本含摘要、路由、恢复judge、标题、embedding、重试和失败尝试。工具/container费用若无计价，只报资源时间。invocation用于逻辑关联，attempt是供应商实际尝试；同一attempt的路由层/循环层记录只计一次，不能再叠加其逻辑调用总usage。价格目录按有效日期、provider、model、输入/输出/缓存计价保存快照；费用是估算或账单值必须标明。`usage_coverage`以有usage的调用数/应计调用数计，`cost_coverage`另计；只有可证明免费的scripted才写0。未知不作免费，partial金额只是已知部分，不作完整成本排序。

**延迟分层：** 用户墙钟（接收→最后响应）、任务完成（包含异步交付）、模型/工具累计工作时间、准备/校验/排队分别报。parent工具等child时，该等待包含child时间，不能再相加当总延迟。仅在span完整时算关键路径，缺边标unknown。

**可靠写入：** 保留JSONL格式，首版约束同run通过同一TraceStore、同一进程序列化写入；当前只有每实例RLock（`trace_store.py:31,82–89`），没有跨实例/跨进程单writer保证。结束产物用现有原子替换。先生成稳定event_id再发出，reader校验schema/seq并幂等去重。异常退出由离线reconciler标abandoned/open_span，不能伪造completed。生产遥测写失败可受控降级并计数；评测必须标incomplete，不将缺指标样本静默删除。只在实测并发写入需要时才引入专用writer队列。

### 6.5 大文件、敏感数据与保留策略

复用 `ArtifactStore/ToolResultStore` 的ID、大小和hash能力，但新增**绑定每次引用或scope的用途、授权和期限记录**。`runtime/context/artifacts.py:148–156`对相同字节复用第一份不可变metadata，因此不能直接覆写内容hash对应的唯一metadata。引用格式不带可直接访问的公网URL/签名token；resolver必须核验用户、任务scope和用途。正文hash用于完整性，含个人信息的短输入做租户隔离HMAC或不记录，不能声称hash即匿名。公开索引只放脱敏信息，原始artifact独立访问控制；到期先撤销对应引用，再清理没有存续合法引用的正文并写tombstone；若涉及数据主体撤回，按撤回范围检查所有引用/副本，不能仅取消一条链接。避免一个任务过期删除另一个任务合法使用的共享正文，也不能让坏引用触发越权兜底。

| 级别 | 默认采集 | 建议保留（待项目裁决，非法律结论） | 用途限制 |
|---|---|---|---|
| 生产metadata_only | 事件ID/状态/耗时/usage/版本/脱敏参数形状 | 30天；无身份聚合90天 | 调试与趋势，不可自动转训练 |
| 单任务受控debug | 用户许可范围内的必要输入/输出artifact，秘密仍过滤 | 7天，到期删正文和索引引用；延长要明确期限 | 必须有ticket/purpose，不能“失败就自动全量采集” |
| 合成/开源fixture评测 | 可重放数据+镜像/依赖摘要+patch+grader结果 | 90天或最近两个基线周期 | 单独确认任务、代码、依赖许可证；保留manifest可更长 |
| 经批准训练快照 | 去标识化样本、来源许可、标注和split血缘 | 每数据版本明确到期/撤回策略 | 运行授权不等于训练授权；撤回要追踪导出和模型版本影响 |

保留天数是默认提案，可按真实任务频率调整。最小实现用定时清理清单和dry-run清理报告即可，不引入数据湖。观测、session上下文、artifact、索引与备份须有统一删除清单；只删trace文件不会删除其它副本。已有access控制是否覆盖所有artifact读取入口仍需专门安全任务确认。

OpenTelemetry GenAI公开规范建议默认不采集完整指令/输入/输出，允许显式选择，并把耗时操作与瞬时事件区分。可借其字段语义做未来导出；规范仍在演进，本项目先固定自己的`trace.v1`，不把外部字段改名扩散到业务代码。访问时官方GenAI文档已迁移至独立仓库，仓库公开、Apache-2.0；本轮未固定该main提交，因此只借设计原则，不宣称已兼容某个SDK版本。[GenAI spans](https://github.com/open-telemetry/semantic-conventions-genai/blob/main/docs/gen-ai/gen-ai-spans.md)、[许可](https://github.com/open-telemetry/semantic-conventions-genai/blob/main/LICENSE)、[迁移说明](https://opentelemetry.io/docs/specs/semconv/gen-ai/)（访问2026-09-27）

## 7. 最小可观测性方案

**先形成五个可回答的问题，不先增加dashboard。** 在既有 `trace_summary.json/md`、`metrics.json` 和关系索引基础上提供：

1. 给定task/run，列出最终验收、未恢复incident、最后可靠步骤及证据引用。
2. 给定失败类别，列出受影响任务、实际模型/配置、首次出现版本，区分agent、grader、环境错误。
3. 给定run，分解主/子/辅助模型、工具、上下文、准备/验证成本时间，显示unknown比例。
4. 给定A/B实验，列出同题同repeat的改善/回退/一致，链接相应diff和验收，保留所有attempt。
5. 列出data_quality不合格运行、坏artifact引用、缺失版本、未知usage及过期待清理数据。

最小汇总字段：`verified_pass_rate`、`policy_violation_count`、`recovery_success_rate`、`run_terminal_status_counts`、`median/p90 wall time`、`known_cost_per_success + cost_coverage`、`usage_coverage`、`trace_completeness`、`grader_error_rate`。样本太少时报告每条耗时及最大值，不输出看似稳定的p99。不要以工具调用越少一律越好；少调用也可能因为提前放弃。

首版只对以下条件生成待处理列表：任何越权副作用、grader失败、必需manifest缺失、未终止运行、unknown费用占比高于实验预设阈值。同一incident聚合，不为每个临时网络错误报警。阈值由任务预算和基线决定，不能凭空宣称已满足某个SLO。

**最小日志落地顺序（后续实施）：** 统一结果字段→run manifest→复用模型/工具起止埋点→独立验证事件→统一脱敏→完整性检查→单页实验对比。为 legacy数据设置 `schema=legacy, provenance=unknown`；只回填可由原记录证明的字段，不生成假模型revision或假成本。

## 8. 从轨迹构建微调数据

本任务提供数据合同与验证方式，**不选择微调模型、方法、训练规模或业务架构**。先证明有一个可测、可学习的组件瓶颈，再决定是否值得训练。

### 8.1 字段可用性与标签

| 数据 | 可直接用于什么 | 需要补充什么／禁止推断什么 |
|---|---|---|
| task/run/event/call ID、版本、工具schema、状态、耗时/usage | 血缘、筛选、成本分析、分组去重 | ID/hash/成功标志通常不应作为模型输入特征，避免背题或通过标签泄漏 |
| 调用前实际可见消息、工具列表、合法记忆片段 | 经授权且未丢失必要上下文时，构建真实输入 | metadata_only不可恢复原输入；后处理摘要不自动等价于当时输入；必须有cutoff_seq和可见性证明 |
| 工具调用及参数、结构化输出、摘要 | 候选训练目标 | 需标正确性、合法性、事实支持、是否关键步骤；工具成功执行不代表选对工具 |
| verifier断言、测试与patch | 对结果的客观标签、证据 | patch通过可见测试不代表无副作用；不得把未来验证结果放入此前的输入 |
| 人审纠正/对比结果 | 修订目标、偏好或错误分类数据候选 | 分离人工原创与模型输出，记录rubric/审查者/分歧；对比需相同可见输入，不能跨题凑偏好对 |
| failure/incident/recovery | 负例和恢复任务 | 网络/环境失败不等于模型动作错误；安全拒绝可能是正确正例；不能把失败后的补救信息回填到失败前 |
| scripted/gold演示 | 独立的合成演示源或grader测试 | 标`source=scripted/reference`；不能冒充模型成功轨迹，也不能与正式holdout/test split同源进入训练 |

### 8.2 最小数据流程

1. **准入：** 来源/授权/许可证明确；采集模式允许训练；无秘密、跨用户内容、未知来源artifact。仅有metadata的run用于错误统计，不强行恢复为SFT样本。
2. **锁定切片：** 对目标动作t，仅取t之前实际可见context、工具schema和当时可见的检索结果。记录 `input_event_ids, cutoff_seq, context_snapshot_id, memory_snapshot_id`；目标为可观察动作/答案，不采集或要求私有思维链。
3. **验收筛选：** 成功轨迹要求独立grader通过、scope合法、预算有效且证据完整；再逐步审查是否有绕过/偶然成功。高成本但正确样本单列，不因效率阈值丢掉所有难例。
4. **失败筛选：** 保留代表性的语义/工具选择失败及修复片段；对纯infra缺陷打标签，不作为模型负例。若训练恢复策略，输入可以包含当时已经发生的失败，但不能包含未来修复答案。结果不明进入quarantine。
5. **标注：** 先标50–100个候选步骤，评估一致性和错误类型占比；这只是数据质量试标建议，不是训练规模承诺。人工标正确/错误/不确定、事实引用、合法性和适用scope；LLM建议标签需人工抽审。
6. **去重与分割：** 先按原始repo/issue/用户任务链/fixture family/记忆来源成组切分，再做精确哈希和近重复检查（规范化prompt、AST/diff指纹、相似描述）。不同repeat、工具同义改写、同修复不同语言描述也不能跨split。小规模先用hash+人工近重复审查，不必上向量库。
7. **导出：** 每条训练行只含必要input/target，另有sidecar保存来源event、授权、标签、split、去重组和构造器版本；生成不可变dataset manifest。模型/训练参数由其他任务选择，但必须回写训练dataset digest、训练代码版本及模型产物digest。
8. **验证：** 留出集不参与提示词调优、检索、训练或LLM judge校准；比较原模型、相同提示词下的候选模型及必要的提示词改进基线。训练收益须在目标组件与端到端同时检查，避免仅格式更好、业务更差。

### 8.3 泄漏的具体例子

```text
时刻 t0：用户给出失败现象
时刻 t1：Agent读到 src/a.py
时刻 t2：Agent选了错误修改位置       ← 待训练的动作
时刻 t3：隐藏测试给出正确文件线索
时刻 t4：人工写了正确patch
```

训练t2的输入只能含t0/t1及此前合法可见上下文；t3不能塞进“任务摘要”，t4可以在标注后作为替代目标，但必须确认t0/t1足以决定该目标，否则应构建更早的“先读取更多信息”动作。隐藏测试原文不导出给模型。模型看到工具失败后学习恢复时，允许使用那条已可见失败，仍禁止使用后来的裁决。

训练集自身可以有独立验证器提供标签；禁止的是正式holdout/test的题目、答案和判题反馈跨split进入训练，不是禁止所有经过测试验证的训练样本。

公开SWE题即使未加入本地训练，也可能已在基础模型预训练中出现；“本地去重通过”不能证明无预训练污染。报告为`public/exposure_unknown`；用新近独立需求和holdout提升可信度，不承诺绝对干净。每次泄漏事件登记并使相关holdout退出正式结论，不静默换题挑好成绩。

## 9. 实验协议与不确定性

### 9.1 固定基线、一次一个主要因素

1. **预登记。** 写清假设、主指标、任务集合/分层、样本数、重复数、预算、停止条件、基础设施失败处理、人工rubric、允许变化项与判定门槛。其余代码/配置固定；不要同时换模型、摘要策略、记忆库和工具权限。
2. **验证grader/环境。** 无模型运行reference solution、原始坏例和作弊控制；若grader变动，A/B都在新grader重新评估，不能只重评候选。明确profile为mock、local-isolated、official-container中的哪种。
3. **固定基线。** 代码SHA+工作区patch、数据/fixture/prompt/tool-schema/config/environment/价格版本；记录实际模型revision可用程度、seed与不可控随机性；禁用未计划的路由漂移，或把整个路由策略作为比较对象。
4. **配对执行。** A/B使用相同task和repetition block，交替/随机顺序运行，固定并发和时间预算；每trial重新建隔离状态。跨时间段均衡，记录provider故障时段。
5. **完整收集。** 失败、超时、取消、token未知、grader错误都进入attempt ledger。不得“跑到过了为止”后只报最好结果；重跑须新attempt_id及原原因。
6. **复核与报告。** 展示主结果、成本/延迟/安全约束、分层、差异题、全部负面结果和适用范围。不能把“没达到显著”写成“完全等效”。

### 9.2 指标及统计口径

对task i、配置c的第r次尝试，先记原始`outcome=pass/fail/inconclusive/not_run`及`valid`。用户视角`y_delivery∈{0,1}`仅对已确认符合合同的交付取1，其余取0，称“已验证交付率（保守）”，不把未知标成模型错误；能力视角`y_ability`在环境/评分无效时为null，有效时为0/1。先计算每题均值，再对题宏平均；按预登记权重另报实际流量加权值。不同难度、不同layer、冷/热记忆、offline/networked分开。scripted和真实模型、组件与端到端不汇成一个排行榜总分。

- **成功率：** 单次部署策略下的每题平均pass；A/B用同题差 `d_i=mean_r(y_ability_B)-mean_r(y_ability_A)`，仅在A/B共同有效的重复配对上计算。重复是同题内观测，不能把20题×3次当60个独立任务。
- **不确定性：** 首版按task/family为簇做配对bootstrap置信区间（抽family保留其中全部任务、重复和A/B配对，再重算预登记的task宏平均），记录方法和重采样seed；独立family很少时区间仅作探索性描述；题数小时同时列全部差异题与每题成功次数。Wilson区间可用于单次独立题样本的辅助描述，不能忽略相关重复。
- **稳定性：** 每题 `0/3,1/3,2/3,3/3` 分布；“三次都过的题比例”与“至少一次过的题比例”分别报，不混称pass@1。三次只粗测波动，不能证明长期99%可靠。
- **多次尝试策略：** 若产品允许最多k次，用实际可用的选择/验证策略评估，并计全部尝试成本；隐藏grader事后挑最好的oracle pass@k只作能力上界，不作用户成功率。独立同分布假设不成立时，不用简单幂公式假装准确。
- **成本：** `所有尝试的已知费用 / 验证成功数` 并列计量覆盖率与失败成本；成功数为0时记undefined，不能记0。另报每题中位成本及预算超限率；不能只比较成功样本平均成本。
- **延迟：** 同题配对的墙钟差、完成时间分布、超时率和所有样本上限；失败不能被过滤后只剩更快的成功。少量样本不报精细尾百分位。
- **恢复能力：** 在预定义可恢复故障子集中恢复并验证成功数/注入数；另报额外调用/时间和重复副作用。在永久故障题测有界终止与正确说明，不强求恢复成功。
- **基础设施错误：** 已验证交付率以所有计划且启动的trial为分母；另报条件于环境/评分正常的能力率。A/B能力差只在共同有效配对子集计算，不能各自删除不同题后比较；公开排除清单、计数、各配置无效率与重跑规则。grader错误标inconclusive，不能写模型fail或静默删掉。已计划未启动也报告执行覆盖率。

**示例（虚构演示，不是本项目成绩）：** A在20题×3次中45次通过，B为47次通过，不能仅据75.0%→78.3%就宣布升级。若新增2次通过均来自同一道题，而安全题出现一次越权，应暂停；若配对区间跨0，则记“收益不确定”，按预登记方案增加独立任务/重复或保留基线。20题不适合证明微小差异。

### 9.3 发布/采纳门槛的建议

首版硬门槛为：grader/reference检查正常、核心无模型回归通过、禁止副作用为0、manifest完整、关键验收证据可追溯。**0次观察到违规不等于总体风险为0。** 主收益优先看配对成功率和事实质量；成本/延迟容忍值按任务预先填写。

拟议的效率实验可预登记“质量下降容忍不超过5个百分点，已知计量覆盖率至少95%，成本下降至少15%”；剩余未知费用必须有可信上界且最坏情况不改变节省结论，否则费用结论仍为不确定，不能只靠覆盖率门槛采纳。这些是可讨论的工程阈值，不是已证实SLO。只有非劣性区间支持容忍界且安全硬门槛满足才采纳；小样本达不到证明要求就报告不确定。增加样本的规则在看结果前写好，避免反复查看直到碰到显著。

架构/上下文/记忆/微调任务都用这个协议，但各自指定主要因素。例如记忆实验先比相同模型、相同合法历史下的off/on；摘要实验比同一上下文预算下的策略；小模型实验先比较目标组件，端到端验证分布外损失。本任务不替它们选择实现。

## 10. 实施路线、现成命令与扩展条件

### 10.1 最小可行改进（先做）

| 顺序 | 具体交付（未来实现） | 复用位置 | 完成标准 |
|---|---|---|---|
| 1 | 统一runtime/verification/infra/policy结果；官方逐题结果导入；patch完整性及单题verifier异常收集 | `evaluation/metrics.py`、harness/adapter、Verified脚本、`runtime/trace/failure.py` | P01/P07结果与恢复例、P10导出/超时例通过，旧字段有兼容说明 |
| 2 | 无敏experiment/run manifest，实际模型及计量unknown | matrix/harness、model_invocation/ModelTaskRunner、TraceStore | 每trial可定位代码/配置/数据，A失败B成功归属准确 |
| 3 | 统一脱敏与artifact引用策略；事件schema/ID/seq | TraceStore、ToolResultStore、既有ContextEvent | 合成canary不泄漏，坏trace/坏引用明确报错 |
| 4 | 固定12个必测案例并验grader；整理为48题合同 | 已有tests+fixture，不改业务架构 | 既有正确解通过、错误/作弊解失败；层次和来源分组可检查 |
| 5 | A/B配对报告与人工评审表 | 现有matrix rows/report | 所有尝试都列出，分层CI与负面案例可追溯 |

建议工作量只按交付拆分，不承诺未实测工期；一次PR只处理一个可验证目标。当前文档不实施上述改动。

### 10.2 已有可用入口与使用限制

当前已有：`scripts/run_evals.py --runner scripted`、`scripts/run_system_evals.py --runner scripted`、`scripts/run_coding_agent_matrix.py`、SWE-bench及RAG脚本。**本轮均未执行。** 将来在获准的临时隔离checkout、已有依赖和无真实凭证/共享DB的环境中，可先运行：

```bash
python scripts/run_system_evals.py --runner scripted --task-id coding-git-diff-008 --quiet
```

该命令会写评测产物、复制fixture，并在临时fixture内初始化/提交基线（`harness.py:511–522`），harness开头还调用sandbox清理（`:108`）；它不是只读检查，故本轮没有运行。不要把它直接当“不产生副作用”的命令放进当前个人工作区。`--context-repetitions`等参数目前不实现重复；需要实际matrix的repetitions或后续修正入口。

`evaluation/coding_agent_matrix.example.json:7` 注释建议关闭real，实际 `:23–25` 为 `enabled=true, runner=real`；因此不能照搬示例当免费smoke。执行前读取展开计划、明确所有调用/镜像/时间预算；`dry-run`也需核查是否加载数据或创建目录，而非只凭名字认为只读。

### 10.3 有明确收益后再做的增强

| 触发条件 | 扩展 | 需证明的收益 |
|---|---|---|
| 本地端到端达到稳定高分但真实需求仍失败 | 增加新需求family、少量外部基准 | 新题确实区分候选，不只是更贵 |
| 摘要/检索错误成为主要失败类别 | 扩充事实与相关性标注、按预算扫k值 | 下游成功/恢复收益大于token/延迟代价 |
| 手读JSONL频繁、跨服务定位困难 | 在现有事件出口增加固定版本OTel导出 | 关联效率提高，采集开销和权限可控 |
| 文件/索引查询实测超过可接受时间或跨机需求出现 | 批量入库/对象存储、异步writer、索引优化 | 保持可搬移/可删除和低维护成本 |
| 有稳定组件错误、足够合法样本和干净holdout | 训练数据导出、标注与小规模微调验证 | 同预算下优于提示词/规则基线且端到端无回退 |

当前不建议：为评测引入多Agent裁判团、强制所有任务多Agent、全量向量化日志、微调自动闭环、以线上用户原始对话直接训练、Kafka/分布式工作流重写、每次提交运行完整公开重型基准、只看排行榜选框架。也不建议为了统一schema重写业务TaskState/上下文事件系统。

## 11. 可直接复制的实验记录模板

### 11.1 实验卡

| 字段 | 填写值 |
|---|---|
| experiment_id / owner / 日期 | |
| 假设（只写一个主要变化） | |
| baseline → candidate | |
| 不变因素／计划改变因素 | |
| agent代码SHA、dirty patch digest | |
| harness / verifier版本与digest | |
| task清单、dataset/fixture digest、split/family | |
| prompt/tool-schema/config digest | |
| requested / resolved model、revision来源 | |
| provider路由/fallback策略、sampling、seed支持情况 | |
| OS/架构/Python/lock/image、CPU/RAM、并发 | |
| 网络、memory/index snapshot、缓存/reset协议 | |
| n任务 × repetitions × 配置；顺序随机种子 | |
| 每题与总预算：token/费用/墙钟/工具timeout | |
| 主终点、次终点、安全硬门槛、非劣容忍值 | |
| CI方法、追加样本/停止规则 | |
| infra/grader异常与重跑规则 | |
| 采集授权、脱敏版本、保留期限 | |
| 结果路径、人工评审者/盲审方法 | |

### 11.2 逐trial记录表

| task_id/family | config/repeat/run_id | execution | verification | policy/infra | 失败incident与恢复 | 质量人工分 | usage/覆盖率 | 费用/计价版本/完整性 | 墙钟/准备/验证ms | patch/证据ref | 人审结论 |
|---|---|---|---|---|---|---|---|---|---|---|---|
| | | | | | | | | | | | |

### 11.3 实验结论表

| 指标或裁决 | Baseline | Candidate | 配对差与区间 | 限制/证据 |
|---|---|---|---|---|
| 完成trial数 / 计划数 | | | | |
| 全部已启动trial已验证交付率 | | | | |
| 环境有效子集能力率及排除数 | | | | |
| 每层/每难度成功率 | | | | |
| 每题0/3、1/3、2/3、3/3 | | | | |
| 安全违规 / 正确安全停止 | | | | |
| 恢复成功 / 重复副作用 | | | | |
| 人工质量分与分歧 | | | | |
| 全尝试费用/成功数 + 计量覆盖率 | | | | |
| 墙钟中位/尾部/超时 | | | | |
| grader错误 / trace不完整 | | | | |
| 改善题 / 回退题 / 不确定题 | | | | |
| 负面结果、未通过假设 | | | | |
| 决策 | 保留基线 / 采纳 / 继续验证 | | 事先门槛是否满足 | |

### 11.4 失败/负面结果卡

```text
案例ID、run_id、配置版本：
期望结果与实际观察：
证据event/artifact/verification ID：
最早异常、最终阻断、已恢复incident（分别填写）：
事实：
根因假设（注明heuristic/人审，未验证写未知）：
是否为grader/环境问题，是否影响对照组：
重跑原因及全部attempt ID：
建议改动及可能负作用：
何时不值得修：
用于验证的新增案例/验收：
是否有资格进入训练集、授权与split：
```

## 12. 关键资料、待确认问题与协调接口

### 12.1 关键资料及支持的具体结论

所有网络资料访问日期为2026-09-27；无发布日期的文档按访问日期定位。代码仓的可变main链接仅支持访问时内容，正式实验需补不可变revision。

| 资料 | 日期/版本 | 支持结论及使用位置 |
|---|---|---|
| 本项目源码：`evaluation/harness.py`、`verifiers.py`、matrix、swebench adapter、trace/context/models | HEAD `5bc28e8d883a6ab4daf7e8d734ca3a1bcfbe24a2` | §2/3的现状、判定与计量缺口；引用行号是实现证据。历史运行只支持“存在这些产物”，不支持当前HEAD成绩 |
| [Anthropic agent eval工程文](https://www.anthropic.com/engineering/demystifying-evals-for-ai-agents) | 2026-01-09 | outcome与transcript分离、多类grader及随机性；不支持本项目已采用这些方法 |
| [Anthropic基础设施噪声](https://www.anthropic.com/engineering/infrastructure-noise) | 2026-02-05 | 固定资源与超时、区分保障与上限；其数值是自家实验，非本项目预测 |
| [SWE-bench仓库](https://github.com/SWE-bench/SWE-bench)、[固定grading.py](https://github.com/SWE-bench/SWE-bench/blob/02e7a74ffd0b707aab73d203fe87bdc7c76afc8e/swebench/harness/grading.py) | 核查commit `02e7a74…`，2026-09-02 | MIT状态、环境建议、独立测试判定和缓存限制；§4.2/4.3 |
| [Verified数据卡](https://huggingface.co/datasets/SWE-bench/SWE-bench_Verified)、[Lite数据卡](https://huggingface.co/datasets/SWE-bench/SWE-bench_Lite) | 访问2026-09-27 | 500/300题及公开任务定义；不提供本地模型无污染保证 |
| [OpenAI Verified审计](https://openai.com/index/why-we-no-longer-evaluate-swe-bench-verified/) | 2026-02-23 | 偏失败子集存在任务质量与污染证据；不能将子集缺陷比例外推全量 |
| [OpenAI Pro后续审计](https://openai.com/index/separating-signal-from-noise-coding-evaluations/) | 2026-07-08 | 旧Pro质量争议及撤回推荐；不得直接套到V2 |
| [Terminal-Bench 2.0论文](https://arxiv.org/html/2601.11868v1)、[任务许可](https://github.com/harbor-framework/terminal-bench-2/blob/main/LICENSE) | 2026-01-17；任务参考commit `2fd12b88…`非2.0锁定版本 | 89任务、最终环境判定、参考解与任务审查方法；公开数据仍有污染风险 |
| [Harbor任务定义](https://docs.harborframework.com/core-concepts/tasks/overview)、[资源配置](https://docs.harborframework.com/core-concepts/tasks/configuration)、[仓库](https://github.com/harbor-framework/harbor) | 访问2026-09-27 | instruction/environment/tests与reward、逐题资源合同；支持借数据格式，非必须安装平台 |
| [SWE-bench Pro数据卡](https://huggingface.co/datasets/ScaleAI/SWE-bench_Pro)、[V2协议](https://github.com/scaleapi/SWE-bench_Pro-os/blob/main/v2/README.md) | 自报v2.0.0，2026-09-22；本轮未取得V2不可变SHA | 642/731/51版本区别、50分钟预算、许可分层、干净验证；修订效果尚未独立验证 |
| [mini-SWE-agent固定Agent模块](https://github.com/SWE-agent/mini-swe-agent/blob/04d809ceab9df28f9adaed044884180159172930/src/minisweagent/agents/default.py)、[批处理模块](https://github.com/SWE-agent/mini-swe-agent/blob/04d809ceab9df28f9adaed044884180159172930/src/minisweagent/run/benchmarks/swebench.py) | commit `04d809ce…`，2026-09-03；MIT | 版本/配置/退出/费用/轨迹分开，恢复跳过已有结果的边界；§4.3 |
| [OTel GenAI spans](https://github.com/open-telemetry/semantic-conventions-genai/blob/main/docs/gen-ai/gen-ai-spans.md)、[LICENSE](https://github.com/open-telemetry/semantic-conventions-genai/blob/main/LICENSE) | 访问2026-09-27；Apache-2.0；本轮未锁定main SHA | 默认不采原始内容、字段语义参考；本文自己的trace.v1不宣称与某个SDK现成兼容 |

### 12.2 尚未确认的问题与待验证假设

1. **真实需求分布未知。** Coding、聊天、文档、RAG、长任务各占多少，常见成功条件与可接受预算尚无经授权统计；48题配额是起点，需要实际任务抽样校准。
2. **历史结果来源未知。** `.runs/.evals`缺完整版本清单，部分running和缺报告原因未确认；不能将这些状态归因为当前缺陷。
3. **部署provider能力未知。** 本轮未读取真实.env或发请求；model revision、seed、失败usage、缓存计价和账单接口是否提供需以后按实际provider验证。
4. **实际调用范围待核对。** 除已定位ModelTaskRunner标题外，所有摘要/路由/检索改写/embedding调用是否都能统一归因，需要实施时盘点调用边界；不假定此次静态清单已覆盖全部费用。
5. **重放和权限边界待安全任务确认。** artifact读取、session持久化、memory namespace、子进程环境与网络隔离在真实部署是否完全符合评测合同，本轮未端到端复现。
6. **公开基准版本未全部锁定。** Pro V2 SHA与文件校验和、Terminal-Bench实际registry任务digest、官方CLI与本地适配兼容尚待执行前确认；不因此阻塞本地评测方案。
7. **语义瓶颈是假设。** 是否压缩导致事实丢失、记忆确实提升成功率、小模型微调是否比提示词/规则便宜有效，都要按§9验证，本文没有性能收益承诺。
8. **统计功效待先导数据。** 尚无当前版本每题方差、family相关性和环境失败率，不能为20题×3次保证检测某个效应；先导后预登记扩样规则。
9. **隐私与训练资格待定。** 哪些数据允许保留/外部处理/训练、保留周期与撤回流程、provider协议以及上游许可需项目负责人确认；文档不将运行许可推导成训练许可。

### 12.3 与其他任务的接口或依赖

| 协作任务/责任方 | 它提供给评测的最小接口 | 本任务提供 | 边界 |
|---|---|---|---|
| 架构/Agent编排 | task/actor/父子关系、状态转换、取消/恢复合同、可注入故障点 | 分层case合同、运行/验收语义、指标和实验manifest | 不要求多Agent，不替其重写生命周期 |
| 上下文/摘要 | context version、来源event区间、snapshot状态、事实/约束集合、预算与估计器版本 | 摘要保真集、后续行为oracle、成本/恢复A/B协议 | 不决定压缩算法或窗口策略 |
| 长期记忆/检索 | scope、memory ID/version、检索库snapshot、过滤/选择理由、合法历史初始化/reset | pooled标注、相关性与下游收益分离、防跨trial泄漏 | 不要求向量数据库，不决定embedding/检索算法 |
| 小模型/微调 | 目标组件、候选模型不可变版本/训练配置、数据需求与许可 | 可训练样本合同、cutoff/split/lineage、基线/负例与holdout协议 | 不选模型、微调方法或启动训练 |
| 工具/安全 | side_effect_class、权限结果、幂等/恢复能力、边界策略 | canary与指令冲突评测、状态验收、脱敏/保留合同 | 不把观测替代真实sandbox或授权 |
| Web/网关/产品 | request/session关联、用户可感知开始结束、交付验收定义 | 五类查询、统一结果与unknown展示 | 不先重写UI，避免把内部评测字段暴露成用户负担 |
| 汇总任务 | 真实优先级、预算、维护人、跨任务共同版本 | 本文全部事实证据、待裁决表与最小实施顺序 | 其它任务文件本轮均未修改 |

这些接口可独立逐步落地，未等待其它任务结论才设计；若接口变化，升级manifest/adapter版本，不追溯篡改历史结果。

### 12.4 建议交给汇总任务裁决的争议

| 争议 | 本文建议 | 需要裁决的取舍 |
|---|---|---|
| 先改业务架构还是先修评测判定 | 先修成功语义、patch完整性、grader抗绕过/异常收集 | 是否给评测P0留出独立交付，避免架构收益无法验证 |
| completed与pass旧字段兼容 | 保留execution，新增verification；旧数据未知不补假值 | UI/历史报告名称迁移范围 |
| 子Agent需不需要独立run | 首版兼容共享run+actor/task/span，仅真实独立run填parent | 不为观测强迫业务生命周期重构 |
| 生产原文采集与调试便利 | 默认metadata_only、必要任务选择性debug | 保留30/90/7天提案、授权流程、谁能看原文 |
| 私有holdout与单人维护成本 | 小而明确的family隔离留出集；个人延迟盲复核 | 允许接触测试的人员、轮换频率、是否需要双审 |
| 公开基准的角色 | 本地集主判据，Verified/Lite与Terminal少量外部校准，Pro V2后置 | 计算预算、匹配任务清单、是否值得维护Docker矩阵 |
| 5个百分点质量容忍、15%成本改善等阈值 | 仅作可讨论示例，必须实验前确认 | 业务能承受的回退、最小实用收益与扩样预算 |
| 微调与更简单改进的优先级 | 数据合格、组件瓶颈明确且优于提示词/规则基线后再考虑 | 数据授权、标注成本和长期维护责任 |

