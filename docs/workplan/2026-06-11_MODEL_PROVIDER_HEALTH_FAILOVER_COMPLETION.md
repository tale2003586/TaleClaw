# Model Provider 健康检查与自动切换完成记录

日期：2026-06-11

## 任务背景

当前模型路由已经支持 route chain 和 fallback，但 primary provider 如果启动时不可用，或者运行中连续报错，系统仍会优先尝试它。

这会带来两个问题：

- 启动后默认 provider 已经不可用，但系统直到真实请求时才暴露问题。
- provider 连续失败时，每次请求都先等 primary 失败，再走 fallback，浪费时间并污染 trace。

本次改动目标是：在模型池里维护 provider 健康状态，启动时可选检测 primary provider，运行中连续失败后自动临时跳过坏 provider。

## 改动范围

涉及文件：

- `models/model_pool.py`
- `config.py`
- `tests/test_model_pool_routing.py`
- `docs/system-design/04-模型路由与Provider池.md`

## 核心实现

### 1. ModelPool 增加健康状态

新增 `ModelProfileHealth`，记录：

- `consecutive_failures`
- `disabled_until`
- `last_error`
- `last_checked_at`

`ModelPool` 新增方法：

- `profile_available(profile_name)`
- `profile_health(profile_name)`
- `mark_profile_success(profile_name)`
- `mark_profile_failure(profile_name, exc)`
- `health_check_profile(profile_name)`
- `health_check_purposes(purposes)`

### 2. 运行中自动降级

`RoutedModelProvider._call_with_fallbacks()` 现在会通过：

```python
self.pool.route_profiles_for_call(self.purpose)
```

获取本次实际尝试链。

如果 primary profile 已经处于 cooldown 窗口，route 会优先跳过它，直接尝试 backup provider。

如果所有 profile 都不可用，则回退到原始 chain 再尝试，避免因为健康状态导致完全无法请求。

### 3. 成功和失败都会更新健康状态

模型调用成功后：

```python
self.pool.mark_profile_success(profile.name)
```

模型调用失败后：

```python
self.pool.mark_profile_failure(profile.name, exc)
```

流式请求如果已经输出了部分文本再失败，不会 fallback 到下一个 provider，但仍会记录当前 provider 的失败状态。

启动健康检查使用更直接的语义：一次 ping 不通就把对应 primary profile 标为 unavailable。运行中的普通请求仍然按连续失败阈值累计。

### 4. 启动健康检查可配置

`config.py` 新增：

```python
MODEL_HEALTHCHECK_ON_STARTUP
MODEL_HEALTHCHECK_PURPOSES
MODEL_HEALTHCHECK_RESULTS
```

对应环境变量：

```bash
LLM_HEALTHCHECK_ON_STARTUP=1
LLM_HEALTHCHECK_PURPOSES=chat,coding,summary,hybrid
LLM_PROVIDER_FAILURE_THRESHOLD=3
LLM_PROVIDER_FAILURE_COOLDOWN_SECONDS=300
```

默认不启用启动健康检查，避免测试和普通 import 自动访问外部模型服务。

### 5. model_for / client_for_purpose 健康感知

`ModelPool.profile_for()`、`model_for()`、`client_for_purpose()` 现在会优先返回当前可用 profile。

这意味着启动健康检查把 primary 标为不可用后，后续 `MODEL_POOL.model_for("chat")` 会返回 backup profile 的 model。

## 验证方式

运行模型路由测试：

```bash
python -m unittest discover -s tests -p 'test_model_pool_routing.py' -v
```

结果：

```text
Ran 13 tests
OK
```

新增覆盖：

- health check 失败后 active model 切到 backup。
- primary 达到失败阈值后，下一次 routed call 直接跳过 primary。
- backup provider 使用自己的 profile model，不会误用 primary 的 override model。

## 使用方式

示例 `.env`：

```bash
LLM_ROUTE_CHAT=deepseek,mimo,openai
LLM_ROUTE_CODING=deepseek,mimo

LLM_HEALTHCHECK_ON_STARTUP=1
LLM_HEALTHCHECK_PURPOSES=chat,coding
LLM_PROVIDER_FAILURE_THRESHOLD=2
LLM_PROVIDER_FAILURE_COOLDOWN_SECONDS=300
```

含义：

- 启动时检测 chat/coding 的 primary provider。
- 某个 provider 连续失败 2 次后，5 分钟内跳过它。
- cooldown 后会重新尝试该 provider，成功则清空失败状态。

## 后续建议

- 把 provider health 状态展示到 status command 或 Web trace viewer。
- 将 health check 结果写入启动日志或 runtime trace。
- 增加 provider 级熔断统计，例如最近 10 分钟失败率。
- 未来可以支持按延迟、成本和错误率动态调整 route 顺序。
