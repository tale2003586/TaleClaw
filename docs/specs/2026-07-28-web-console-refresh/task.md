# TaleClaw Web 控制台前端改版 Tasks

## 文件清单

| 操作 | 文件 | 职责 |
|---|---|---|
| 修改 | `web/static/index.html` | 重组应用外壳并增加所有主视图语义结构 |
| 修改 | `web/static/app.js` | 导航权限、Runs、日志、Trace、状态与设置原型交互 |
| 修改 | `web/static/styles.css` | 控制台视觉、页面组件与响应式布局 |
| 新建 | `tests/test_web_console_frontend.py` | 前端静态结构与关键契约回归测试 |
| 已建 | `docs/specs/2026-07-28-web-console-refresh/spec.md` | 已批准需求规格 |
| 已建 | `docs/specs/2026-07-28-web-console-refresh/plan.md` | 已批准技术设计 |
| 已建 | `docs/specs/2026-07-28-web-console-refresh/task.md` | 本任务拆解 |
| 待建 | `docs/specs/2026-07-28-web-console-refresh/checklist.md` | 验收清单 |

## T1: 建立前端静态契约测试

**文件：** `tests/test_web_console_frontend.py`

**依赖：** 无

**步骤：**

1. 使用标准库 HTML Parser 读取 `index.html`，收集元素 ID、主视图名、导航目标和管理员权限标记。
2. 断言所有 ID 唯一，导航指向存在的主视图。
3. 断言 chat、logs、runs、settings、files、analysis、memory、status 视图存在。
4. 断言 logs/runs 导航带管理员权限标记，设置页带未接入保存的可见提示。
5. 断言 `app.js` 包含 Runs API、Run Detail API 和受控视图切换入口。

**验证：** 运行 `pytest -q tests/test_web_console_frontend.py`；在页面结构尚未改动时预期新增契约失败，以证明测试能识别缺口。

## T2: 重组全局控制台外壳

**文件：** `web/static/index.html`

**依赖：** T1

**步骤：**

1. 将侧栏改为品牌区、垂直主导航、会话区和账号区。
2. 为 chat、logs、runs、settings、files、analysis、memory、status 建立唯一的导航目标。
3. 保留现有会话列表、搜索、新会话、退出、移动遮罩和侧栏折叠元素 ID。
4. 为 logs/runs 导航加管理员权限标记。
5. 为每个主视图提供一致的移动端侧栏打开按钮与页面标题结构。

**验证：** 运行前端静态契约测试；预期 ID 唯一且所有导航目标均存在。

## T3: 建立日志、Runs/Trace 与设置页面骨架

**文件：** `web/static/index.html`

**依赖：** T2

**步骤：**

1. 添加日志页统计、Run 选择、关键词、级别、刷新、表头、内容区和状态提示。
2. 添加 Runs 页筛选、列表、指标、请求摘要、步骤时间线、Run State 与最终结果容器。
3. 添加设置页 Provider/API、模型路由、上下文、记忆/RAG、反思、Subagent 分组。
4. 将设置提交按钮设为禁用或预览行为，并在页面中明确显示未接入保存。
5. 所有表单元素补齐 `label`、`aria-label` 或可关联文本。

**验证：** 运行静态契约测试，并用 HTML Parser 确认不存在重复 ID。

## T4: 将文件、分析、记忆和状态整理为主视图

**文件：** `web/static/index.html`

**依赖：** T2

**步骤：**

1. 保留文件页现有按钮、上传输入、面包屑、列表和预览弹窗 ID。
2. 保留分析页输入、提交、结果和下载链接 ID。
3. 将记忆选择、刷新与内容容器移动到 Memory 主视图且不复制 ID。
4. 将用户、会话、模式、路径与 Workspace 表单移动到 Status 主视图。
5. 添加 Runtime 健康和最新 Run 概览容器。

**验证：** 运行静态契约测试，并搜索确认现有 JavaScript 引用的关键 ID 仍各存在一次。

## T5: 扩展状态与 DOM 引用

**文件：** `web/static/app.js`

**依赖：** T3、T4

**步骤：**

1. 删除旧 sidebar panel 状态依赖，保留侧栏折叠和移动抽屉状态。
2. 增加 Runs、Run Detail 缓存、筛选、加载、错误和 Runtime 健康状态。
3. 收集新增日志、Runs、Trace、设置与状态 DOM 引用。
4. 为可选管理员元素和新视图提供空值防护。
5. 保留现有聊天、文件、分析、记忆、Workspace 引用名称，减少回归面。

**验证：** 运行 `node --check web/static/app.js`，预期无语法错误。

## T6: 重构导航、权限与按需加载

**文件：** `web/static/app.js`

**依赖：** T5

**步骤：**

1. 实现允许视图白名单和管理员视图集合。
2. 改造 `setMainView`，拒绝未知或无权限视图并回退聊天页。
3. 实现 `applyRoleVisibility`，隐藏当前角色无权访问的导航项。
4. 实现 `loadViewData`，仅在进入 files、memory、status、logs、runs 时加载对应数据。
5. 更新初始化与导航事件绑定，移除旧 sidebar panel 切换调用。

**验证：** 运行 Node 语法检查与静态契约测试；普通用户路径的单元断言应回退 chat。

## T7: 实现 Run 数据控制器

**文件：** `web/static/app.js`

**依赖：** T6

**步骤：**

1. 实现 `/api/runs` 列表加载、强制刷新、加载态和错误态。
2. 实现 Run 状态筛选与列表渲染。
3. 实现默认选择最新 Run，并保持有效的当前选择。
4. 实现 `/api/run?run_id=...` 详情加载和页面生命周期缓存。
5. 让日志与 Runs 页面共享当前 Run 和刷新结果。

