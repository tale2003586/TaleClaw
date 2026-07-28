# TaleClaw Web 控制台前端改版 Checklist

> 每项都通过运行代码或观察页面行为验证；所有条目通过后才视为本轮完成。

## 实现完整性

- [ ] 统一导航包含聊天、日志、Runs、设置、文件、分析、记忆和状态，且每个入口打开唯一主视图。（验证：逐个点击导航，标题与内容同步切换，页面不刷新）
- [ ] 聊天页保留会话搜索、新会话、删除、模式切换、流式输出、停止、Markdown、工具活动和 Workspace 行为。（验证：执行一次普通聊天和一次带工具活动的聊天，观察完整交互）
- [ ] 日志页显示选中 Run 的真实 Trace Event，并呈现时间、级别、来源和摘要。（验证：选择 `.runs` 中有事件的 Run，对照 `/api/run` 返回的 event 数和关键事件名）
- [ ] 日志关键词、级别和 Run 筛选可组合使用，刷新后重新读取当前 Run。（验证：分别筛选 INFO/WARN/ERROR、输入事件关键词并切换 Run，观察结果数量变化）
- [ ] Runs 页显示真实 Run 列表以及状态、模式、会话、时间、步骤、模型和工具调用。（验证：对照 `/api/runs` 中至少两个 Run 的字段）
- [ ] Trace 详情显示真实指标、请求摘要、步骤、模型/工具事件、Subagent、错误或停止原因、Run State 和最终结果。（验证：分别选择 completed、stopped/failed 和带工具调用的 Run）
- [ ] 无法归属到 Step 的事件仍在 Run 事件区可见。（验证：选择包含 `run.started` 或 `route_selected` 的 Run，确认事件未丢失）
- [ ] 设置页展示 Provider/API、模型路由、上下文、记忆/RAG、反思和 Subagent 分组。（验证：打开设置页逐组观察）
- [ ] 设置页明确提示“尚未接入保存”，提交或操作不会发送配置请求，也不会写入 `.env` 或配置型 `localStorage`。（验证：观察 Network/Storage，并在操作前后比较 `.env` 校验和）
- [ ] 文件浏览、预览、上传、新建目录、重命名、删除和下载入口保留。（验证：在临时测试目录完成一轮可恢复的文件操作）
- [ ] 分析页可提交文本、显示回复并提供记录下载。（验证：提交一段测试文本并下载对应记录）
- [ ] 记忆页可选择文件、刷新并显示后端返回内容，空数据时显示空态。（验证：对照 `/api/memory` 返回值）
- [ ] 状态页展示当前用户、角色、会话、模式、Runtime 健康、应用路径、Coding Workspace 和管理员 Run 概览。（验证：对照 `/api/health`、`/api/runtime-health` 与 `/api/runs`）

## 数据与容错

- [ ] Run Detail 缓存按 Run ID 隔离，切换回已加载 Run 不重复请求，手动刷新会重新请求。（验证：观察 Network 请求次数）
- [ ] 缺少 metrics、payload、step、span、时间或最终结果时页面仍可渲染，并显示“不适用”或空态。（验证：用最小 Run Detail fixture 调用渲染逻辑）
- [ ] 未知 Event 不会被丢弃或触发异常，而是以原始事件名和安全摘要展示。（验证：注入 `custom.unknown.event` fixture）
- [ ] 超长 Run ID、路径、事件名和 Payload 不会撑破页面；Payload 可折叠并在区域内滚动。（验证：使用超长 fixture 并观察桌面与移动布局）
- [ ] 动态 Trace、Run、文件和会话文本不作为 HTML 执行。（验证：fixture 中加入 `<img src=x onerror=...>`，页面只显示文本）
- [ ] `/api/runtime-health` 失败时状态页显示异常或不可用，其他只读页面仍能使用。（验证：模拟 503 响应并切换文件、记忆页面）
- [ ] 没有 Run 时日志和 Runs 页面显示明确空态，不发生未处理异常。（验证：用空 Run 列表 fixture）

## 权限与安全

- [ ] 管理员可以看到并进入日志和 Runs 页面。（验证：使用管理员账号登录）
- [ ] 普通用户看不到日志和 Runs 导航，刷新保存的管理员视图时回退聊天页。（验证：普通用户设置 `mainView=runs` 后刷新）
- [ ] 普通用户直接请求 `/api/runs` 或 `/api/run` 仍被现有后端拒绝。（验证：以普通用户会话执行 HTTP 请求并检查状态码）
- [ ] 新页面不读取、回传、显示或缓存 API Key、密码和 Token。（验证：检查 Network、DOM、Storage，并搜索新增前端代码中的敏感配置访问）

## 集成

