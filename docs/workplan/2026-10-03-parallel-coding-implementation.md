# 多会话与隔离 Coding 实施合同

日期：2026-10-03。用户已授权按[调研方案](2026-10-03-parallel-coding-worktrees-research.md)自动实施、迭代和验收。此文件先记录设计，完成后补实际结果；不把设计项写成已实现能力。

## 实施边界

在现有 Web、CodingApplication、TaskState 与 RunState 上完成一个可用闭环：多会话后台运行、独立任务工作树、继续和分叉、显式取消、恢复订阅、可核验的集成交付与保守回收。保留用户当前未提交代码，不对用户仓库自动提交、stash、reset、合并或推送。源码基线已保存于本轮临时审计目录供差异核对。

## 唯一执行与状态归属

- Web AgentService 是单机唯一 supervisor，持有进程级排他锁，限制 worker 数量和等待队列；每个 worker 独立 runtime，避免共享可变会话和模型对象。
- 服务端生成 run_id；同用户 request_id 和相同输入幂等，同 session 已有活动 run 时拒绝第二次启动。RunState 保持执行权威，trace 索引是投影。
- 请求先持久接收再返回；run 的事件带递增 seq，订阅断开不取消。终态与消息片段可恢复，重启发现未确认退出的任务标记 interrupted 并隔离其工作区。
- 取消先停止进程集合，确认退出后释放资源；无法确认不自动重跑，不通过租约超时推断文件写入已经停止。

## 工作区与交付

- Web 新 Coding 任务默认 worktree，从明确已提交版本启动，界面说明不包含源目录未提交变更；in_place 保留为显式兼容选择。
- new 创建受管 task/worktree，continue 复用，branch 同时分叉任务与明确源码版本；稳定 repo_id 不随工作树路径漂移。
- 受管 worktree 单写，互相独立；外部重叠目录保留保守互斥。管理 Git 操作短锁；命令环境私有临时目录，无法隔离的服务资源串行使用。
- 交付固定源码提交；独立集成工作区产生精确候选，再记录命令、环境、输入与结果。通过测试与 run 完成分开表示。
- 自动 ref 更新仅允许平台所有且未在任何 checkout 检出的目标，带 expected-old 校验；用户主 checkout 通过独立集成分支接纳。脏、活动、未保全的工作树禁止清理。

## Web 合同

为避免与既有管理员 trace 查询混淆，后台交互 API 使用 `/api/chat/runs`：POST 接收、GET 列表；`/{run_id}` 查询；`/{run_id}/events?after=N` 订阅 NDJSON；`/{run_id}/cancel` 显式取消。所有操作按当前登录用户授权。旧 chat API 只适配同一执行入口。

提交沿用 message/session_id/workspace_root/attachments/thinking_enabled/model_profile，增加 request_id。Coding 配置沿用嵌套 coding_task，含 mode、task_id、workspace_mode、base_ref。continue/branch 不从浏览器全局设置改写既有 workspace。

前端按 session 保存草稿、附件和会话缓存，按 run 保存连接、游标与输出；当前页面只选择视图。后台状态可见，A 完成只能更新 A，网络不确定重试复用 request_id。

## 分工与验收

后端运行服务、工作树生命周期和前端由三个 Agent 并行实现；主线程接 HTTP、交付验证与整体审查。所有测试只用临时 Git 仓库、fake worker/model、隔离 PostgreSQL 和本地预览，不访问真实服务或读取真实 .env。

必须验证跨 session 真并行、同 session 顺序约束、同工作区单写、工作区相互隔离、断线恢复、输出归属、取消后代、启动/退出崩溃、脏源保留、continue/branch、候选版本固定、目标变更重验、跨用户拒绝以及保守回收。最后运行后端、前端类型/测试/构建和真实浏览器交互，并记录证据。

## 已实现的闭环

实施代码已完成。当前规范移至 [PARALLEL_CODING.md](../architecture/PARALLEL_CODING.md)，本文件记录本轮范围与验收。

