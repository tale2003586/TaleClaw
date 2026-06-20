# Web 会话删除改动记录

## 一、目标

Web 侧栏中的普通 Web 会话现在可以删除。删除当前会话后：

- 如果仍有其他 Web 会话，页面自动切换到最近的可用会话。
- 如果没有其他 Web 会话，页面自动创建一个新的空白会话。

CLI 会话和内部 TaskSession 不允许通过 Web 页面删除。

## 二、后端改动

`SessionStore` 新增：

```text
delete_session(session_id)
```

它会在同一事务中删除 PostgreSQL 中的消息和会话记录。

`SessionManager` 新增：

```text
delete(session_id)
```

它会同时清理 runtime 内存缓存和 PostgreSQL 记录。这样已经加载过的会话不会在后续保存时重新出现。

Web API 新增：

```http
DELETE /api/session
Content-Type: application/json

{
  "session_id": "default"
}
```

接口只接受普通 Web 会话 ID 或 `web:<chat_id>`，并拒绝删除 `task:`、`cli:` 等其他来源的会话。

## 三、前端改动

侧栏中每个 Web 会话右侧新增删除按钮。点击后会显示二次确认：

```text
删除会话 <id>？此操作不能撤销。
```

消息流式输出期间，删除按钮会暂时禁用，避免会话正在写入时触发删除。

## 四、验证

运行：

```bash
node --check web/static/app.js
python3 -B -m unittest discover -s tests -v
```

新增测试覆盖：

- PostgreSQL 会话和消息同步删除
- Runtime 缓存同步清理
- 普通 Web ID 规范化
- 拒绝删除非 Web 会话
- HTTP 删除接口返回更新后的会话列表
