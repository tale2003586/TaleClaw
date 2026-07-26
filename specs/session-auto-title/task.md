# Web 会话自动标题 Tasks

## 文件清单

| 操作 | 文件 | 职责 |
|---|---|---|
| 新建 | `web/session_titles.py` | 首组问答提取、标题规范化、模型调用、超时与兜底 |
| 修改 | `web/server.py` | 标题服务装配、post-turn 调用、API 投影 |
| 修改 | `web/static/app.js` | 会话列表、搜索和当前页标题展示 |
| 修改 | `.env.example` | 标题超时配置说明 |
| 新建 | `tests/test_web_session_titles.py` | 标题领域行为和异步服务测试 |
| 修改 | `tests/test_web_streaming.py` | Web complete 与流式顺序回归测试 |

## T1：实现首组问答提取与标题规范化

**文件：** `web/session_titles.py`

**依赖：** 无

**步骤：**

1. 定义 `SessionTitleResult`。
2. 实现 `first_question_answer`，只选择最早 user 及其后第一条 assistant。
3. 实现 `normalize_session_title`，折叠空白、清理引号并截断到 20 字符。
4. 实现 `session_display_title`，优先 metadata title，缺失时返回稳定 ID。

**验证：** 运行纯函数测试，覆盖中文、英文、emoji、引号、换行、超长和不完整消息序列。

## T2：实现异步标题生成服务

**文件：** `web/session_titles.py`

**依赖：** T1

**步骤：**

1. 定义使用 summary 路由和 48 output tokens 的标题任务规格。
2. 实现 `WebSessionTitleService.ensure_title` 的 existing 和 unavailable 快速路径。
3. 用隔离 Prompt 调用 `ModelTaskRunner`，只传首组问答。
4. 用 `asyncio.to_thread` 与 `wait_for` 限制等待时间。
5. 实现模型结果规范化以及异常、超时、空结果的首问题 fallback。
6. 仅在产生新标题时更新 Session metadata。

**验证：** 运行服务测试，确认 model、fallback、timeout、existing 和 unavailable 五条路径。

## T3：接入 Web post-turn 生命周期

**文件：** `web/server.py`

**依赖：** T2

**步骤：**

1. Web Runtime 启动后从 `RuntimeServices` 取得 `model_task_runner` 并构造标题服务。
2. 在 `_ask_async` 的正文 `run_message` 完成后取得当前 Session。
3. 调用标题服务，仅在 `updated=True` 时再次保存 Session。
4. 保证标题服务位于既有 Session 锁范围内，并发生在 `ask_stream` 返回之前。

**验证：** 集成测试观察正文 delta 先到达、标题随后保存，模型异常不会把成功回答变成 error。

## T4：向 Web API 投影标题

**文件：** `web/server.py`

**依赖：** T1、T3

**步骤：**

1. `read_sessions` 为每条 Web Session 增加顶层 `title`。
2. `read_session` 为存在和不存在的 Session 增加兼容 title 投影。
3. 保持 `id`、`chat_id`、删除和加载参数不变。

**验证：** API 测试确认标题一致、缺失时返回可用 fallback，并且 Session ID 未改变。

## T5：更新 Web 标题展示

**文件：** `web/static/app.js`

**依赖：** T4

**步骤：**

1. 会话列表名称改为 `title || chat_id`，交互标识仍使用 `chat_id`。
2. 过滤搜索同时匹配 title、chat_id 和 mode。
3. `loadSession` 的页面标题优先显示 title。
4. 流式 complete 到达后立即更新当前页面标题。
5. 继续使用 `textContent` 写入标题。

**验证：** 前端契约测试或源码断言确认显示与身份字段分离，恶意标题不会按 HTML 渲染。

## T6：补充配置说明

**文件：** `.env.example`

**依赖：** T2

**步骤：**

1. 增加 `WEB_SESSION_TITLE_TIMEOUT_SECONDS=8`。
2. 说明它只限制回答结束后的标题生成等待，不影响正文模型超时。

**验证：** 搜索配置项，确认默认值和代码默认值一致。

## T7：完成定向测试

**文件：** `tests/test_web_session_titles.py`、`tests/test_web_streaming.py`

**依赖：** T1–T6

**步骤：**

1. 覆盖首轮生成、旧会话按需生成、已有标题幂等。
2. 覆盖模型异常、超时、空输出的 fallback。
3. 覆盖规范化和 20 字符硬限制。
4. 覆盖 API 列表、详情和 complete 返回标题。
5. 覆盖正文 delta 在标题生成前已发送。
6. 保留 Responses API 和 Chat Completions 流式测试。

**验证：** 运行 `python -m pytest -q tests/test_web_session_titles.py tests/test_web_streaming.py`，全部通过。

## T8：执行完整验收并提交

**文件：** 全部变更

**依赖：** T7

**步骤：**

1. 运行 Web、Session、模型路由相关测试。
2. 运行完整测试。
3. 运行 compileall 和 `git diff --check`。
4. 检查工作区只包含本需求文件并提交。

**验证：** 所有命令通过，提交可追溯，工作区干净。

## 执行顺序

```text
T1 → T2 → T3 → T4 → T5
          └────→ T6
T4 + T5 + T6 → T7 → T8
```
