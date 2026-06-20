# mytry 改造成 Nanobot + Coding Assistant 的计划

## 目标

把现有 `mytry` 从一个以 `cli.py` 为中心的教学型 agent harness，改造成一个统一 runtime：

- 可以像 Nanobot 一样进行日常对话、记忆、提醒和低打扰主动触达。
- 也可以像 Claude Code 助手一样阅读代码、执行 shell、编辑文件、拆任务、协调队友。
- 两种能力共享同一个底层骨架，不做两套 agent。

核心原则：

```text
一个 Runtime，多种 Mode/Profile。

MessageBus -> AgentLoop -> TurnPipeline -> ModeProfile -> ToolRegistry -> Provider
```

## 现状判断

当前项目已经具备很多有价值的 harness 能力：

- `cli.py`：入口、agent loop、LLM 调用、tool loop、compact、inbox/background 注入。
- `agent.py`：工具 schema。
- `tools.py`：工具 handler，包括文件、bash、task、background、team 通信。
- `message_bus.py`：队友之间的 JSONL inbox 总线。
- `teammate.py` / `subagent.py`：队友和子 agent 能力。
- `task.py`：持久化任务系统。
- `background_task.py`：后台任务。
- `compact.py`：上下文压缩。
- `skills.py`：按需加载技能。

主要问题不是功能不够，而是职责还没有分层：

- `cli.py` 同时负责入口、对话历史、agent loop、LLM 调用、工具执行和输出。
- `message_bus.py` 是 team inbox bus，不是用户消息进出的 runtime bus。
- 工具 schema 和 handler 分散在 `agent.py`、`tools.py`、`cli.py` 之间，靠名称隐式对齐。
- Nanobot 和 Coding Assistant 的差异还没有抽象成 mode/profile。

## 目标结构

建议逐步演进到下面的结构：

```text
mytry/
  main.py                 # 最终入口，只做命令分流和启动
  bootstrap.py            # build_app_runtime / build_core_runtime
  runtime.py              # AppRuntime/CoreRuntime，管理 start/shutdown

  bus.py                  # Runtime MessageBus: 用户/channel <-> agent
  team_bus.py             # 原 message_bus.py：队友 inbox 通信

  agent_loop.py           # 消费 inbound，调用 pipeline，发布 outbound
  pipeline.py             # PassiveTurnPipeline，一轮被动对话主链
  session.py              # Session / SessionManager
  provider.py             # LLMProvider wrapper

  modes/
    base.py               # ModeProfile / ModePolicy
    router.py             # 根据用户输入和 session 状态选择 mode
    nanobot.py            # Nanobot profile
    coding.py             # Coding Assistant profile
    hybrid.py             # 默认自动模式

  tools/
    registry.py           # ToolRegistry: schema + handler + risk + mode visibility
    filesystem.py
    shell.py
    task_tools.py
    team_tools.py
    background_tools.py
    skill_tools.py

  memory/
    store.py              # 第一版可先 JSON/Markdown
    retriever.py          # 后续再做 embedding/semantic retrieval

  proactive/
    loop.py               # Nanobot 主动循环，后期再做
    policy.py             # cooldown / quiet hours / busy gate
```

第一阶段不用一次性创建所有目录。先拆出最小骨架即可。

## 模式设计

不建议写两套 agent，例如 `nanobot_agent.py` 和 `coding_agent.py`。更好的设计是一个 runtime，多个 `ModeProfile`。

```python
class ModeProfile:
    name: str
    system_prompt: str
    enabled_tools: set[str]
    memory_policy: str
    proactive_enabled: bool
    permission_policy: str
    response_style: str
```

### Nanobot Mode

用途：

- 日常陪伴。
- 记住用户偏好、状态、项目和长期上下文。
- 提醒、总结、低打扰主动触达。

默认工具：

- `recall_memory`
- `memorize`
- `task_list` / `task_create`
- `send_message` 或未来的 `message_push`
- 可选 `web_search`

默认关闭：

- `bash`
- `write_file`
- `edit_file`
- 高风险系统操作

### Coding Mode

用途：

- 像 Claude Code 一样辅助编程。
- 读代码、查文件、跑测试、改代码、做 code review。
- 使用 task/subagent/teammate 做复杂任务拆分。

默认工具：

- `bash`
- `read_file`
- `write_file`
- `edit_file`
- `load_skill`
- `task_create` / `task_update` / `task_list`
- `background_run`
- `spawn_teammate`
- `send_message` / `read_inbox`

