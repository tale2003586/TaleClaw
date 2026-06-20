# 模型路由与 Provider 池

这篇文档讲当前系统怎么选择模型、provider 和 fallback。

## 这层解决什么问题

系统里并不是所有模型调用都应该用同一个模型。

当前至少有这些 purpose：

- `chat`
- `coding`
- `summary`
- `hybrid`
- `compact`
- `teammate`
- `reflection`
- `task_conclusion`

不同 purpose 可以路由到不同 provider，也可以配置 fallback。

核心实现位于：

- `models/model_pool.py`
- `models/provider.py`
- `models/model_task_runner.py`
- `runtime/bootstrap.py`

`config.py` 现在只保留轻量配置读取和 system prompt helper。模型池不在 import 阶段构造，而是由 `runtime/bootstrap.py` 懒加载，避免普通脚本 import 时立刻读取所有 provider 环境变量或触发网络健康检查。

## ModelProfile

一个 provider 配置会被表示成 `ModelProfile`：

```python
@dataclass(frozen=True)
class ModelProfile:
    name: str
    provider: str
    api_key: str
    base_url: str
    model: str
    max_tokens_param: str = "max_tokens"
    wire_api: str = "chat_completions"
    fallbacks: tuple[str, ...] = ()
```

它描述的是一个具体模型后端，包括：

- provider 名称。
- API key。
- base url。
- 默认 model。
- token 参数名。
- wire api 类型。
- fallback provider 链。

当前默认 provider settings 包含 deepseek、mimo、openai relay、gemini 这类 provider。大多数 provider 走 OpenAI-compatible chat completions；需要特殊协议时由 `wire_api` 区分。

同一个供应商的不同模型应该配置成不同 profile。例如 `deepseek_pro` 和 `deepseek_flash` 都可以使用 `provider="deepseek"`，但 `model` 不同。这样 route、fallback、健康状态和 trace 都能按 profile 粒度区分，而不是只按供应商区分。

## route 是什么

`ModelPool` 不直接按 provider 名调模型，而是按 purpose 找 route。

例如：

```python
model_pool.routed_provider("chat")
model_pool.routed_provider("coding")
model_pool.routed_provider("summary")
```

route 配置来自环境变量，例如：

- `LLM_ROUTE_DEFAULT`
- `LLM_ROUTE_CHAT`
- `LLM_ROUTE_CODING`
- `LLM_ROUTE_SUMMARY`
- `LLM_ROUTE_HYBRID`
- `LLM_ROUTE_COMPACT`
- `LLM_ROUTE_TEAMMATE`
- `LLM_ROUTE_REFLECTION`
- `LLM_ROUTE_TASK_CONCLUSION`

如果某个 purpose 没有直接配置，会走 alias 或 default。

当前 alias 包括：

```python
PURPOSE_ALIASES = {
    "teammate": "coding",
    "reflection": "summary",
    "task_conclusion": "summary",
    "compact": "summary",
}
```

## fallback 怎么工作

`RoutedModelProvider._call_with_fallbacks()` 会按 route chain 依次尝试 provider。

它会收集每次尝试的信息：

- provider
- model
- stream
- status
- error

如果所有 provider 都失败，会抛 `ModelRouteError`。

这个异常里带有：

- `purpose`
- `attempts`
- error message

`ReasoningLoop` 捕获模型调用异常时，会把 route attempts 写入 trace：

- `model.route.attempts`
- `model.call.failed`

这样你能看到到底是哪个 provider、哪个模型、什么错误导致失败。

## Provider 健康状态和自动切换

当前模型池还维护每个 profile 的健康状态。

当某个 provider 调用失败时，`ModelPool.mark_profile_failure()` 会记录：

- consecutive failures
- last error
- last checked time
- disabled until

失败次数达到 `LLM_PROVIDER_FAILURE_THRESHOLD` 后，这个 profile 会在 `LLM_PROVIDER_FAILURE_COOLDOWN_SECONDS` 时间内被视为 unavailable。