| 原问题 | 本次实现 | 主要代码 |
|---|---|---|
| 单会话流拥有运行生命周期 | 持久接收、独立进程、事件重放；切换和断线不取消 | `web/agent_service.py`、`web/run_worker.py`、`web/chat_runs.py` |
| A 完成覆盖 B 的页面状态 | session 缓存与 run 控制器分开，外层运行身份与 seq 去重 | `useChatRuns.ts`、`useSessions.ts`、`AppBootstrap.tsx` |
| 多会话同时写同一源码目录 | task 绑定 owned worktree，continue 复用，branch 分叉源码与任务 | `runtime/worktrees.py`、`applications/coding/{runner,session}.py` |
| 目录分开后仍争用进程/服务资源 | 私有命令环境，单机资源 lease，退出不确定时保留隔离 | `tools/command.py`、`runtime/workspace_lease.py` |
| completed 被当成测试成功或可直接合并 | checkpoint → 固定提交候选 → 命令验证 → 独立交付分支 | `runtime/worktree_delivery.py`、`web/coding_workspaces.py`、`CodingWorkspacePanel.tsx` |
| 新旧执行设计重叠 | 移除旧 AgentService、请求线程队列、旧单流 Hook 和无 caller 的标题模型服务 | `web/server.py`、删除 `useChatStream.ts`、精简 `web/session_titles.py` |

没有引入第二套 Job 状态模型：继续使用 RunState，journal 用于恢复同一状态，worker 的 report/index 为诊断投影。任务与目录绑定使用现有 task metadata，工作树 registry 只拥有目录/仓库身份与回收证据。

## 对抗审查与迭代中修复

三个子 Agent 分别实现和交叉复核后端运行、工作区与前端，主线程接路由和交付服务。迭代中处理了：

- Web 默认 worktree 只作用于新任务，旧 in_place 的 auto/continue 不被强制迁移；branch 显式切原目录时移除旧 binding。
- trace 子 run ID 不覆盖外层 admitted run ID，assistant segment/completed 字段与前端一致；未消费事件不能仅根据 DTO 的 last_seq 跳过。
- 旧 API 收敛为同一个 submit/events 服务；等待超时返回 202 和 run_id，附件解析移入 worker，session-only stop 被明确拒绝。
- Git 管理操作禁用 hooks，拒绝 external filters、merge drivers、fsmonitor；common Git identity 包含设备/inode，检测元数据替换。
- 资源争用返回明确的未执行结果；已存在且不可读的同 UID 进程不能被误判为新命令后代，新出现且无法确认的进程仍保守处理。
- worker 使用 Linux subreaper 与 PID 启动身份；当前 Python 无 pidfd 内置接口时采用 libc 等价路径。进程组退出但输出未完成时不释放资源。
- 目标分支变化使验证失效，发布用 expected SHA 的 ref 事务；只创建平台生成的候选分支。验证改源码、非零退出、冲突和中断均不能发布。
- preparing/verifying 的进程身份失效时展示 interrupted；保留现场，不自动重放操作或清除未知写锁。
- Runs 页 queued/cancelling 自动轮询，筛选包含 interrupted；交付面板不会对中断旧候选提供发布按钮。

源码替换曾因缺少验证证据被自动审批拒绝。先在隔离副本运行 134 项定向和 1,343 项全量回归，通过后重新提交相同替换，获准完成；没有绕过审批。相关前置证据保存在本轮 evidence 的 `pre-replacement-*` 文件。

## 验收方法与使用边界

所有后端回归均使用排除真实 `.env` 的源码副本、清空凭据的环境和新建 PostgreSQL Unix socket。Python 网络与数据库访问加审计限制；这是测试夹具约束，不宣称 OS 沙箱。临时数据库在 finally 中停止，检查 pidfile/socket 不再存在。没有安装项目依赖、调用真实模型或写生产数据库。

另有真实端到端 worker 验收：唯一替换的是模型池，保留 `worker_entry → run_agent → build_runtime → CodingApplication → registry/tools → PostgreSQL`。4 个独立进程完成模式切换、新建、继续、分叉；真实 read_file/bash 在私有 venv 中执行，来源目录的未提交内容保持原样。

