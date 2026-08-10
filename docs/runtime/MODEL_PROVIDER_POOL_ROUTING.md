# 模型路由与 Provider 池

本文说明当前系统如何选择 LLM provider、model、fallback 链，以及一次运行里怎么排查实际命中的模型。

先区分两个容易混淆的“路由”：

- **模式路由**：`ModeRouter` 判断用户这一轮走 `bot`、`coding`、`hybrid`，入口在 `runtime/routing/router.py`。
- **模型路由**：`ModelPool` 根据调用用途 `purpose` 选择具体 provider/model，入口在 `models/model_pool.py`。

这篇主要讲第二个：模型路由。

## 核心结论

当前模型路由是：

```text
业务调用点
  -> 产生 AgentSpec.model_purpose 或 purpose 字符串
  -> ModelPool.route_profile_names(purpose)
  -> RoutedModelProvider 按 profile 链依次调用
  -> 成功返回 LLMResponse；失败时 fallback
  -> trace 记录 selected profile/model 和 attempts
```

它不是按 query embedding 动态选择模型，也不是按问题难度自动升级模型。现在更准确地说，它是：

```text
静态 purpose 路由 + profile fallback 链 + profile 健康状态冷却跳过
```

## 代码位置

主要文件：

- `models/model_pool.py`：模型池、profile、route、fallback、健康状态。
- `models/provider.py`：OpenAI-compatible provider 适配层，统一 `chat_completions` 和 `responses` wire api。
- `models/model_task_runner.py`：一次性无工具模型任务，例如总结、RAG rewrite、RAG classifier。
- `runtime/bootstrap.py`：启动时构建 `ModelPool`，注入主 `Pipeline`、Hybrid classifier、RAG、Memory、Reflection。
- `runtime/agent_runner.py`：主 reasoning loop 根据 `AgentSpec.model_purpose` 取 provider/model。
- `runtime/pipeline.py`：普通对话和 coding 的 purpose 映射。
- `runtime/reasoning_loop.py`：实际调用模型，并把 route attempts 写入 trace。

## ModelProfile

一个可调用的模型后端会被解析成 `ModelProfile`：

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

字段含义：

- `name`：系统内部 profile 名，例如 `deepseek_pro`、`openai_relay`。
- `provider`：供应商类型，例如 `deepseek`、`mimo`、`glm47`、`openai_relay`。
- `api_key`：密钥，来自 env 或 JSON；文档和 trace 不应展示明文。
- `base_url`：OpenAI-compatible base URL。
- `model`：该 profile 默认模型。
- `max_tokens_param`：不同 provider 的 token 参数名，比如 MiMo 用 `max_completion_tokens`。
- `wire_api`：`chat_completions` 或 `responses`。
- `fallbacks`：这个 profile 自带的 fallback profile 名。

同一个供应商的不同模型建议配成不同 profile。例如：

```json
{
  "deepseek_pro": {"provider": "deepseek", "model": "deepseek-v4-pro"},
  "deepseek_flash": {"provider": "deepseek", "model": "deepseek-v4-flash"}
}
```

这样 route、fallback、健康状态、trace 都能按 profile 粒度区分。

## Web 前端选择模型与思考模式

Web Chat 的模型菜单来自 `/api/health` 返回的已配置 profile。用户选择的是
profile，而不是任意输入一个模型字符串；选择只作用于当前请求，不会修改全局
route，也不会影响其他用户。未选择时保持原有自动 route 和 fallback 行为。

思考模式同样按 profile 能力判断。某个 profile 只有同时声明
`supports_thinking: true` 和实际 API 参数名 `thinking_param` 后，前端才会启用
“深度思考”按钮。例如，具体 relay 文档确认参数为 `reasoning_effort` 时：

```json
{
  "openai_relay": {
    "api_key_env": "OPENAI_RELAY_API_KEY",
    "base_url": "https://relay.example.com/v1",
    "model": "reasoning-model",
    "supports_thinking": true,
    "thinking_param": "reasoning_effort",
    "thinking_value": "high"
  },
  "plain_model": {
    "api_key_env": "PLAIN_MODEL_API_KEY",
    "base_url": "https://plain.example.com/v1",
    "model": "plain-model"
  }
}
```