策略：

- 工作前先观察代码。
- 修改前尽量小范围定位。
- 修改后跑测试或给出未验证原因。
- destructive shell 命令需要拦截或确认。

### Hybrid Mode

默认模式。

用途：

- 日常聊天和编程请求都能接。
- 根据用户意图临时切换工具可见性。
- 用户显式指定时写入 session mode。

切换规则：

```text
用户说“进入编程模式 / 帮我改 bug / 看这个 repo”
  -> coding

用户说“回到聊天 / 陪我聊聊 / 先别写代码”
  -> nanobot

用户说“自动模式”
  -> hybrid
```

## 分阶段改造

### Phase 0：冻结当前行为

目标：先确保现有项目能作为回归基线。

建议动作：

- 手动跑一轮 `python -m mytry.cli` 或当前启动方式。
- 记录能工作的关键命令：
  - 普通聊天。
  - `read_file`。
  - `bash`。
  - `task_create/task_list`。
  - `spawn_teammate`。
  - background task。
- 暂时不要清理 pycache、transcripts、tasks 等历史文件。

完成标准：

- 有一份“现在能跑什么”的清单。

### Phase 1：拆出 Session

目标：把 `cli.py` 里的 `history = []` 变成 `SessionManager`。

新增：

```text
session.py
```

接口：

```python
session = sessions.get_or_create("cli:local")
session.add_message("user", query)
session.add_message("assistant", reply)
sessions.save(session)
```

第一版可以只用内存，第二版再落盘到 `.sessions/cli_local.json`。

完成标准：

- `cli.py` 不直接维护裸 `history`。
- 现有 agent loop 行为不变。

### Phase 2：拆出 PassiveTurnPipeline

目标：把 `cli.py` 里的 `agent_loop(messages)` 搬出去。

新增：

```text
pipeline.py
```

第一版结构：

```python
class PassiveTurnPipeline:
    def run(session, inbound_message) -> str:
        inject_inbox()
        inject_background_results()
        maybe_compact()
        reason_until_no_tool_calls()
        return final_assistant_text
```

注意：

- 此阶段不要重写 tool loop，只搬家。
- `execute_tool_call()` 可以先一起搬到 pipeline，后面再进入 ToolRegistry。

完成标准：

- `cli.py` 只负责读用户输入、调用 pipeline、打印回复。
- compact/inbox/background 行为保持一致。

### Phase 3：新增 Runtime MessageBus

目标：区分用户消息总线和队友 inbox 总线。

新增：

```text
bus.py
```

包含：

```python
InboundMessage(channel, chat_id, sender, content)
OutboundMessage(channel, chat_id, content)
MessageBus.publish_inbound()
MessageBus.consume_inbound()
MessageBus.publish_outbound()
MessageBus.dispatch_outbound()
```

现有 `message_bus.py` 建议后续改名为 `team_bus.py`，但不要急着改 import，避免一次性破坏太多。

完成标准：

- CLI 输入会包装成 `InboundMessage`。
- agent 回复会包装成 `OutboundMessage`。
- CLI 输出通过 outbound subscriber 打印。

### Phase 4：拆出 AgentLoop

目标：让 agent 主循环从 CLI 中独立。

新增：

```text
agent_loop.py
```

职责：

```text
consume inbound
get session
pipeline.run()
publish outbound
```

完成标准：

- `cli.py` 不再直接调用 `pipeline.run()`，而是把用户消息送进 bus。
- `AgentLoop` 可以未来被 Telegram、HTTP、IPC 复用。

### Phase 5：新增 bootstrap/runtime

目标：集中装配，减少全局变量横向引用。

新增：

```text
bootstrap.py
runtime.py
```

第一版：

```python
def build_runtime():
    bus = MessageBus()
    sessions = SessionManager()
    provider = LLMProvider(...)
    tools = current_handlers_or_registry()
    pipeline = PassiveTurnPipeline(...)
    loop = AgentLoop(bus, sessions, pipeline)
    return AppRuntime(...)
```

完成标准：

- `cli.py` 变成启动壳。
- 模型 client、handlers、TEAM、BUS 等依赖集中在 bootstrap 装配。

### Phase 6：ToolRegistry

目标：把 schema 和 handler 绑定到一个地方。

新增：

```text
tool_registry.py
```

第一版可以直接包装现有 `agent.py` 和 `tools.py`：

