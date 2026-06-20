# taleclaw Web 流式输出改动记录

## 一、目标

本次将 Web 聊天从“等待完整 JSON 回复”改为“边生成边显示”，并将网页标题和侧栏品牌名改为：

```text
taleclaw
```

原有 `/api/chat` JSON 接口继续保留，脚本调用不受影响。

## 二、浏览器数据流

新增接口：

```text
POST /api/chat/stream
Content-Type: application/json

{
  "session_id": "default",
  "message": "你好"
}
```

响应类型：

```text
application/x-ndjson
```

服务端逐行返回事件：

```json
{"type":"delta","text":"你"}
{"type":"delta","text":"好"}
{"type":"complete","reply":"你好","session":{"messages":[]}}
```

发生异常时返回：

```json
{"type":"error","error":"错误信息","error_type":"RuntimeError"}
```

前端会立即创建一个 AI 消息气泡，将每个 `delta` 追加到同一个气泡。收到 `complete`
后再使用 PostgreSQL 中已经保存的 Session 校准页面内容。

## 三、Nginx 兼容

流式响应包含：

```text
Cache-Control: no-cache, no-transform
X-Accel-Buffering: no
```

因此沿用现有 Nginx `proxy_pass` 配置即可。`X-Accel-Buffering: no` 会要求 Nginx
不要缓存完整回复后再一次性发送。

## 四、后端实现

### `core/provider.py`

新增 `OpenAICompatibleProvider.stream_chat()`：

```text
调用 OpenAI 兼容接口 stream=True
逐段触发 on_text(text)
拼接完整 assistant content
拼接流式 tool_calls 的 name 和 arguments
返回与普通 chat() 一致的 LLMResponse
```

工具调用仍在服务端内部执行。最终回答生成时，浏览器会逐段看到回复。

### `core/pipeline.py`

`Pipeline.run()` 新增可选 `on_text` 回调。传入回调时优先使用 provider 的
`stream_chat()`；不支持流式方法的 provider 会回退到普通 `chat()`，并一次性触发回调。

### `core/runtime.py` 与 `core/agent_loop.py`

将可选回调从 Web 服务传到 Pipeline。模式切换、插件直接回复和 Coding TaskSession
最终回复也会通过相同事件接口返回。

### `web/server.py`

新增 `/api/chat/stream`。HTTP handler 使用线程安全队列接收 agent runtime 的文本片段，
持续写出 NDJSON 并立即 flush。

### `web/static/app.js`

新增 NDJSON reader 和流式消息气泡。请求异常时，已经接收到的部分内容会保留，并在末尾
显示中断原因。

## 五、验证

新增：

```text
tests/test_web_streaming.py
```

覆盖：

```text
文本 delta 按顺序推送
流式 tool_calls 参数重组
HTTP /api/chat/stream 的 delta 和 complete 事件
X-Accel-Buffering: no 响应头
```

验证命令：

```bash
python3 -B -m unittest discover -s tests -v
python3 -B -m py_compile \
  core/provider.py \
  core/pipeline.py \
  core/runtime.py \
  core/agent_loop.py \
  web/server.py
node --check web/static/app.js
git diff --check
```
