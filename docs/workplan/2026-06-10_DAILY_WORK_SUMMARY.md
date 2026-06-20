# 2026-06-10 工作总结

日期：2026-06-10

## 总体目标

今天的工作主要围绕一个方向展开：把项目从“能跑的 agent demo”继续收束成一个更清晰的 agent runtime 平台。

重点不只是加功能，而是把几个关键边界立起来：

- runtime 目录和职责边界。
- coding task 与普通聊天的隔离边界。
- 真实 workspace 的安全边界。
- 工具调用的治理边界。
- run / eval 的证据边界。
- 插件和 core 的职责边界。
- benchmark 对 runtime 行为的验证边界。

## 一、Runtime 结构继续收束

今天继续围绕 `runtime/`、`models/`、`agents/coding/` 这套目标结构做清理。

主要结果：

- 主链路集中到 `runtime/`：
  - `runtime/agent_loop.py`
  - `runtime/pipeline.py`
  - `runtime/reasoning_loop.py`
  - `runtime/bootstrap.py`
  - `runtime/app_runtime.py`
- 模型层集中到 `models/`：
  - `models/model_pool.py`
  - `models/provider.py`
  - `models/model_task_runner.py`
- coding task 能力集中到 `agents/coding/`：
  - `agents/coding/runner.py`
  - `agents/coding/session.py`
  - `agents/coding/artifacts.py`
  - `agents/coding/memory_lifecycle.py`
  - `agents/coding/promotion.py`

同时移除了不再需要的 scheduler agent 相关代码，让当前系统重点回到 runtime platform + coding agent。

## 二、简化 RuntimeKernel / 重复装配

今天讨论并清理了 `RuntimeKernel` 一类重复持有对象的问题。

原先 `AppRuntime`、`RuntimeKernel`、`AgentLoop` 会重复保存很多运行时组件，比如 pipeline、router、trace store、task runner 等。现在主运行对象更轻：

- `AppRuntime` 主要负责 runtime 生命周期。
- `AgentLoop` 负责 inbound turn 的主执行编排。
- `Pipeline` 负责 turn lifecycle、context、memory lifecycle。
- `ReasoningLoop` 负责模型调用和工具调用闭环。

这个方向让 runtime 的对象图更直观，也减少了“一个组件到底应该从哪里拿”的困惑。

## 三、模型路由和 Provider 兼容

今天梳理了当前模型路由机制，并处理了 DeepSeek 兼容问题。

当前模型路由基于 `ModelPool`：

- `chat`
- `coding`
- `summary`
- `hybrid`
- `reflection`
- `task_conclusion`

这些 purpose 可以通过环境变量配置不同 provider 和 fallback。

同时修复/验证了 provider 不应该向某些 OpenAI-compatible 后端发送空 assistant message 的问题，避免出现：

```text
Invalid assistant message: content or tool_calls must be set
```

相关测试覆盖在 `tests/test_model_pool_routing.py` 中。

## 四、Trace 与 Observability 加强

今天重点增强了 run 级 trace。

现在单次 run 会记录：

- run started / finished / failed
- route selected
- reasoning step started / completed
- context build started / completed
- context sanitized
- model call started / completed / failed
- model route attempts
- tool call started / completed / failed
- workspace resolved
- workspace snapshot captured
- workspace diff written

每个 run 默认会写入：

```text
.runs/<run_id>/
```

主要产物包括：

- `run_state.json`
- `trace.jsonl`
- `metrics.json`
- `report.json`
- `report.md`

`metrics.json` 会从 trace 中聚合：

- 模型调用次数和失败次数。
- 工具调用次数、失败次数、拒绝次数。
- 模型耗时、工具耗时。
- token usage。
- provider fallback attempts。
- sanitized message 数量。
- run duration。

这让失败定位从“看最终回复”变成“看结构化执行链”。

## 五、真实 Workspace 接入

今天完成了 coding task 绑定真实 workspace 的第一版。

新增核心模块：

- `runtime/workspace.py`

核心能力：

- `WorkspaceResolver`
- allowed roots 校验
- default workspace fallback
- session metadata 绑定 `workspace_root`
- `safe_workspace_path()` 防止路径逃逸

coding task 执行时会：

1. 从 inbound metadata 或默认配置解析 workspace。
2. 校验 workspace 是否在 allowed roots 内。
3. 把 workspace metadata 写入父 session 和 task session。
4. 文件工具从 session metadata 获取 workspace root。
5. 执行前后拍 workspace snapshot。
6. 生成 workspace diff。

这让 agent 可以接入一个真实项目目录进行修改，同时保留路径边界和变更记录。

## 六、文件工具和 Git 工具补齐

今天补齐了 coding 模式需要的基础 workspace 工具。

文件侧：

- `list_files`
- `read_file`
- `write_file`
- `edit_file`

Git 侧：

- `git_status`
- `git_diff`
- `git_log`
- `git_branch`
- `git_add`
- `git_commit`

这些工具都是 session-scoped，会基于当前 session 的 workspace root 工作。

其中写文件、编辑文件、bash、git add、git commit 等高风险工具继续受 tool policy / hook 约束。

## 七、Benchmark 框架第一版落地

今天实现了 coding benchmark 第一版。

新增目录和模块：