`thinking_param` 和 `thinking_value` 必须以供应商实际 API 文档为准。
`thinking_value` 默认是布尔值 `true`，也可配置字符串、数字或 JSON 对象；不能仅
因为模型名称包含 `reasoning` 就猜测参数。当前 `.env` 的 6 个 profile 都没有能力
字段，因此
前端禁用思考按钮是预期行为；配置并重启 Web 服务后，支持思考的 profile 会自动
显示为可选模型并启用按钮。

## Provider 配置来源

入口是：

```python
build_model_pool_from_env()
```

配置有两种方式。

### 方式一：单 provider 或内置 provider

最简单可以只配：

```env
LLM_PROVIDER=deepseek
DEEPSEEK_API_KEY=...
DEEPSEEK_BASE_URL=https://api.deepseek.com
DEEPSEEK_MODEL=deepseek-v4-pro
```

内置 provider 默认值在 `models/model_pool.py` 的 `DEFAULT_PROVIDER_SETTINGS`：

- `deepseek`
- `mimo`
- `openai`
- `gemini`

如果没有 `LLM_PROVIDERS_JSON`，系统会先构建 `LLM_PROVIDER` 指定的 selected profile，再尝试把有 API key 的内置 provider 也加入池。

### 方式二：LLM_PROVIDERS_JSON

复杂场景推荐用 JSON：

```env
LLM_PROVIDERS_JSON='{
  "deepseek_pro": {
    "provider": "deepseek",
    "model": "deepseek-v4-pro"
  },
  "deepseek_flash": {
    "provider": "deepseek",
    "model": "deepseek-v4-flash"
  },
  "mimo_v25_pro": {
    "provider": "mimo",
    "model": "mimo-v2.5-pro",
    "max_tokens_param": "max_completion_tokens"
  },
  "openai_relay": {
    "provider": "openai_relay",
    "model": "gpt-5.5"
  },
  "glm4.7flash": {
    "provider": "glm47",
    "model": "glm-4.7-flash"
  }
}'
```

JSON profile 可以直接写 `api_key`，也可以写 `api_key_env` 引用环境变量。建议用 `api_key_env` 或 provider env，不要把 key 写进文档。

## route 是什么

模型路由不是直接写“我要用 deepseek”，而是写“这个任务目的是什么”。

例如：

```python
model_pool.routed_provider("chat")
model_pool.model_for("chat")

model_pool.routed_provider("coding")
model_pool.model_for("coding")

model_pool.routed_provider("summary")
model_pool.model_for("summary")
```

`purpose -> profile chain` 的配置来自：

- `LLM_ROUTES_JSON`
- `LLM_ROUTE_DEFAULT`
- `LLM_ROUTE_CHAT`
- `LLM_ROUTE_CODING`
- `LLM_ROUTE_SUMMARY`
- `LLM_ROUTE_HYBRID`
- `LLM_ROUTE_COMPACT`
- `LLM_ROUTE_TEAMMATE`
- `LLM_ROUTE_REFLECTION`
- `LLM_ROUTE_TASK_CONCLUSION`
- `LLM_ROUTE_FALLBACK`

`LLM_ROUTE_FALLBACK` 会追加到所有已有 route 后面，并自动去重。

### purpose alias

代码里有一组 alias：

```python
PURPOSE_ALIASES = {
    "teammate": "coding",
    "reflection": "summary",
    "task_conclusion": "summary",
    "compact": "summary",
}
```

意思是：如果没有显式配置 `teammate`，它会尝试使用 `coding`；如果没有显式配置 `reflection`，它会尝试使用 `summary`。

如果都找不到，最后走 `default`。如果 `default` 也没有显式配置，就走 `LLM_PROVIDER` 对应的默认 profile。

## fallback 怎么工作

route 值可以是一个 profile，也可以是逗号分隔 fallback 链：

