# Web 会话自动标题 Checklist

## 标题生成行为

- [ ] C01 首组完整问答后生成非空标题。（验证：运行新会话服务测试，观察 `metadata.title` 被写入）
- [ ] C02 标题模型只收到最早一组问题和回答，不包含后续消息或 Session Context。（验证：检查 fake runner 的实际 messages）
- [ ] C03 标题中的引号、换行和多余空白被清理，最终长度不超过 20 个 Unicode 字符。（验证：运行规范化参数化测试）
- [ ] C04 中文、英文和 emoji 输入按字符安全截断，不追加导致超长的省略号。（验证：运行边界样本测试并检查 `len(title) <= 20`）
- [ ] C05 模型异常、超时和空输出分别使用首问题 fallback。（验证：运行三条失败路径测试）
- [ ] C06 已有标题时保持原值且不调用模型。（验证：fake runner 调用次数为零）
- [ ] C07 没有完整问答时不生成、不保存标题。（验证：分别输入空消息、只有 user 的 Session）
- [ ] C08 升级前无标题的旧会话在下一次成功回答后使用历史首组问答生成标题。（验证：构造多轮旧 Session，检查 runner 输入）

## Web 集成

- [ ] C09 正文所有流式 delta 在标题模型调用开始前已经发出。（验证：用记录顺序的 fake runtime/title service 运行集成测试）
- [ ] C10 标题生成失败不会把成功正文转换为 SSE error。（验证：标题服务失败场景仍收到 complete）
- [ ] C11 标题更新后通过现有 SessionManager 持久化，重新加载结果一致。（验证：隔离 SessionStore 保存并重载）
- [ ] C12 会话列表、会话详情和 SSE complete 返回同一个顶层 title。（验证：运行 Web API/stream 测试）
- [ ] C13 缺少 title 的旧 Session 仍能加载并以 `chat_id` 作为显示 fallback。（验证：运行兼容性测试）
- [ ] C14 标题生成前后 Session ID、chat_id、加载参数和删除参数保持不变。（验证：对比生成前后 API 数据）
- [ ] C15 同一会话的后续请求不会重新生成或覆盖标题。（验证：连续执行两轮，runner 仅调用一次）

## 前端

- [ ] C16 会话列表和当前页显示 `title || chat_id`，但点击、加载和删除继续使用 chat_id。（验证：前端源码契约测试）
- [ ] C17 会话搜索能匹配 title 和 chat_id。（验证：前端源码契约测试或浏览器观察）
- [ ] C18 SSE complete 到达后当前页标题立即更新，无需手动刷新。（验证：流式处理测试或浏览器观察）
- [ ] C19 标题通过 `textContent` 呈现，HTML 字符串不会作为标签执行。（验证：源码契约与恶意标题样本）

## 边界与回归

- [ ] C20 标题只写入 Session metadata，不新增消息、长期记忆、向量点或数据库表。（验证：比较生成前后消息及 schema，并检查依赖边界）
- [ ] C21 `WEB_SESSION_TITLE_TIMEOUT_SECONDS` 默认值为 8，代码与 `.env.example` 一致。（验证：搜索配置并运行默认配置测试）
- [ ] C22 Responses API 与 Chat Completions 的文本和工具调用流式测试继续通过。（验证：运行 `python -m pytest -q tests/test_web_streaming.py tests/test_model_pool_routing.py -k stream`）
- [ ] C23 Web、Session 和模型任务相关定向测试全部通过。（验证：运行 `python -m pytest -q tests/test_web_session_titles.py tests/test_web_streaming.py tests/test_web_session_deletion.py tests/test_model_pool_routing.py`）
- [ ] C24 全仓测试、compileall 和 `git diff --check` 通过。（验证：运行完整 pytest、`python -m compileall -q web runtime models` 和 `git diff --check`）

## 端到端场景

- [ ] E01 新建 Web 会话 → 输入第一个问题 → 正文逐段显示 → 回答完成 → 20 字以内主题出现在当前页和会话列表 → 再问一次后标题保持不变。（验证：使用 fake 完整链路测试或本地 Web 浏览器执行并记录事件顺序与标题）
- [ ] E02 标题模型超时 → 正文仍逐段完成 → complete 正常返回 → 首问题精简文本成为标题。（验证：注入超时 runner，检查 delta、complete 和持久化 Session）
