# TaleClaw 控制台 Untitled 视觉与主题重构 Checklist

> 所有条目都必须通过运行命令、观察 DOM/网络、检查截图或完成用户操作来验证。只有在实现完成后逐项记录实际证据。

## 主题初始化与切换

- [ ] 无 `taleclawTheme` 时模拟浅色系统偏好打开应用，首帧根元素为 `data-theme="light"`，页面没有先暗后亮的可见闪烁。（验证：清空 Storage、模拟 `prefers-color-scheme: light`，观察 Performance 截图和 DOM）
- [ ] 无 `taleclawTheme` 时模拟暗色系统偏好打开应用，首帧根元素为 `data-theme="dark"`，浏览器 `theme-color` 与暗色画布一致。（验证：清空 Storage、模拟暗色媒体特性，读取 DOM 与 Meta）
- [ ] 点击主题控件后页面即时切换全局颜色，当前 Hash、当前页面组件和已加载业务数据保持不变。（验证：记录 Hash/页面标识，点击按钮后比较）
- [ ] 手动选择主题后刷新页面仍恢复该主题，且系统偏好变化不覆盖手动选择。（验证：写入 Storage、刷新、触发 MediaQuery change，观察根属性）
- [ ] 无效主题值、Storage 异常或 `matchMedia` 不可用时应用仍加载，并回退到可读主题。（验证：注入异常 fixture 后打开入口）
- [ ] 主题控件显示与当前主题匹配的太阳/月亮图标、可访问名称和 `title`；键盘聚焦后可用 Enter/Space 切换。（验证：DOM/屏幕阅读器语义检查与键盘操作）
- [ ] 主题偏好只写入一个主题标识，不写入消息、配置、Payload 或其他业务数据。（验证：比较 localStorage 变化和 Network/文件系统）

## 视觉语言与令牌

- [ ] 暗色 1440x900 截图呈现深墨蓝画布、约 260px 固定侧栏、紧凑顶栏、青绿色强调色、细描边和低圆角面板，与 `Untitled/` 的整体层级一致。（验证：截图对照 `Untitled/chat-page.png`、`config-page.png`、`log-viewer-page.png`）
- [ ] 浅色 1440x900 截图保留相同布局和强调色层级，画布、面板、输入、边框和正文对比度清晰，不是简单反转导致的灰度混乱。（验证：截图对照并检查关键文字/控件计算颜色）
- [ ] 消费端 CSS 不再包含 Hex、RGB/RGBA 或暗色专用固定前景/背景；主题颜色集中在令牌文件。（验证：`rg -n '#[0-9a-fA-F]{3,8}|rgba?\(' web/frontend/src/styles/{base,layout,components,pages,responsive}.css` 无结果）
- [ ] 普通面板、按钮、输入和页面区段圆角不超过 8px；胶囊、开关和圆形状态点是唯一例外。（验证：浏览器计算样式扫描）
- [ ] 页面不再使用正文径向渐变、卡片渐变、大范围玻璃模糊或强发光装饰；悬停不造成明显位移或布局跳动。（验证：CSS 搜索和交互观察）
- [ ] 成功、信息、警告、错误、禁用、悬停、选中和焦点状态在浅/暗两套主题中均有独立且可辨识的语义样式。（验证：构造状态 fixture，逐主题截图）

## 应用外壳与响应式

- [ ] 管理员桌面显示品牌、全部允许导航、会话列表、账户区、主题控件和退出控件；普通用户隐藏管理员入口。（验证：管理员/普通用户登录后检查 DOM 和可见导航）
- [ ] 侧栏折叠后保留图标导航、主题、退出和展开控制，折叠偏好刷新后恢复。（验证：点击折叠、刷新、检查可访问按钮）
- [ ] 移动视口 390x844 使用抽屉导航；打开按钮、遮罩和 Escape 均能关闭，主题控件在抽屉中可操作。（验证：移动浏览器逐项操作）
- [ ] Hash 导航切换八个视图不发生 Document 刷新，刷新合法 Hash 后恢复当前视图。（验证：Network Document 请求计数和刷新操作）
- [ ] 未知 Hash、无权限 Hash 和无效主视图偏好都回退 Chat，后端管理员 API 仍拒绝普通用户。（验证：手工输入 Hash、Storage fixture 和直接 HTTP 请求）
- [ ] 1440x900 与 390x844 下八个视图的 `document.documentElement.scrollWidth` 等于 `clientWidth`；必要的表格、Trace、JSON 和代码只在内部容器滚动。（验证：逐页执行浏览器脚本）
- [ ] 长 Run ID、文件路径、Markdown 表格、Payload 和代码不会遮挡相邻内容或撑破父容器。（验证：注入超长 fixture，观察布局和截图）