```env
LLM_ROUTE_CODING=deepseek_pro,mimo_v25_pro,openai_relay
```

调用时 `RoutedModelProvider._call_with_fallbacks()` 会按顺序尝试：

```text
deepseek_pro -> mimo_v25_pro -> openai_relay
```

非流式调用：

- 当前 profile 报错，记录失败。
- 自动尝试下一个 profile。
- 成功后返回。
- 全部失败时抛 `ModelRouteError`，异常里包含所有 attempts。

流式调用：

- 如果 primary 在输出任何文字前失败，可以 fallback。
- 如果已经向用户流式输出了部分文本，再失败就直接抛错，不继续 fallback。
- 这样避免用户看到两个模型混杂的输出。

## 健康状态与冷却

`ModelPool` 维护 profile 级健康状态：

- `consecutive_failures`
- `disabled_until`
- `last_error`
- `last_checked_at`

相关 env：

```env
LLM_PROVIDER_FAILURE_THRESHOLD=3
LLM_PROVIDER_FAILURE_COOLDOWN_SECONDS=300
LLM_HEALTHCHECK_ON_STARTUP=0
LLM_HEALTHCHECK_PURPOSES=chat,coding,summary,hybrid
```

行为：

- 普通模型调用失败会累计该 profile 的连续失败次数。
- 达到 `LLM_PROVIDER_FAILURE_THRESHOLD` 后，该 profile 会进入冷却窗口。
- 冷却期间，同 purpose 会优先跳过这个 profile。
- 如果所有 profile 都不可用，会退回原始 chain 再尝试，避免被健康状态永久锁死。
- 成功调用会清空该 profile 的失败状态。

启动健康检查默认关闭。开启后，系统会检查 `LLM_HEALTHCHECK_PURPOSES` 里每个 purpose 的 primary profile。

## wire_api

`models/provider.py` 的 `OpenAICompatibleProvider` 支持两种 wire api：

- `chat_completions`
- `responses`

示例：

```env
OPENAI_RELAY_WIRE_API=responses
GEMINI_WIRE_API=chat_completions
```

不同 wire api 会在请求层有差异，但都会被解析成统一的 `LLMResponse`：

```python
@dataclass
class LLMResponse:
    content: str | None
    tool_calls: list[ToolCall]
    raw_message: dict | None
    usage: LLMUsage | None
    provider_metadata: dict
```

主循环只关心统一后的 `content/tool_calls/usage/provider_metadata`。

## 主 Pipeline 如何选 purpose

`runtime/pipeline.py` 的规则很简单：

```python
def _model_purpose(self, session, profile) -> str:
    if profile.tool_mode == "coding":
        return "coding"
    return "chat"
```

也就是说：

- `BOT_PROFILE` -> `chat`
- `CODING_PROFILE` -> `coding`

`ModeRouter` 负责决定本轮 profile 是 bot 还是 coding；`ModelPool` 再负责把 `chat/coding` 映射到具体模型。

## Hybrid 模式如何用模型

Hybrid 模式有两层：

```text
用户消息
  -> ModeRouter / IntentClassifier
  -> 必要时 HybridModeClassifier 调用模型判断是否进入 coding
  -> 进入 bot 或 coding profile
  -> Pipeline 再按 profile 选择 chat/coding purpose
```

`HybridModeClassifier` 自己使用：

```python
model_pool.routed_provider("hybrid")
model_pool.model_for("hybrid")
```

它要求模型返回 JSON：

```json
{"mode":"coding|bot","reason":"short explanation"}
```

如果 hybrid classifier 调用失败或返回非法 JSON，会保守回落到 bot。

## ModelTaskRunner 的用途

不需要工具循环的一次性模型任务用 `ModelTaskRunner`：

```python
runner.run(
    spec=AgentSpec(
        name="history_summarizer",
        profile=None,
        model_purpose="summary",
        max_tokens=220,
    ),
    messages=[...],
)
```

它会：

- 根据 `AgentSpec.model_purpose` 选择 routed provider/model。
- `tools=[]`
- `tool_choice="none"`
- 执行一次 `provider.chat()`。

