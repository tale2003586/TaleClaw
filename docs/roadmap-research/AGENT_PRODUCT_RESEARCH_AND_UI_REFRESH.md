# taleclaw UI 改进与同类 Agent 调研报告

> 调研日期：2026-05-31  
> 调研范围：本地代码、GitHub 官方仓库 README 与官方架构文档。  
> 说明：本文比较的是产品设计和工程取舍，不是模型效果 benchmark。不同项目的目标不同，不应只用 star 数或功能数量判断优劣。

## 一、先说结论

taleclaw 已经不是一个只能聊天的 Demo。它有明确的 Agent Loop、模式路由、工具注册表、TaskSession、记忆生命周期、定时任务审查、受限自动执行、Web 控制台和插件机制。对于一个个人助手 MVP，这个骨架是清楚而且有继续生长空间的。

但目前最需要补的不是更多工具，而是三件更基础的事：

1. **真实执行隔离**：当前 `bash` 安全策略仍以字符串黑名单为主。它能挡住少量误操作，但不能构成可靠沙箱。
2. **可观察、可审核的自动任务控制台**：后台 Agent 已经能执行，但用户仍缺少统一的任务运行记录、审批队列、工具时间线和产物查看入口。
3. **真正可检索的长期记忆**：项目已经会归档旧上下文，但 `recall(query)` 仍然忽略查询并返回全文。长期记忆目前更像“存下来了”，还不是“用起来了”。

最适合 taleclaw 的方向不是立刻追求 OpenClaw 的渠道数量，也不是照搬大型平台，而是成为一个：

> **轻量、可审计、适合个人研究和 coding 工作流的长期运行助手。**

这个定位有辨识度，也符合现有代码的规模。

## 二、本轮 UI 改进

本轮前端调整保持原生 HTML、CSS、JavaScript，不引入新的前端框架或 CDN 依赖。

### 2.1 已完成的视觉调整

- 将侧栏品牌区整理成更清晰的工作台入口，强化 `taleclaw` 的第一视觉信号。
- 收紧侧栏、标签页、会话列表的层级和间距，使其更适合持续使用，而不是像临时管理页。
- 将模式切换改成更明确的分段控件，并保留当前会话内切换模式的逻辑。
- 为消息区域增加更舒展的阅读宽度、气泡层级和空白会话起始任务。
- 将工具请求和工具结果改为默认折叠，用户需要时再展开查看完整过程。
- 调整底部输入区：输入框仍固定在底部，并与可滚动的消息区分离。
- 保留手机端抽屉式侧栏：默认隐藏，通过左上角按钮打开。

### 2.2 Markdown 展示

此前助手回复中的标题、列表、代码块和粗体会以原始 Markdown 字符展示，阅读体验比较粗糙。

现在后端使用项目已有的 `mistune` 生成展示 HTML，并额外做了限制：

- 原始消息文本仍存进 SQLite，模型上下文和记忆内容不变。
- Web API 只为助手消息附加 `display_html` 展示字段。
- 原始 HTML 会被转义。
- 链接只允许 `http`、`https`、`mailto`。
- 远程图片不会加载，只显示文本占位符。

这比在浏览器端直接信任 Markdown HTML 更稳妥，也避免引入第三方 CDN。

## 三、taleclaw 当前架构快照

| 层次 | 当前实现 | 评价 |
| --- | --- | --- |
| 入口 | CLI、Web 控制台 | 足够支撑个人使用，但还不是多渠道 Gateway |
| 消息总线 | `bus/user_bus.py`、`bus/team_bus.py` | 已有解耦意识，可以继续扩展渠道适配器 |
| Agent Loop | `core/agent_loop.py`、`core/pipeline.py` | 路径清楚，适合继续加审批、事件和取消机制 |
| 模式路由 | Chat、Coding、Hybrid，Hybrid 支持 LLM 二次判断 | 比纯正则切换更稳，方向正确 |
| 工具系统 | 注册表、延迟暴露、风险元数据、hook、`tool_search` | 是项目里做得较好的部分，但执行边界仍需加强 |
| TaskSession | 独立任务记忆、日志、结论提升回主会话 | 已形成清楚的任务作用域 |
| 记忆 | Markdown、JSON、SQLite 归档、History 摘要、Recent 淘汰 | 生命周期已成形，但检索尚未真正接通 |
| 定时任务 | APScheduler、planner、auditor、审批范围、预算和 trace | 已经超过普通 MVP，需要补控制台才能发挥价值 |
| 插件 | 手工注册的插件模块 | 简单直观，但缺少统一 manifest、权限声明和发现机制 |
| 部署 | Docker Compose、Nginx、Basic Auth | 能部署；离多用户、密钥隔离和公网长期运行仍有距离 |

