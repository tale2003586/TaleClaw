# 自主可靠性迭代（2026-10-03）

状态：完成。7 类缺陷已修复并经独立审查，冻结源码完整后端测试 932 项通过，前端测试 27 项通过。

## 范围与基线

用户要求自主迭代当前项目。本轮沿现有可靠性改造继续检查可复现的问题，保留开始时全部未提交改动。没有改动前端设计，也没有提交、推送或部署。

- HEAD：`5bc28e8d883a6ab4daf7e8d734ca3a1bcfbe24a2`。
- 新建隔离源码副本的聚合 SHA-256：`301896082a2bf0bc9c30c7715296941ddf20ad52f54bec45829f50deae046f02`，与上一轮交付一致，详见[身份比对](evidence/2026-10-03-autonomous-iteration/baseline-identity.json)。
- 本轮基线完整测试：**895 passed / 1 failed**。失败为记忆确认后重复提案的候选去重；保留[原始日志](evidence/2026-10-03-autonomous-iteration/baseline-tests.txt)。不沿用上次 896 通过作为本次结果。
- 基线使用项目 `.venv`、新建 PostgreSQL 16、私有 Unix socket、剔除真实 `.env` 的源码副本以及阻止其他网络/数据库连接的 Python guard。临时数据库已关闭，见[清理回执](evidence/2026-10-03-autonomous-iteration/baseline-cleanup.json)。
- 前端现有依赖检查：`npm run typecheck`、`npm run lint`、`npm test -- --run`（27 项 / 11 文件）及 `npm run build` 均成功；前端源码与构建产物没有 Git 差异。

## 已证实的问题与修改

### 记忆候选匹配

原 `find_exact` 在候选和正式记忆中选更新时间最新的一条，然后服务才判断是否可合并。正式记忆、过期候选或尚未生效的候选较新时，会掩盖有效候选，导致重复创建记录或向尚未生效记录添加证据。

仓库新增可选 `status` 和 `now` 查询过滤，在选最新记录前先限定状态和有效期；命令服务按目标生命周期调用。未传过滤参数的旧导入查询保持原合同。内存仓库和 PostgreSQL 仓库实现保持一致。

新增参数化测试覆盖 active / expired / future 遮挡及查询过滤。修前 4 个内存反例失败；修后同既有审核边界合计 25 通过。PostgreSQL 对应 4 项参数化用例已在最终完整回归中通过。

证据：[修前](evidence/2026-10-03-autonomous-iteration/memory-before.txt)、[修后](evidence/2026-10-03-autonomous-iteration/memory-after.txt)。

### 根预算与流式取消

- 根预算：线程预检通过后，其他调用可能结算耗尽 token，或 deadline 在等待期间到期。现在在授予 model/tool attempt 的同一把锁内重新检查资源限制，拒绝多发调用。
- 流式取消：Chat/Responses 原来在读取下一块后才检查取消，回调中取消后仍可能进入新的阻塞读取。现在在读取前后均检查，已有 stream 所有权清理保留。
- 8 项新反例修前全部失败、修后通过；相关 provider/pool 定向 89 通过，无数据库 runtime 组合 124 通过。证据：[红测](evidence/2026-10-03-autonomous-iteration/runtime-red.txt)、[绿测](evidence/2026-10-03-autonomous-iteration/runtime-green.txt)、[集成](evidence/2026-10-03-autonomous-iteration/runtime-integration.txt)。

### 文件工具

- grep 枚举目录子文件时增加真实路径边界检查，跳过指向工作区外的文件；内部文件别名的显示路径和 glob 行为保留。
- read_file/write_file/edit_file 在打开前拒绝已存在的非普通文件，避免 FIFO 无限阻塞；不存在的目标仍允许创建，正常 UTF-8 操作和二进制解码报错保留。
- 9 项新增回归：修前 5 失败、4 通过，修后相邻工具组合 88 通过；独立审查另跑 9 项通过。FIFO 测试在临时目录及有超时和清理的子进程中执行。证据：[红测](evidence/2026-10-03-autonomous-iteration/tools-red.txt)、[绿测](evidence/2026-10-03-autonomous-iteration/tools-green.txt)。

### 评测判定与目录

- 文件变更验收必须有完整合法的 diff 证据；缺失/不完整/畸形证据返回 `inconclusive`，合法空 diff 保留正常通过。
- SWE-bench 实例 ID 拒绝空白、`.`、`..`，在删除、克隆或 Git 调用之前失败；危险分支回归均 mock 文件删除，临时哨兵文件保持不变。
- 11 项新增回归：修前 8 失败、3 通过；修后相邻评测组合 52 通过，数据库集成交最终全量。证据：[红测](evidence/2026-10-03-autonomous-iteration/evaluation-red.txt)、[绿测](evidence/2026-10-03-autonomous-iteration/evaluation-green.txt)。

## 独立审查

评测实施者只读审查记忆改动与查询合同，独立 25 项内存回归通过；runtime 实施者只读审查文件工具与进程清理，独立 9 项通过；工具实施者只读审查 provider/预算的流资源释放、锁和确定性回归。三项交叉审查均无阻断问题。主 Agent 复核新 diff 与调用链，并负责冻结后的整体验收。阶段定向计数存在重叠，不相加为总测试数。

## 最终验证

- 项目 `.venv` + 全新 PostgreSQL 16：**932 passed / 0 failed / 0 skipped**，pytest 用时 20.64 秒；另有 17 项关键路径定向通过。总数较原 896 增加 36 项，包含内存和 PostgreSQL 参数化用例，不能等同为 36 个独立产品缺陷。
- 最终源码 SHA-256：`f580beb09eec7a51ee86c529861f0da782947fa0688de4a7107b64f7aa6280d1`。完整套件之后逐文件复核，当前源码与测试快照一致；本轮共 14 个源码/测试文件变化，详见[变化清单](evidence/2026-10-03-autonomous-iteration/source-changes.json)及[完整性检查](evidence/2026-10-03-autonomous-iteration/integrity.json)。
- `git diff --check` 和变更 Python 文件 AST 解析通过。
- 临时 PostgreSQL 正常停止，socket 与 pidfile 均不存在；[最终回执](evidence/2026-10-03-autonomous-iteration/final-validation.json)、[清理证明](evidence/2026-10-03-autonomous-iteration/final-cleanup.json)。
- [完整日志](evidence/2026-10-03-autonomous-iteration/final-tests.txt)、[JUnit](evidence/2026-10-03-autonomous-iteration/final-tests.xml)、[网络/数据库 guard 验证](evidence/2026-10-03-autonomous-iteration/final-guard.txt)。

可复现完整后端检查：

```bash
.venv/bin/python docs/agent-design-review/implementation/evidence/final/validate_isolated_pg.py --source . --python .venv/bin/python
```

该脚本使用已有依赖，新建 `/tmp` 副本和私有数据库，在 finally 中清理服务。前端检查在 `web/frontend` 执行上述四条 npm 命令。所有实现仍留在原工作区，没有 Git 提交、推送或部署。

边界：本轮没有调用真实/付费模型、连接既有数据库、安装依赖或运行正式 SWE-bench；本地通过不代表真实任务成功率或生产部署效果。流式检查不能中断已经进入的网络读取，该阶段仍依赖超时；普通文件前置检查不是抵御并发文件替换的 OS 沙箱。