真实 localhost HTTP 验收通过：两个进程同时运行、相同 key 返回相同 run、订阅断开后 run 继续、只停止指定 run、按 seq 重连、三个不同工作树，以及 checkpoint/prepare/verify/publish。浏览器工具返回 `apps=[]、browsers=[]`，故没有完成视觉验收；前端使用交互测试、类型/lint 检查、生产构建及实际静态资源 HTTP 响应验证，不能把这些称为真实浏览器点击验收。

单机 Linux 和协作式 Git/进程锁是当前交付范围。没有自动更新用户主分支，没有跨主机调度或 OS 级命令沙箱。异常隔离与脏/冲突目录保留，不自动 GC；私有 venv 不复制宿主依赖。新并发配置需要重启 Web 服务，技能文档的热更新仍按既有合同即时供后续调用使用。

最终测试日志、源码版本与汇总位于 [evidence/2026-10-03-parallel-coding](evidence/2026-10-03-parallel-coding)。可复现命令：

```bash
.venv/bin/python docs/workplan/evidence/2026-10-03-parallel-coding/validate_isolated.py \
  --source /path/to/project --python /path/to/project/.venv/bin/python
```

脚本依赖当前机器已有 PostgreSQL 16 工具和 Python 依赖，不启动 Compose。临时目录保留日志和源码快照以便审阅；服务端后台记录/受管 worktree 并未写入用户实际项目。

## 最终结果与版本记录

| 验收 | 结果 | 证据 |
|---|---|---|
| 本轮定向后端 | 145 passed | [parallel-targeted.log](evidence/2026-10-03-parallel-coding/parallel-targeted.log) |
| 整库快照 | 1,384 passed，27 subtests passed | [full-suite.log](evidence/2026-10-03-parallel-coding/full-suite.log) |
| 最后并发增量 | 190 passed | [final-delta-delta-confirmed.log](evidence/2026-10-03-parallel-coding/final-delta-delta-confirmed.log) |
| 真实 runtime/PG/工具烟测 | 1 passed，4 个真实 worker；仅模型池为 fake | [最终 worker 结果](evidence/2026-10-03-parallel-coding/final-delta-worker-summary.json) |
| 前端 | 60 passed；typecheck/lint/build 均 exit 0 | [frontend-results.json](evidence/2026-10-03-parallel-coding/frontend-results.json) |
| 实际 HTTP 闭环 | 并行、取消、重放、隔离、交付通过 | [http-http-acceptance.json](evidence/2026-10-03-parallel-coding/http-http-acceptance.json) |
| 当前文件校验 | 最后增量源码和前端构建均无哈希差异 | [summary.json](evidence/2026-10-03-parallel-coding/summary.json) |
| 清理 | 临时 PG 和预览服务已停止 | [最终 PG 清理](evidence/2026-10-03-parallel-coding/final-delta-cleanup-verification.json) |

验收期间检测到其他并发编辑在持续修改上下文、证据有效性和评测代码，均保留。记录分为整库快照和后续增量快照，不把不同版本测试结果合并成虚构的单次测试。中途两次捕获了新实现与旧测试夹具短暂不一致（read_file 默认参数、pressure tool receipts），更新后的统一副本已通过对应回归；红日志 `concurrent-*` 保留。

并发增量复核另修复两处已证明的评测边界：排除的 `.env/.env.*` 与 symlink 数据集不能被 fixture digest 读取，scripted 结果不能进入真实成本比较。4 个回归先红后绿，已纳入最后 190 项统一回归。没有以合成模型测试证明真实模型质量、上下文最优性或 KV cache 成本收益。

交付为工作区代码、生产静态资源和文档；未对用户仓库提交/推送或重启实际部署。更新后的 Web 服务需要按现有部署方式重启。视觉验收因无可用浏览器未完成，不能以 HTTP 和组件测试替代该项声明。