- `evaluation/`
  - `task_schema.py`
  - `harness.py`
  - `metrics.py`
  - `verifiers.py`
- `benchmarks/coding_tasks.json`
- `benchmarks/fixtures/`
- `scripts/run_evals.py`
- `tests/test_coding_benchmark.py`

benchmark 支持：

- fixture repo
- allowed tools
- step budget
- scripted provider baseline
- real model runner
- verifier
- workspace diff check
- trace completeness check
- failure category
- failure reason
- progress output
- single task run
- keep workspace

常用命令：

```bash
python scripts/run_evals.py --suite coding
```

单条任务：

```bash
python scripts/run_evals.py --suite coding --task-id coding-git-diff-008 --keep-workspace
```

真实模型：

```bash
python scripts/run_evals.py --suite coding --runner real
```

这套 benchmark 的定位是：先证明 runtime、工具、workspace、trace、verifier 这条链路稳定，再去评估真实模型能力。

## 八、Report 插件化

今天把人类可读报告从 runtime core 中拆到了插件层。

现在边界是：

- `plugins/run_report` 负责单个 run 的 `report.md`。
- `plugins/eval_report` 负责 benchmark 的 `summary.md`。

同时扩展了插件生命周期：

- `after_run`
- `after_eval`

这样 runtime core 只负责结构化事实：

- `run_state.json`
- `trace.jsonl`
- `metrics.json`
- `report.json`
- `summary.json`
- `rows.json`

人类可读展示交给插件生成。

这个边界比把 markdown 生成逻辑写在 core 里更干净。

## 九、系统设计文档沉淀

今天参考 Pico 文档的组织方式，为当前项目生成了一套真实系统设计文档。

新增目录：

```text
docs/system-design/
```

包含：

- `00-全局总览.md`
- `01-消息入口、会话与身份边界.md`
- `02-运行时主循环与执行编排.md`
- `03-工具接入、可见性与执行护栏.md`
- `04-模型路由与Provider池.md`
- `05-上下文构建、压缩与记忆生命周期.md`
- `06-CodingTaskSession与真实Workspace.md`
- `07-Trace、Run工件与报告插件.md`
- `08-CodingBenchmark与评测方法.md`

这套文档没有使用图片，也没有照搬 Pico 中当前项目不存在的 checkpoint/resume 等能力。没有实现的部分只在“当前边界”里说明。

同时更新了 `docs/README.md`，加入 system design 入口。

## 十、今天验证过的内容

今天运行过的主要验证：

```bash
python -m unittest discover -s tests -p 'test_coding_benchmark.py' -v
```

结果通过。

```bash
python -m unittest discover -s tests -p 'test_run_trace.py' -v
```

结果通过。

全量测试：

```bash
python -m unittest discover -s tests -v
```

结果：

```text
Ran 154 tests
OK
```

也手动跑过 benchmark 单任务验证：

```bash
python scripts/run_evals.py --suite coding --task-id coding-git-diff-008 --keep-workspace
```

验证了 eval report 插件能够生成新的 `summary.md`。

## 今天形成的核心收益

今天最大的收益不是某一个单点功能，而是几条链路被打通了：

### 1. Coding task 链路

```text
用户请求 -> coding route -> task session -> real workspace -> file/git tools -> workspace diff -> task artifacts
```

### 2. Trace 证据链路

```text
run state -> trace events -> metrics -> report.json -> run_report plugin -> report.md
```

### 3. Benchmark 证据链路

```text
benchmark task -> fixture workspace -> scripted/real runner -> verifier -> rows.json -> eval_report plugin -> summary.md
```

### 4. 插件扩展链路

```text
plugin tools/hooks -> before_turn/after_turn -> after_run -> after_eval
```

这些链路让系统开始从“能执行”变成“能解释、能复盘、能评测”。

## 当前仍然明显欠缺的地方

今天也明确了下一阶段短板：

- 上下文治理还没有 section budget。
- coding workspace 还没有分支隔离、rollback、patch preview。
- trace 还没有 OpenTelemetry 导出。
- trace viewer 还可以继续增强筛选和失败定位。
- 插件协议还需要更稳定的 manifest/config/version 边界。
- benchmark 还缺多模型对照、多次重复运行、ablation 和趋势对比。
- 安全边界还偏个人本地使用，没有完整审批流和多租户 ACL。

## 下一步建议

推荐下一轮优先做三件事：

1. **上下文预算治理**

   把 system/profile、memory、task context、recent history、tool results、current request 拆成明确 section，并记录每轮裁剪 metadata。

2. **Workspace 变更治理**

   coding task 自动创建临时分支，支持 patch preview，并在失败时至少提供 restore 指南或 rollback artifact。

3. **Benchmark 实验层**

   在当前 `rows.json` 基础上增加多模型对照、重复运行、ablation 和历史 eval 对比。

## 总结

今天的工作把项目往“工业级 agent runtime”方向推了一大步。

现在系统已经不只是能调用模型和工具，而是有了更清晰的运行边界：

- 消息和 session 边界。
- 用户身份和权限边界。
- 工具可见性和执行边界。
- coding task 与普通聊天边界。
- workspace 安全边界。
- run/eval 证据边界。
- core 与 plugin 展示边界。

下一阶段的重点应该是把这些边界继续契约化、版本化，并用 benchmark 持续证明每次改动真的让系统更稳。