当前工作区约有 **11.6k 行 Python 代码**，其中包括测试。这个规模仍适合保持架构简单，不必过早平台化。

## 四、GitHub 同类项目概览

本次选取六个项目作为核心参照，另补充一个垂直 Agent 作为设计参考。

| 项目 | 定位 | 值得关注的 Agent 设计 |
| --- | --- | --- |
| [OpenClaw](https://github.com/openclaw/openclaw) | 本地运行的个人 AI 助手 | Gateway 控制面、多渠道、按 workspace 或发送者路由 Agent、可选沙箱、配套 onboarding |
| [nanobot](https://github.com/HKUDS/nanobot) | 超轻量个人助手 | 约 4k 行核心 Agent、cron、heartbeat、subagent、MCP、多渠道，结构容易阅读 |
| [NanoClaw](https://github.com/qwibitai/nanoclaw) | 安全优先的轻量 Claw 实现 | 每组独立容器、显式目录挂载、凭证代理、SQLite 状态、文件队列 IPC、单写者设计 |
| [Agent Zero](https://github.com/agent0ai/agent-zero) | 通用个人 Agent 框架 | 动态下属 Agent、项目隔离、知识与记忆、MCP、扩展中心、实时 Web UI |
| [DeerFlow](https://github.com/bytedance/deer-flow) | 长任务 SuperAgent harness | skills 渐进披露、subagent 作用域上下文、长上下文压缩与恢复、任务沙箱、Gateway |
| [OpenHands SDK](https://github.com/OpenHands/software-agent-sdk) | 软件工程 Agent SDK | 可组合 Agent、委派、远程执行、风险确认策略、事件观测、token 和成本指标 |
| [browser-use](https://github.com/browser-use/browser-use) | 浏览器自动化 Agent | 面向单一复杂领域提供类型化动作面，说明垂直工具应尽量结构化 |

## 五、逐项分析：优点与适合借鉴的部分

### 5.1 OpenClaw：先把 Gateway 做成控制面

OpenClaw 的亮点不只是支持大量聊天渠道。更关键的是，它把 Gateway 放在中心位置：渠道接入、会话、Agent 路由、工作区和沙箱策略都围绕控制面组织。

**优点**

- 多渠道不是散落的 webhook，而是统一进入 Gateway。
- 可以按 workspace、发送者或会话把消息路由到隔离的 Agent。
- 对非主会话提供可选 Docker 沙箱。
- 有 onboarding wizard、安全说明和诊断入口，降低运维成本。

**taleclaw 应借鉴**

- 将未来的 Web、Telegram、飞书等入口接到统一 Channel Adapter 接口。
- 在入口层做发送者 ACL、会话绑定和审计字段，而不是在 Agent 内临时判断。
- 先做一个小型 Gateway，不急着一次接入十几个渠道。

### 5.2 nanobot：用小代码量保留完整 Agent 骨架

nanobot 的价值在于克制。它的核心 Agent 很小，但并不只是一个 `while tool_calls` 循环：仓库结构中仍然有 cron、heartbeat、memory、skills、subagent、providers、session 和 channels。

**优点**

- 结构容易读，也容易修改。
- 功能边界清晰，适合作为个人助手二次开发。
- MCP 和 provider 抽象降低了工具、模型接入成本。
- README 明确提醒：部署后的 `exec` 工具有代码执行风险，应限制允许的用户并使用沙箱。

**taleclaw 应借鉴**

- 继续保持代码路径可读，不要为了“架构感”引入复杂服务拆分。
- 给插件补统一描述文件和权限声明。
- 把安全提醒落实为默认配置，而不只是 README 中的注意事项。

### 5.3 NanoClaw：把隔离边界当成第一原则

NanoClaw 与 taleclaw 最值得比较的不是功能，而是安全模型。它默认让 Agent 在隔离 Linux 容器中运行，只能看到显式挂载目录，并通过 credential proxy 避免把真实凭证交给任务容器。

**优点**

- 每个群组拥有独立容器、记忆和文件工作区。
- 文件系统默认不可见，只暴露显式挂载目录。
- 凭证代理减少密钥泄露风险。
- SQLite 保存消息、会话、任务和 Agent run；IPC 使用文件队列和单写者设计，容易理解和追踪。
- 安全文档明确把外部输入视为不可信内容。

**taleclaw 应借鉴**

- 为 coding task 和 scheduled task 引入任务级工作目录。
- 中期加入任务级容器，至少让高风险自动任务进入隔离执行器。
- 把 API key 与任务执行环境分离，不要让工具命令天然读到全部环境变量。

### 5.4 Agent Zero：项目空间和下属 Agent 是用户可见能力

Agent Zero 强调“会成长的个人 Agent”。它允许主 Agent 拆分任务给下属 Agent，提供项目隔离、知识和记忆能力、MCP，以及实时可编辑的 Web UI。

**优点**

- 多 Agent 协作不是隐藏实现细节，而是工作流能力。
- 项目有独立上下文，适合长期 coding 和研究任务。
- Web 界面展示实时执行过程。
- 扩展中心和 MCP 让能力添加更直接。

**taleclaw 应借鉴**

- TaskSession 已经是正确起点，可以进一步升级为可见的“任务空间”。
- 用户需要在 Web 中看到任务执行到哪一步、调用了什么工具、产出了什么文件。
- 下属 Agent 只在任务确实可以分解时使用，不需要为了展示复杂度默认开启。

### 5.5 DeerFlow：长任务的关键是上下文工程

DeerFlow 面向研究、代码、网页、幻灯片和内容生成等长任务。它强调 skills 的渐进披露、subagent 的作用域上下文，以及长上下文摘要和恢复。

**优点**

- 子 Agent 接收范围清晰的上下文，结束时返回结构化结果。
- 长任务会压缩和恢复上下文，不要求主对话始终携带全部中间过程。
- 每个任务有隔离沙箱和文件系统。
- skills、memory、MCP、Gateway 彼此是清楚的层次。

**taleclaw 应借鉴**

- TaskSession 日志和结论提升机制方向正确。
- 下一步应给任务定义结构化状态：目标、预算、阶段、产物、结论、失败原因。
- 将“压缩后的任务结论”作为回主会话的主要内容，而不是把过程噪音重新塞回 Pending。

### 5.6 OpenHands SDK：执行型 Agent 需要可观察性和策略

OpenHands SDK 更偏软件工程 Agent。它把 bash、文件、Web、MCP、远程执行、委派、风险确认和事件指标当成 SDK 级能力。

**优点**

- Agent、工具和委派可以组合。
- 支持 Docker、Kubernetes 等远程执行环境。
- 可配置确认策略，对高风险动作要求人工批准。
- 事件流中包含运行过程、token 和成本指标。

**taleclaw 应借鉴**

- 将工具 trace 从内存记录升级为持久化事件。
- 给自动任务增加取消、重试、暂停、批准、拒绝等明确状态。
- 为模型调用记录 token、耗时和错误，不必一开始做复杂计费系统。

## 六、taleclaw 已经做对的地方

### 6.1 Agent Loop 简单而清楚

`core/agent_loop.py` 和 `core/pipeline.py` 没有把路由、工具执行、记忆和消息发送揉成一个巨大函数。对于个人项目，这种清楚程度很宝贵。

### 6.2 工具暴露不是全量裸奔

`tools/tool_registry.py` 已经区分常驻工具、按模式预加载工具和延迟发现工具，也为定时 Agent 限制了批准能力。这个方向比“把所有函数都塞进模型上下文”更稳，也更省 token。

### 6.3 TaskSession 作用域和结论提升机制有价值

TaskSession 拥有独立记忆和详细日志，任务结束后只将可复用结论提升到主会话 Pending。这个设计能够减少任务过程噪音污染长期记忆。

### 6.4 自动任务不是简单 cron shell

定时任务已经具备 planner、auditor、批准范围、预算、超时和 trace。它比“定时给主会话伪造一条用户消息”更合适：自动任务可以作为独立 Agent run 执行，也更容易审计。

### 6.5 记忆生命周期已经开始分层

用户原文保留、助手回复摘要、Recent Context 滚动淘汰、旧内容进入 SQLite 归档，这套分层思路是合理的。欠缺的是检索层，而不是重新推倒存储层。

## 七、当前不足：直白版

### P0：`bash` 安全仍然只是薄弱护栏

`tools/handlers.py` 的 `run_bash()` 使用 `subprocess.run(..., shell=True)`，只检查少数字符串黑名单。`tools/hooks.py` 的 `ShellSafetyHook` 也是相似策略。

这会产生一种危险错觉：看起来“有安全检查”，但实际绕过方式很多。`FileWriteScopeHook` 只覆盖 `write_file` 和 `edit_file`，不能限制 Agent 通过 bash 写出工作区。Docker 部署能限制一部分宿主机风险，但当前任务仍共享应用容器、挂载目录和环境变量。

**应对**

- 给高风险工具增加人工批准。
- 将普通任务限定在任务工作目录。
- 将自动任务和 coding task 放进独立执行容器。
- 对密钥使用代理或最小化注入。
- 将命令、参数、批准人、结果和退出码持久化。

### P0：公网控制面仍然过于简单

当前 Web 部署依赖 Basic Auth 和 Nginx。个人自用时可以工作，但如果继续加入网盘、自动任务和 shell 能力，它就不再只是聊天页。

**应对**

- 保留仅绑定 `127.0.0.1` 的默认部署。
- 公网访问优先加 HTTPS、强密码、登录速率限制。
- 新渠道接入时加入 sender ACL、配对码或白名单。
- 不要直接把容器端口和调试接口暴露到公网。

### P1：长期记忆“会存”，但还不会“找”

`memory/store.py` 的 `recall(query)` 当前忽略 `query`，直接返回全文。归档 SQLite 已经存在，但语义召回、时间过滤、来源过滤和结果去重还没有进入真实请求链路。

**应对**

1. 先定义统一记忆条目结构：`id`、`kind`、`content`、`summary`、`source`、`created_at`、`session_id`、`tags`。
2. 加关键词与时间过滤，先建立可验证的检索接口。
3. 再加 embedding 和向量索引，做混合召回。
4. 由轻量 router 判断是否需要检索，并把召回结果限制在固定 token 预算内。

不要先接一个向量数据库，再思考召回结果如何进入 Agent。

### P1：后台 Agent 缺少用户可见的运行控制台

自动任务已经有审查和 trace，但 Web 页面主要仍是聊天、文件和文本分析。用户无法直观看到：任务何时运行、调用了哪些工具、是否等待批准、消耗多少时间、最终产生哪些文件。

**应对**

- 增加“自动任务”和“运行记录”页。
- 展示运行状态、触发时间、工具调用时间线、审批状态和产物链接。
- 支持取消、暂停、批准、拒绝、重新执行。

### P1：任务有记忆作用域，但执行作用域还不够完整

TaskSession 解决了上下文污染问题，但还没有像 NanoClaw 或 DeerFlow 那样提供明确的 OS 级任务隔离。不同任务仍可能读写共享工作区。

**应对**

- 每个任务生成独立目录。
- 工具注册表携带 task workspace。
- 高风险任务进入临时容器。
- 任务结束后只提升明确产物和结论。

### P1：消息总线已有雏形，但还不是 Gateway

`bus/` 为渠道扩展提供了基础，但当前真实入口仍主要是 Web 和 CLI。还缺少渠道身份、ACL、重试策略、速率限制、会话绑定和审计记录。

**应对**

- 先实现一个统一 `ChannelAdapter` 协议。
- 第一批只接一个高价值渠道，例如 Telegram 或飞书。
- 将来源身份、原始消息 ID 和会话映射写入统一事件结构。

### P1：模型配置过于单一

当前模型配置偏硬编码。不同任务的需求其实不同：聊天需要快，研究需要更强推理，摘要需要便宜稳定，代码任务可能需要更长上下文。

**应对**

- 增加 provider profile：`fast`、`reasoning`、`summary`、`coding`。
- 为每类任务配置默认 profile。
- 记录调用模型、耗时、token 和异常。

### P2：插件机制缺少统一契约

目前插件以手工注册为主，简单但不利于长期扩展。尤其在加入 shell、搜索、PDF、定时器后，插件权限开始变得重要。

**应对**

- 为插件增加 manifest：名称、版本、工具、风险级别、环境变量、是否允许 scheduled agent 使用。
- 支持 MCP 作为外部工具接入方式。
- 暂时不要做插件市场，先把权限契约做清楚。

### P2：缺少 Agent 质量评估基线

单元测试能保证接口不坏，但无法判断 Agent 是否更会完成任务。

**应对**

- 建立十到二十个固定任务样例。
- 记录完成率、工具调用次数、耗时、token、人工批准次数和失败原因。
- 每次修改路由、提示词或工具策略后跑一次回归。

## 八、推荐路线图

| 阶段 | 目标 | 具体交付 | 为什么先做 |
| --- | --- | --- | --- |
| Phase 0 | 建立可靠执行边界 | 任务目录、bash 审批、持久化工具 trace、密钥最小化注入 | Agent 越能自动执行，越需要先控制风险 |
| Phase 1 | 做后台任务控制台 | 定时任务列表、run 详情、工具时间线、批准与拒绝、产物入口 | 现有 scheduler 能力已经值得被用户看见 |
| Phase 2 | 接通长期记忆检索 | 结构化条目、关键词过滤、混合召回、token 预算、召回日志 | 让已有归档真正产生价值 |
| Phase 3 | 加一个真实聊天渠道 | `ChannelAdapter`、身份映射、ACL、配对流程、审计事件 | 验证 Gateway 抽象，不追求渠道数量 |
| Phase 4 | 完善长任务 harness | 子任务状态、取消、恢复、预算、任务级容器、结构化结论 | 承接研究和 coding 的长期执行 |
| Phase 5 | 规范插件与模型配置 | 插件 manifest、MCP、provider profiles、基础评估集 | 为后续扩展减少隐性耦合 |

## 九、暂时不要做的事

- 不要立刻追求十几个聊天渠道。一个可靠渠道比十个半成品适配器更有价值。
- 不要先上复杂向量数据库。先把记忆条目、召回接口和评估方式定义清楚。
- 不要为了“多 Agent”而制造层层委派。只有可分解任务才值得交给子 Agent。
- 不要先做插件市场。权限 manifest、审计和隔离机制才是前置条件。
- 不要把 Basic Auth 当成最终安全方案。它是个人 MVP 的部署起点。

## 十、最终建议

taleclaw 最值得保留的是它的轻量和可读性。下一阶段应围绕一个核心问题推进：

> 当 Agent 可以在后台长期运行并调用真实工具时，用户能否知道它正在做什么、限制它能做什么，并在任务结束后留下可检索的有效记忆？

把这个问题答好，taleclaw 就会从“功能不少的个人助手”变成“可以放心持续使用的个人 Agent 工作台”。

## 十一、参考资料

- [OpenClaw GitHub 仓库](https://github.com/openclaw/openclaw)
- [OpenClaw Architecture](https://docs.openclaw.ai/concepts/architecture)
- [HKUDS/nanobot GitHub 仓库](https://github.com/HKUDS/nanobot)
- [qwibitai/NanoClaw GitHub 仓库](https://github.com/qwibitai/nanoclaw)
- [Agent Zero GitHub 仓库](https://github.com/agent0ai/agent-zero)
- [DeerFlow GitHub 仓库](https://github.com/bytedance/deer-flow)
- [OpenHands Software Agent SDK GitHub 仓库](https://github.com/OpenHands/software-agent-sdk)
- [browser-use GitHub 仓库](https://github.com/browser-use/browser-use)