## 重点页面

- [ ] 聊天页有紧凑顶栏、居中阅读区、右侧用户气泡、带青绿色边线的助手面板和稳定底部 Composer。（验证：加载聊天 fixture，浅/暗桌面截图）
- [ ] 聊天模式切换、流式文本、停止、技术输出展开/收起、Markdown 和复制操作行为保持可用。（验证：现有流 fixture 与键盘操作）
- [ ] 日志页显示紧凑总计/错误/警告指标、Run/关键词/级别筛选和可展开 Payload；筛选结果即时更新。（验证：组合筛选真实或 fixture Trace）
- [ ] 日志时间、来源、Run ID 和 Payload 在两套主题中适合高密度扫描，错误/警告/信息状态不只依赖颜色。（验证：截图与无障碍文本检查）
- [ ] Runs 页显示紧凑运行摘要、指标带、Run 索引和纵向步骤时间线；模型、工具、Subagent、Run State 和最终结果均可读。（验证：completed、failed、stopped 和工具事件 fixture）
- [ ] Runs 列表选择与详情切换保持缓存连续性，不重复请求同一个 Run Detail。（验证：Network 请求计数和 Provider 测试）
- [ ] 设置页展示六组配置预览，面板和 Toggle 与参考图风格一致；提交仍显示未接入提示，不调用配置 API。（验证：操作表单、Network/Storage 和 `.env` 校验）
- [ ] 文件页、分析页、记忆页和状态页使用同一套面板、表单、表格、状态和等宽内容令牌，主要业务行为未回退。（验证：逐页操作现有 API fixture）

## 可访问性与动效

- [ ] 导航、主题、筛选、Disclosure、Modal、表单和主要动作均可通过 Tab、Shift+Tab、Enter、Space 和 Escape 完成。（验证：键盘完整走查）
- [ ] 所有图标按钮都有可访问名称和悬停提示，焦点环在浅/暗主题均清晰可见。（验证：DOM 断言和键盘截图）
- [ ] 加载、错误、成功、停止和空态有可读文本或语义状态，不只依赖颜色或动画。（验证：屏幕阅读器/DOM 文本检查）
- [ ] 模拟 `prefers-reduced-motion: reduce` 后，页面淡入、抽屉、Disclosure、骨架和主题过渡被关闭或显著缩短，阅读内容不跳动。（验证：浏览器媒体特性与 Performance 观察）
- [ ] Modal 打开时 Escape、遮罩和关闭按钮均有效，焦点不会遗留在隐藏内容中。（验证：键盘操作和 `document.activeElement`）

## 自动化、构建与 Python 回归

- [ ] `npm ci` 可从 lockfile 重建依赖，且只包含批准的新增图标依赖。（验证：清洁 `web/frontend` 依赖目录运行命令）
- [ ] `npm run typecheck` 通过且无 TypeScript strict 错误。（验证：命令退出码为 0）
- [ ] `npm run lint` 通过且无 ESLint 错误。（验证：命令退出码为 0）
- [ ] `npm test -- --run` 通过，包含主题、Shell、Markdown、Adapter、Client、路由和 Technical Output 测试。（验证：记录测试文件和断言数量）
- [ ] `npm run build` 成功，生产入口包含哈希 JS/CSS、主题预加载和正确 `/static/app/` 路径。（验证：检查 `web/static/app/index.html` 与资源文件）
- [ ] Python 静态 React 集成测试通过，根入口、SPA fallback、登录资源和静态 MIME 行为不回退。（验证：`pytest -q tests/test_web_react_frontend.py`）
- [ ] 现有相关 Web 认证、会话、标题、文件、流式输出、模式和 Trace Python 测试通过。（验证：运行相关 pytest 集合并记录结果）
- [ ] 构建产物不加载旧 `web/static/app.js` 或 `web/static/styles.css`，登录页仍使用原生资源。（验证：源码搜索和 Network）

## 浏览器观测