后续 `RoutedModelProvider` 调用同一个 purpose 时，会优先跳过 unavailable profile，直接尝试 route chain 里的下一个 provider。如果所有 provider 都不可用，才会回退到原始 chain 再尝试，避免因为健康状态把整条路由永久锁死。

成功调用会清空该 profile 的连续失败状态。

启动时也可以打开轻量健康检查：

```bash
LLM_HEALTHCHECK_ON_STARTUP=1
LLM_HEALTHCHECK_PURPOSES=chat,coding,summary,hybrid
LLM_PROVIDER_FAILURE_THRESHOLD=3
LLM_PROVIDER_FAILURE_COOLDOWN_SECONDS=300
```

启动健康检查会检测这些 purpose 当前 route 的 primary profile。如果 primary 不通，会立即把它标记为 unavailable，并在 cooldown 窗口内切到可用的后备 provider。运行中的普通模型调用失败则按 `LLM_PROVIDER_FAILURE_THRESHOLD` 累计，达到阈值后再进入 cooldown。

默认 `LLM_HEALTHCHECK_ON_STARTUP=0`，避免普通测试或本地 import 时自动打外网。

## Pipeline 如何决定 purpose

`Pipeline._model_purpose()` 很直接：

```python
if profile.tool_mode == "coding":
    return "coding"
return "chat"
```

普通聊天走 `chat` purpose。

coding profile 走 `coding` purpose。

summary、history summarizer、task conclusion 这些一次性模型任务不走主 reasoning loop，而是用 `ModelTaskRunner` 指定 `AgentSpec.model_purpose`。

## ModelTaskRunner 的作用

`ModelTaskRunner` 用在不需要工具循环的模型任务上，例如：

- history summarizer
- task conclusion extractor
- candidate memory extractor
- security RAG route classifier

这些任务只需要一次模型请求，不需要完整 `ReasoningLoop`。

`ModelTaskRunner.run()` 根据 `AgentSpec.model_purpose` 选择 routed provider，并用 `tools=[]`、`tool_choice="none"` 发起一次模型调用。构造 runner 时可以传 `on_error` 回调，调用方可以在辅助模型任务失败时写 trace 或降级，而不是把一次辅助模型失败直接变成主 run 失败。

在 `runtime/bootstrap.py` 中：

```python
model_task_runner = ModelTaskRunner(
    model_pool=model_pool,
    default_max_tokens=800,
)
```

随后 `HistorySummarizer` 会用它，并指定：

```python
AgentSpec(
    name="history_summarizer",
    profile=None,
    model_purpose="summary",
    max_tokens=220,
)
```

这说明 summary 类任务可以和 chat/coding 分开路由。

## provider wire api

`ModelProfile.wire_api` 支持不同 wire api。

测试里覆盖了：

- chat completions
- responses wire api

OpenAI-compatible provider 会把内部 messages/tools 转成对应 provider 请求，并把返回解析成统一的 `LLMResponse`。

`LLMResponse` 至少会被 reasoning loop 使用这些字段：

- `content`
- `tool_calls`
- `raw_message`
- `usage`
- `provider_metadata`

## 空 assistant message 的兼容处理

之前 DeepSeek 报过：

```text
Invalid assistant message: content or tool_calls must be set
```

当前测试覆盖了 provider 会丢弃空 assistant messages，避免把既没有 content 也没有 tool_calls 的 assistant message 发给 chat provider。

这属于 provider 兼容层，而不是 runtime 主循环逻辑。

## 当前边界

当前模型路由已经支持 purpose、fallback、轻量健康状态和 cooldown。它还没有：

- 按成本/延迟动态选择模型。
- 基于任务难度自动升级模型。
- 成熟的熔断器和半开探测策略。
- route 配置热更新。

现在更准确的说法是：静态 purpose route + fallback chain + profile 级失败计数和冷却跳过。

## 总结

模型层的核心是把“用哪个模型”从业务代码里抽出来。

`Pipeline` 和 `ModelTaskRunner` 只表达 purpose，`ModelPool` 决定 provider chain，`RoutedModelProvider` 负责 fallback 和 trace attempts。这样 chat、coding、summary、hybrid 可以独立配置，也便于后续做模型对照评测。