- [ ] 导航切换与移动侧栏、桌面折叠状态兼容。（验证：桌面折叠后切换全部视图，再在移动端打开/关闭抽屉）
- [ ] Run 数据控制器同时驱动日志、Trace 和状态概览，三个页面的当前 Run 信息一致。（验证：选择同一 Run 后对照 ID 和状态）
- [ ] 现有聊天流式 Activity 中的 Run 链接可以进入新的 Runs/Trace 视图或仍可访问兼容详情。（验证：完成一次生成后点击 Run 链接）
- [ ] 文件预览 Dialog、Escape 关闭、侧栏 Escape 关闭不互相冲突。（验证：分别打开弹窗和移动侧栏并按 Escape）
- [ ] 页面刷新恢复合法主视图；未知视图和无权限视图回退聊天页。（验证：修改 `localStorage.mainView` 后刷新）

## 编译与测试

- [ ] JavaScript 语法检查通过。（验证：运行 `node --check web/static/app.js`）
- [ ] HTML 静态契约测试通过且不存在重复 ID 或悬空导航。（验证：运行 `pytest -q tests/test_web_console_frontend.py`）
- [ ] Web 登录、会话、流式输出和 Trace 相关测试通过。（验证：运行相关 `pytest` 测试集合）
- [ ] 静态资源与现有 API 的 HTTP 冒烟检查通过。（验证：启动本地 Web 服务，请求首页、CSS、JS、health、sessions，以及管理员 runs/run 接口）
- [ ] 主要视图切换与数据加载期间浏览器控制台无未处理异常。（验证：打开 DevTools 依次进入八个视图并刷新数据）

## 响应式与可访问性

- [ ] 1440×900 下导航、聊天、日志、Trace、设置和工具页无页面级横向滚动。（验证：逐页检查 `document.documentElement.scrollWidth <= clientWidth`）
- [ ] 390×844 下侧栏为抽屉，打开后可用按钮、遮罩和 Escape 关闭，不遮挡关闭后的主内容。（验证：移动视口实际操作）
- [ ] 日志表格、Trace 时间线、JSON Payload 和长路径在移动端可读且滚动受控。（验证：390×844 下选择复杂 Run）
- [ ] 所有可交互元素有可见焦点，键盘可进入导航、筛选、列表、详情和表单。（验证：只用 Tab、Shift+Tab、Enter、Space 和 Escape 操作）
- [ ] 状态变化和加载错误具有可读文本，不只依赖颜色表达。（验证：观察 ready、warn、error 和 empty 状态）

## 端到端场景

- [ ] 场景 1：管理员登录 → 在聊天页完成一次请求 → 打开 Runs → 选择刚产生的 Run → 查看真实指标和 Trace → 切到日志页筛选该 Run 的事件。（预期：Run ID、状态和事件一致，整个流程无刷新与异常）
- [ ] 场景 2：管理员进入设置 → 调整预览控件 → 点击不可保存入口 → 返回聊天。（预期：看到未接入提示，`.env`、Network 和 Storage 均无配置变更）
- [ ] 场景 3：普通用户登录 → 使用聊天、文件、分析、记忆和状态 → 尝试恢复 `runs` 视图。（预期：常规页面可用，管理员入口不可见并回退聊天）
- [ ] 场景 4：Run 列表为空或 Run Detail 请求失败 → 打开日志与 Runs。（预期：显示明确空态/错误态，导航和其他页面继续工作）

## 验收执行记录（2026-07-28）

- [x] JavaScript、HTML 契约与回归测试通过：`node --check`、`git diff --check`、76 项 Web/Trace 测试全部通过。
- [x] 管理员真实数据冒烟通过：浏览器读取 100 个 Run、当前 Run 21 条日志事件与 Trace Step，HTTP health/runs/run 均返回成功。
- [x] 八个主视图切换通过：每次仅一个 active panel，1440×900 下页面宽度 1440/1440，浏览器无 error/warning/未处理异常。
- [x] 移动端通过：390×844 下页面宽度 390/390，Runs 列表与详情分区可滚动，侧栏抽屉可打开关闭。
- [x] 权限前端行为通过：普通用户状态下 Logs/Runs 入口隐藏，持久化 `mainView=runs` 会回退到 Chat；后端管理员约束由既有 Web 鉴权测试覆盖。
- [x] 设置预览安全边界通过：操作控件并提交后只显示未接入提示，Network 无配置请求，Local Storage 仅含既有视图/侧栏键，`.env` SHA-256 前后一致。
- [x] 容错与内容安全通过：未知 Event、缺失 metrics 与恶意 HTML fixture 均成功渲染；指标显示“不适用”，注入文本未生成 DOM 元素或执行脚本。
- [x] 既有聊天、会话、上传解析、模式切换与 Run Trace 回归通过：相关测试集合 76/76 通过。