- [ ] 管理员在浅色和暗色桌面组合下完成八视图切换，页面内容非空，控制台无未处理异常。（验证：浏览器 Console 收集与 DOM 检查）
- [ ] 管理员在浅色和暗色 390x844 组合下打开抽屉、切换八视图、展开长 Payload，内容不被遮挡且无页面级横向溢出。（验证：移动浏览器操作和截图）
- [ ] 主题切换前后 API 请求、当前会话、当前 Run 和页面 Hash 不发生无关变化。（验证：Network HAR/请求计数与状态快照）
- [ ] 设置、状态和错误页的局部失败不会使 App Error Boundary 或其他视图崩溃。（验证：Runtime 503、空列表和异常 Trace fixture）

## 端到端场景

- [ ] 场景 1：管理员登录 -> 打开聊天 -> 发送或回放流式请求 -> 展开 Activity -> 点击 Run -> 查看 Trace -> 切到 Logs 并筛选同一 Run；预期 Run ID 连续、无整页刷新、主题保持一致。（验证：真实或 fixture API 全流程）
- [ ] 场景 2：首次系统暗色 -> 切换浅色 -> 刷新 -> 折叠侧栏 -> 进入设置 -> 返回聊天；预期主题和侧栏偏好恢复，Hash 与会话状态不丢失。（验证：浏览器完整操作）
- [ ] 场景 3：普通用户登录 -> 使用允许页面 -> 手工输入 `#/runs`；预期管理员入口隐藏、路由回退 Chat、直接 Runs API 请求仍被拒绝。（验证：普通用户浏览器和 HTTP 请求）
- [ ] 场景 4：启用 reduced-motion + 390x844 -> 打开抽屉 -> 切换暗/浅主题 -> 浏览 Logs 与 Runs -> 展开长 JSON；预期动画克制、内容可读、没有横向溢出或未处理异常。（验证：浏览器媒体特性、操作和截图）
- [ ] 场景 5：Runs 空列表、Detail 失败、Runtime 503、未知 Trace Event；预期各自显示局部空态/错误态，其他页面仍可导航，Error Boundary 不接管整个应用。（验证：异常 fixture 和页面切换）

## 实际验收记录（2026-07-29）

- 最终响应式加固：Runs 横向索引按钮裁切自身内容，标题行允许收缩，状态 Badge 保持固定宽度；Run 元数据使用省略显示，Subagent 长标题允许换行且不会挤压 Badge。
- 前端静态检查：`npm run typecheck` 与 `npm run lint` 均通过。
- 前端测试：`npm test -- --run` 通过，结果为 7 个测试文件、19 项测试全部通过，覆盖主题、Shell、Markdown、Technical Output、路由、Adapter 和 Client。
- 生产构建：`npm run build` 通过，共转换 2083 个模块；入口生成哈希 JS/CSS，并保留同步主题预加载脚本与 `/static/app/` 基础路径。
- Python 静态入口：`python -m pytest -q tests/test_web_react_frontend.py` 通过，结果为 4 passed。
- Web 回归：`python -m pytest -q tests/test_web_*.py` 在本地 PostgreSQL 测试环境中通过，结果为 51 passed；连同 `tests/test_mode_switch.py` 的组合回归为 55 passed。
- 浏览器验收：生产服务 `http://127.0.0.1:8765` 下完成 8 个视图 x 2 个主题 x 2 个视口，共 32 项；页面级横向溢出、主题错配、空页面、Error Boundary、Runs 索引内部越界和浏览器异常均为 0。
- 截图验收：生成 16 张重点页截图；桌面均为 1440x900，移动端均为 390x844，图片熵均大于 0。人工复核移动 Runs 浅色/暗色截图，Run ID 与状态 Badge 无重叠。
- 样式约束：消费端 `base.css`、`layout.css`、`components.css`、`pages.css`、`responsive.css` 的 Hex/RGB/RGBA 扫描为 0；生产入口未引用旧 `/static/app.js` 或 `/static/styles.css`。
- 扩展 Trace 回归在同一工作树中为 76 passed、1 failed。唯一失败是现有 Runtime 路径 `tests/test_run_trace.py::RunTraceTests::test_background_memory_lifecycle_does_not_block_pipeline_reply`，原因是 `memory/background_lifecycle.py` 对 `mappingproxy` 执行 `deepcopy` 时失败；该路径不属于本次前端改动，本次未修改或规避它。