**验证：** 运行 Node 语法检查；通过替换 `fetchJson` 的最小 Node/DOM 测试或手工浏览器请求确认列表和详情各按预期请求一次。

## T8: 实现日志投影与筛选

**文件：** `web/static/app.js`

**依赖：** T7

**步骤：**

1. 实现 Trace 事件级别推导，覆盖失败、拒绝、重试、截断、压缩与普通事件。
2. 从已有 preview、error、status、tool/model 字段生成长度受限摘要。
3. 实现 Run、关键词和级别筛选。
4. 渲染统计卡、日志行、空态、错误态及可展开 JSON Payload。
5. 所有动态文本使用 `textContent`，不拼接不可信 HTML。

**验证：** 运行 Node 语法检查；用成功、警告、失败和未知事件样例检查分类与摘要不抛错。

## T9: 实现 Runs 列表与 Trace 详情

**文件：** `web/static/app.js`

**依赖：** T7

**步骤：**

1. 渲染 Run 列表、状态标记、调用计数和选择态。
2. 渲染真实 metrics 指标卡，缺失指标显示“不适用”。
3. 从 inbound 或 run state 提取用户请求摘要。
4. 按 step/span 整理并渲染模型、工具、其他事件和 Subagent 时间线。
5. 渲染错误/停止原因、Run State、最终结果和无法归属的 Run 事件。
6. 超长 Payload 使用折叠详情和内部滚动，不截断原始详情对象。

**验证：** 使用 `.runs` 中 completed、failed/stopped、带工具调用和缺字段样例进行页面检查；Node 语法检查通过。

## T10: 实现 Runtime 状态与设置预览交互

**文件：** `web/static/app.js`

**依赖：** T6、T7

**步骤：**

1. 调用 `/api/runtime-health` 并区分 ready、error、unknown。
2. 状态页汇总现有用户、角色、会话、模式、路径、Workspace 与最新 Run 数据。
3. 健康请求失败时保留明确错误态，不覆盖其他页面全局状态。
4. 初始化设置页预览控件，不填充任何真实密钥。
5. 拦截设置表单提交并显示未接入提示，不调用 API 或写 `localStorage`。

**验证：** Node 语法检查通过；检查设置提交不会触发 `fetch`，健康失败时显示异常而非正常。

## T11: 应用全局视觉系统与桌面布局

**文件：** `web/static/styles.css`

**依赖：** T2、T3、T4

**步骤：**

1. 调整深色背景、表面层、边框、文字、状态色、阴影和尺寸令牌。
2. 实现设计图方向的垂直导航、品牌、会话卡片、账号区和主 Workspace。
3. 统一页头、按钮、输入框、卡片、徽章、空态、错误态和滚动条。
4. 保持聊天消息、Composer、Markdown 与活动时间线功能样式兼容。
5. 为折叠侧栏提供仅图标或紧凑状态，不遮挡 Workspace。

**验证：** 在 1440×900 视口检查控制台外壳、聊天页和导航层级接近设计图，且无横向溢出。

## T12: 完成日志、Trace、设置和工具页样式

**文件：** `web/static/styles.css`

**依赖：** T8、T9、T10、T11

**步骤：**

1. 实现日志统计、筛选栏、表格行、级别徽章和 Payload 展开样式。
2. 实现 Runs 列表、指标条、Trace 步骤轴、事件 Chip、Subagent 和 Run State 样式。
3. 实现设置双栏/卡片布局、开关、禁用保存和预览提示。
4. 统一文件、分析、记忆和状态卡片的间距、层级及滚动行为。
5. 对长 Run ID、事件名、路径、JSON 和表格提供换行或内部滚动策略。

**验证：** 在 1440×900 视口依次检查八个主视图，确认布局稳定且主要信息可读。

## T13: 完成移动端与可访问性适配

**文件：** `web/static/index.html`、`web/static/styles.css`、`web/static/app.js`

**依赖：** T11、T12

**步骤：**

1. 在 860px 以下把侧栏切换为可关闭抽屉并显示遮罩。
2. 将日志表格、Runs 列表/详情和设置网格调整为单列或可控横向滚动。
3. 确保所有主视图都有移动导航入口，Escape 可关闭抽屉和弹窗。
4. 补齐可见焦点、按钮标题、状态区 `aria-live` 和表单标签。
5. 检查键盘导航顺序，不让隐藏视图中的元素获得焦点。

**验证：** 在 390×844 和 860px 断点检查导航、页面切换、筛选、折叠详情和表单；键盘 Tab 路径可用。

## T14: 运行回归与整理证据

**文件：** 全部本次修改文件

**依赖：** T1–T13

**步骤：**

1. 运行 JavaScript 语法检查和前端静态契约测试。
2. 运行现有 Web 登录、会话、流式输出及 Trace 相关测试。
3. 启动本地 Web 服务，对静态资源和可用 API 做 HTTP 冒烟检查。
4. 按 `checklist.md` 检查桌面、移动、管理员、普通用户、空数据和错误数据场景。
5. 记录实际命令输出与未覆盖项；若失败则修复并重跑。

**验证：** `checklist.md` 全部条目通过，并形成带实际证据的验收报告。

## 执行顺序

```text
T1 -> T2 -> T3 -----> T5 -> T6 -> T7 -> T8 ----\
       \-> T4 ------/             \-> T9 ----+-> T12 -> T13 -> T14
                                      \-> T10 -/    ^
T2 + T3 + T4 -----------------------------> T11 ---/
```