当前使用场景包括：

- `summary`：历史总结、候选记忆提取。
- `task_conclusion`：Coding task 完成后的结论抽取。
- `security_rag_route_classifier`：安全 RAG 的 LLM 路由判别。
- `security_rag_query_rewriter`：安全 RAG 的 LLM rewrite。

## 安全 RAG 相关模型路由

安全 RAG 自己有两种可选 LLM 调用：

- `SECURITY_RAG_ROUTE_LLM_ENABLED=1`：低置信/模糊情况下调用 LLM classifier。
- `SECURITY_RAG_REWRITE_LLM_ENABLED=1`：rewrite provider 用 LLM 生成改写 query。

它们的 purpose 分别由这些 env 决定：

```env
SECURITY_RAG_ROUTE_LLM_PURPOSE=summary
SECURITY_RAG_REWRITE_LLM_PURPOSE=summary
```

默认都走 `summary`。如果想让安全 RAG 独立走 GLM，可以这样配置：

```env
SECURITY_RAG_ROUTE_LLM_PURPOSE=security_rag
SECURITY_RAG_REWRITE_LLM_PURPOSE=security_rag
LLM_ROUTES_JSON='{"security_rag":["glm4.7flash","deepseek_flash","mimo_v25_pro","openai_relay"]}'
```

注意：当前 `models/model_pool.py` 的 `ROUTE_ENV_NAMES` 没有包含 `security_rag`，所以单独写：

```env
LLM_ROUTE_SECURITY_RAG=glm4.7flash,deepseek_flash
```

目前不会被自动读取。要么使用 `LLM_ROUTES_JSON`，要么在 `ROUTE_ENV_NAMES` 里补一项。

## 你当前 .env 的非密钥路由状态

根据当前 `.env`，profile 大致是：

| profile | provider | model | wire api / token 参数 |
| --- | --- | --- | --- |
| `openai_relay` | `openai_relay` | `gpt-5.5` | `responses` / `max_tokens` |
| `deepseek_pro` | `deepseek` | `deepseek-v4-pro` | `chat_completions` / `max_tokens` |
| `deepseek_flash` | `deepseek` | `deepseek-v4-flash` | `chat_completions` / `max_tokens` |
| `mimo_v25_pro` | `mimo` | `mimo-v2.5-pro` | `chat_completions` / `max_completion_tokens` |
| `gemini_v35_flash` | `gemini` | `gemini-3.5-flash` | `chat_completions` / `max_tokens` |
| `glm4.7flash` | `glm47` | `glm-4.7-flash` | `chat_completions` / `max_tokens` |

当前 route：

| purpose | 当前 route chain |
| --- | --- |
| `default` | `openai_relay -> deepseek_flash -> mimo_v25_pro` |
| `chat` | `openai_relay` |
| `coding` | `deepseek_pro -> mimo_v25_pro -> openai_relay` |
| `summary` | `deepseek_flash` |
| `hybrid` | `openai_relay -> deepseek_flash -> mimo_v25_pro` |
| `teammate` | `openai_relay -> deepseek_pro -> mimo_v25_pro` |
| `reflection` | `openai_relay -> deepseek_pro -> mimo_v25_pro` |
| `compact` | `openai_relay -> deepseek_flash -> mimo_v25_pro` |
| `task_conclusion` | `openai_relay -> deepseek_flash -> mimo_v25_pro` |

当前安全 RAG LLM classifier：

```env
SECURITY_RAG_ROUTE_LLM_ENABLED=1
SECURITY_RAG_ROUTE_LLM_PURPOSE=summary
```

因此它实际会走 `summary`，也就是 `deepseek_flash`，不是 `LLM_ROUTE_SECURITY_RAG`。

## trace 里怎么看实际命中的模型

一次 run 完成后，可以看：

```bash
rg '"event": "model.call.completed"' .runs/<run_id>/trace.jsonl
rg '"provider_metadata"' .runs/<run_id>/trace.jsonl
jq '.models, .model_retry_count, .model_route_attempts' .runs/<run_id>/metrics.json
```