```python
registry.register_schema(schema, handler)
registry.schemas_for_mode(mode)
registry.execute(name, args)
```

工具元数据建议包含：

```text
name
schema
handler
risk
enabled_modes
source
```

完成标准：

- pipeline 不再直接依赖 `LEAD_TOOLS` 和 `handlers` 两个分离对象。
- Nanobot/Coding 可以按 mode 过滤工具。

### Phase 7：ModeProfile + ModeRouter

目标：一个 runtime 支持 Nanobot/Coding/Hybrid。

新增：

```text
modes/base.py
modes/nanobot.py
modes/coding.py
modes/hybrid.py
modes/router.py
```

实现：

- session 保存 `current_mode`。
- 显式命令切换模式。
- router 根据用户输入决定本轮 mode。
- mode 决定 system prompt 和工具可见性。

完成标准：

- “进入编程模式”后开放代码工具。
- “回到聊天模式”后隐藏 shell/edit。
- hybrid 默认可自动识别明显编程请求。

### Phase 8：Memory

目标：Nanobot 有长期连续性，Coding 也能记住项目偏好。

第一版：

```text
memory/MEMORY.md
memory/SELF.md
memory/NOW.md
```

工具：

- `memorize`
- `recall_memory`

第二版再考虑：

- SQLite。
- embedding。
- semantic retrieval。
- dedup/supersede。

完成标准：

- Nanobot mode 能读写简单长期记忆。
- Coding mode 能记住用户对代码风格、测试偏好、项目约定的要求。

### Phase 9：Proactive Nanobot

目标：只给 Nanobot/Hybrid 加主动触达，不让 Coding Assistant 随便打断用户。

新增：

```text
proactive/loop.py
proactive/policy.py
```

最小策略：

- quiet hours。
- cooldown。
- session busy gate。
- 用户显式开启后才启用。

主动内容第一版只做：

- 定时提醒。
- 后台任务完成通知。
- 每日/阶段性总结。

完成标准：

- proactive 默认不骚扰。
- coding mode 下只通知后台任务、队友消息、用户订阅事件。

## 推荐优先级

最推荐的前三刀：

1. `SessionManager`
2. `PassiveTurnPipeline`
3. `Runtime MessageBus + AgentLoop`

这三刀做完，项目骨架就立起来了。

暂时不要优先做：

- 完整插件系统。
- 复杂向量记忆。
- dashboard。
- 多平台 channel。
- 自动 proactive。

这些都应该等骨架稳定后再加。

## 验收路线

每个阶段都要保持一个最小可运行闭环：

```text
python -m mytry.cli
> 你好
能收到回复
```

Coding 验收：

```text
> 进入编程模式
> 看一下当前目录有哪些文件
模型能调用 bash/read_file
```

Nanobot 验收：

```text
> 回到聊天模式
> 记住我喜欢简洁一点的回答
> 你还记得我的偏好吗
模型能通过 memory 回复
```

Hybrid 验收：

```text
> 帮我看看这个项目结构
自动进入 coding turn

> 今天有点累
自动进入 nanobot turn
```

## 风险和取舍

### 不要一次性重命名太多文件

`message_bus.py` 目前被多个模块引用。即使概念上它应该叫 `team_bus.py`，也建议等 runtime bus 跑通后再改名。

### 不要先做插件系统

插件系统很诱人，但现在最重要的是拆主链。先有 pipeline 和 mode，再考虑 plugin。

### 不要让 Nanobot 暴露高风险工具

聊天模式默认不应该看到 `bash/write_file/edit_file`。这些应该只在 coding mode 或明确授权下出现。

### 不要让主动循环污染编程助手

Coding Assistant 的主动性应该很弱，只用于后台任务完成、测试结束、队友消息这类明确事件。

## 最终心法

`mytry` 不需要变成 `akashic-agent` 的复制品。它应该保留自己的方向：

```text
Claude Code 风格 coding harness
+ Nanobot 风格长期陪伴和主动性
+ team/subagent/task 的实验能力
```

你要学的是骨架：

```text
入口只启动
bootstrap 只装配
AgentLoop 只调度
Pipeline 只处理一轮 turn
ModeProfile 决定人格、工具和权限
ToolRegistry 管工具
Session/Memory 管连续性
```

这套骨架立住后，Nanobot 和 Coding Assistant 就不是两套系统，而是同一个 agent 在不同场景下换了驾驶模式。
