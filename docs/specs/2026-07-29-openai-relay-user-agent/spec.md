# OpenAI Relay User-Agent Spec

## 背景

OpenAI 兼容中转网关可能根据 HTTP 客户端标识执行访问控制。当前模型调用没有提供可配置的 `User-Agent`，当网关返回权限拒绝时，无法通过配置进行请求头 A/B 测试，只能修改代码或依赖 SDK 默认值。

## 目标

- 为单个模型 Provider Profile 提供可选的 `User-Agent` 配置。
- 允许 `openai_relay` 通过 `OPENAI_RELAY_USER_AGENT` 设置客户端标识。
- 未配置时保持现有请求行为不变。

## 功能需求

- F1: 系统应从 Provider Profile 配置或该 Provider 对应的环境变量读取可选的 `User-Agent`。
- F2: 配置非空时，系统应仅向对应 Provider 的 HTTP 客户端添加 `User-Agent` 请求头。
- F3: 配置缺失或为空时，系统不得额外注入 `User-Agent`。
- F4: 示例配置和部署文档应说明该选项用于中转网关兼容性诊断，并提醒不得放入令牌、Cookie 等凭据。

## 非功能需求

- N1: 配置必须限定在单个 Provider Profile，不得影响回退链中的其他 Provider。
- N2: 不得覆盖或复制 `Authorization`、Cookie、ChatGPT 会话信息等认证数据。
- N3: 保持现有 Provider 路由、模型选择、Responses/Chat Completions 协议行为不变。
- N4: 配置值不得出现在包含 API Key 的敏感日志中。

## 不做的事

- 不伪造 Codex 或 ChatGPT 的私有认证头、内部遥测头或会话身份。
- 不新增任意 HTTP Header 映射能力。
- 不修改网关规则、模型权限或 API Key。
- 不保证更换 `User-Agent` 一定能绕过网关；它只提供受控的兼容性测试能力。

## 验收标准

- AC1: 当 `OPENAI_RELAY_USER_AGENT=codex-cli` 时，`openai_relay` 客户端收到且仅收到新增的 `User-Agent: codex-cli` 默认请求头。
- AC2: 同一路由中的 DeepSeek 等其他 Profile 不接收该请求头。
- AC3: 未设置或设置为空字符串时，客户端构造参数与改动前一致，不包含额外请求头参数。
- AC4: Profile JSON 中的非空 `user_agent` 能实现相同行为，并优先于环境变量。
- AC5: 现有模型池与路由测试全部通过，新增测试覆盖已配置、未配置及 Profile 隔离场景。
- AC6: `.env.example` 和部署说明包含安全提示与配置示例。