`model.call.completed` 的 payload 里有：

```json
{
  "model": "...",
  "provider": "RoutedModelProvider",
  "provider_metadata": {
    "route_purpose": "coding",
    "selected_profile": "deepseek_pro",
    "selected_provider": "deepseek",
    "selected_model": "deepseek-v4-pro",
    "retry_count": 0,
    "attempts": [
      {"profile": "deepseek_pro", "provider": "deepseek", "model": "deepseek-v4-pro", "status": "success"}
    ]
  }
}
```

如果所有 provider 都失败，`ReasoningLoop` 会记录：

- `model.route.attempts`
- `model.call.failed`

可以这样查：

```bash
rg '"event": "model.route.attempts"|"event": "model.call.failed"' .runs/<run_id>/trace.jsonl
```

## 常见误区

1. `model_for(purpose)` 返回的是当前可用 route chain 的第一个 profile 的 model。

如果 primary 因健康状态处于 cooldown，它可能返回 fallback model。

2. 主调用传入的 `model` 只对配置链里的 primary profile 保留。

fallback profile 会使用自己的 `ModelProfile.model`，不会继续使用 primary 的模型名。

3. `LLM_ROUTE_FALLBACK` 是追加到所有 route 后面，不是替换 route。

例如：

```env
LLM_ROUTE_CHAT=openai_relay
LLM_ROUTE_FALLBACK=deepseek_flash,mimo_v25_pro
```

最终 `chat` 是：

```text
openai_relay -> deepseek_flash -> mimo_v25_pro
```

4. `LLM_ROUTE_SECURITY_RAG` 当前不会自动生效。

安全 RAG 要独立 purpose，请使用 `LLM_ROUTES_JSON` 或补代码。

5. Hybrid classifier 的 `HYBRID_ROUTE_MODEL` 只覆盖 `hybrid` classifier 请求里的 model 名。

为空时使用 `model_pool.model_for("hybrid")`。

## 最小配置示例

单模型：

```env
LLM_PROVIDER=deepseek
DEEPSEEK_API_KEY=...
DEEPSEEK_BASE_URL=https://api.deepseek.com
DEEPSEEK_MODEL=deepseek-v4-pro
```

多模型：

```env
LLM_PROVIDER=openai_relay
LLM_PROVIDERS_JSON='{
  "openai_relay": {"provider":"openai_relay","model":"gpt-5.5"},
  "deepseek_pro": {"provider":"deepseek","model":"deepseek-v4-pro"},
  "deepseek_flash": {"provider":"deepseek","model":"deepseek-v4-flash"},
  "mimo_v25_pro": {"provider":"mimo","model":"mimo-v2.5-pro","max_tokens_param":"max_completion_tokens"}
}'

LLM_ROUTE_CHAT=openai_relay
LLM_ROUTE_CODING=deepseek_pro,mimo_v25_pro,openai_relay
LLM_ROUTE_SUMMARY=deepseek_flash
LLM_ROUTE_HYBRID=openai_relay,deepseek_flash,mimo_v25_pro
LLM_ROUTE_FALLBACK=
```

安全 RAG 独立 GLM route：

```env
SECURITY_RAG_ROUTE_LLM_ENABLED=1
SECURITY_RAG_ROUTE_LLM_PURPOSE=security_rag
SECURITY_RAG_REWRITE_LLM_ENABLED=1
SECURITY_RAG_REWRITE_LLM_PURPOSE=security_rag

LLM_ROUTES_JSON='{
  "security_rag": ["glm4.7flash", "deepseek_flash", "mimo_v25_pro", "openai_relay"]
}'
```

## 目前边界

当前还没有：

- 按 query 难度动态选模型。
- 按实时价格/延迟动态选模型。
- 半开探测和复杂熔断器。
- route 配置热更新。
- 对 `LLM_ROUTE_SECURITY_RAG` 这类任意 `LLM_ROUTE_*` 自动发现。

现在这套设计的优点是简单、可解释、可评测：业务层只声明 purpose，模型池负责 provider chain、fallback、健康状态和 trace。
