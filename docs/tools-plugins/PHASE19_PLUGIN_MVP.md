# Phase 19：Plugin MVP 说明

Phase 19 做的是一个极简插件系统。

我先读了 akashic-agent 里这些文件：

```text
agent/plugins/manager.py
agent/plugins/base.py
agent/plugins/context.py
agent/plugins/registry.py
agent/plugins/decorators.py
plugins/shell_safety/plugin.py
plugins/status_commands/plugin.py
```

akashic-agent 的完整插件系统比较强：

```text
动态发现 plugin.py
manifest/config
decorator 注册工具和生命周期 hook
EventBus 生命周期事件
tool hook 适配
插件 KV store
dashboard 扩展
```

但当前项目还不需要一次性照搬这些。Phase 19 的第一版目标是：

```text
手动注册插件
插件能注册工具
插件能注册 tool hook
插件能处理 before_turn / after_turn
主流程不用知道具体插件是谁
```

---

## 一、本次新增文件

```text
plugins/
plugins/__init__.py
plugins/base.py
plugins/plugin_manager.py
plugins/shell_safety/plugin.py
plugins/status_commands/plugin.py
```

---

## 二、插件基础接口

核心在：

```text
plugins/base.py
```

里面定义了：

```python
class Plugin:
    def tools(self) -> list[ToolRegistration]: ...
    def tool_hooks(self) -> list[ToolHook]: ...
    def before_turn(self, context: TurnContext) -> TurnResult | None: ...
    def after_turn(self, context: TurnContext, reply: str) -> None: ...
```

也就是说，一个插件第一版可以做四类事：

```text
注册工具
注册工具 hook
在 turn 开始前拦截
在 turn 结束后观察
```

这和 akashic-agent 的思路一致，只是我们没有做 decorator 和动态扫描。

---

## 三、PluginManager 做什么

核心在：

```text
plugins/plugin_manager.py
```

它负责：

```text
1. 初始化插件，注入 PluginContext
2. 收集插件注册的工具
3. 收集插件注册的 tool hook
4. 顺序执行 before_turn
5. 顺序执行 after_turn
```

插件上下文里有：

```text
workspace
tool_registry
sessions
memory_store
```

这让插件可以访问 runtime 能力，但不需要直接 import `bootstrap.py` 或 `pipeline.py`。

---

## 四、Shell Safety 变成插件

新增：

```text
plugins/shell_safety/plugin.py
```

它现在只是包装已有的：

```text
ShellSafetyHook
```

代码含义：

```text
ShellSafetyPlugin.tool_hooks()
  -> [ShellSafetyHook()]
```

然后在 `bootstrap.py` 里：

```python
plugin_manager = PluginManager(
    [
        ShellSafetyPlugin(),
        StatusCommandsPlugin(),
    ],
    ...
)

executor = ToolExecutor([
    FileWriteScopeHook(),
    ToolLoopGuardHook(),
    ToolTraceHook(),
    *plugin_manager.tool_hooks,
])
```

这样 shell safety 就从“硬编码在 bootstrap 里的 hook”变成了“插件提供的 hook”。

以后要禁用它，只要不注册 `ShellSafetyPlugin()`。

---

## 五、Status Commands 插件

新增：

```text
plugins/status_commands/plugin.py
```

它做两件事。

第一，注册一个工具：

```text
runtime_status
```

这个工具是 always_on，bot/coding 都能看到。

第二，注册 before_turn 行为：

```text
/status
/plugins
```

当用户输入这些命令时，插件会在进入模型前直接返回状态。

流程是：

```text
CLI 输入 /status
  -> MessageBus inbound
  -> AgentLoop.run_once
  -> plugin_manager.before_turn(...)
  -> StatusCommandsPlugin 命中命令
  -> 返回 TurnResult(abort=True, reply=...)
  -> AgentLoop 直接 publish outbound
  -> 不调用 LLM
```

这就是插件型命令最适合的位置：它不是模型推理任务，而是 runtime 控制命令。

---

## 六、AgentLoop 怎么接入插件

`AgentLoop` 现在多了一个可选参数：

```python
plugin_manager=None
```

每轮开始后，路由和模型调用前：

```python
plugin_result = self.plugin_manager.before_turn(inbound, session)
if plugin_result.abort:
    publish outbound
    return
```

模型正常回复后：

```python
self.plugin_manager.after_turn(inbound, session, reply)
```

主流程只认识 `plugin_manager`，不认识 `StatusCommandsPlugin` 或 `ShellSafetyPlugin`。

这就是插件系统的意义：新增能力时不继续污染主流程。

---

## 七、ToolRegistry 做的小改动

为了让插件注册工具更自然，`ToolRegistry.register()` 增加了：

```python
always_on: bool = False
```

插件工具如果设置：

```python
always_on=True
```

就会进入当前 turn 的可见工具集合。

另外加了：

```python
unregister(name)
```

第一版还没做插件卸载，但这个方法是后续卸载插件时需要的。

---

## 八、这版和 akashic-agent 的区别

akashic-agent 更完整：

```text
自动扫描插件目录
importlib 动态加载
manifest.yaml
plugin_config.json
decorator 元数据注册
EventBus 生命周期事件
插件 KV store
dashboard 扩展
```

当前项目第一版更小：

```text
手动注册插件实例
普通 Python 方法注册工具/hook
AgentLoop 显式调用 before_turn/after_turn
ToolExecutor 接收插件 hooks
```

这不是退步，而是更适合学习路径。

先理解插件系统的核心边界：

```text
插件声明能力
PluginManager 收集能力
Runtime 在固定扩展点调用能力
主流程不依赖具体插件
```

然后以后再加动态发现和 manifest。

---

## 九、你需要掌握什么

面向大模型应用开发实习，Phase 19 的重点是：

```text
Extension points
```

你要能讲清楚：

```text
1. 插件不是“一个工具”，而是一组可插拔能力
2. 插件可以注册工具，也可以注册 hook，也可以注册生命周期回调
3. PluginManager 是插件和 runtime 之间的边界层
4. 主流程应该只调用扩展点，不应该 import 具体插件
5. 工具安全、状态命令、记忆整理、dashboard 都适合插件化
```

可以这样介绍：

> 我实现了一个轻量 Plugin MVP：插件可以注册工具、注册 ToolExecutor hook，并挂载 before_turn/after_turn 生命周期回调。Shell safety 从硬编码 hook 改成插件提供，/status 命令也作为 before_turn 插件拦截，不进入 LLM 推理。这让 runtime 后续扩展能力时不需要继续修改 pipeline 主逻辑。
