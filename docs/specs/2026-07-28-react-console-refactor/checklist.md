# TaleClaw Web 控制台 React 重构 Checklist

> 所有条目必须通过运行、测试或可观察行为验证；React 页面可用后才允许删除旧控制台入口。

## 完整迁移

- [ ] 首页由 React Root 挂载，八个主视图均由 React 组件渲染。（验证：React DevTools/DOM 中存在单一 Root，逐个切换页面）
- [ ] 生产入口不加载旧 `web/static/app.js` 或 `styles.css`。（验证：检查 Network、构建 index 和代码搜索）
- [ ] 旧控制台资源只在 React 构建、测试和服务器集成通过后删除。（验证：检查 Git 历史与任务执行记录）
- [ ] 登录页仍使用现有 `login.html`、`login.js` 和 `auth.css`，登录流程无变化。（验证：登录、退出和失效重定向）

## 构建与 Python 集成

- [ ] `npm install` 可从 lockfile 重现依赖。（验证：清洁依赖环境运行 `npm ci`）
- [ ] TypeScript strict 检查无错误。（验证：`npm run typecheck`）
- [ ] ESLint 无错误。（验证：`npm run lint`）
- [ ] React/Vitest 测试全部通过。（验证：`npm test -- --run`）
- [ ] Vite 生产构建成功并输出哈希 JS/CSS 到 `web/static/app/`。（验证：`npm run build` 并检查 manifest/index）
- [ ] Python Server 的 `/` 和无扩展名 SPA fallback 返回 React index。（验证：Python 集成测试和 HTTP 请求）
- [ ] `/static/app/assets/*` 返回正确 JavaScript/CSS MIME 类型。（验证：HTTP Header）
- [ ] `/login`、`/runs` 兼容 HTML 与 `/api/*` 路由没有被 SPA fallback 截获。（验证：分别请求并检查响应类型）

## 应用外壳与路由

- [ ] 桌面侧栏、会话区、账号区、折叠按钮和主内容区正常。（验证：桌面浏览器实际操作）
- [ ] 390px 移动端使用抽屉导航，按钮、遮罩和 Escape 均可关闭。（验证：移动视口实际操作）
- [ ] Hash 路由切换不发生整页刷新，刷新后恢复当前合法页面。（验证：观察 Network Document 请求并刷新）
- [ ] 未知 Hash、失效本地偏好和普通用户管理员 Hash 回退聊天页。（验证：设置三类地址/Storage 条件）
- [ ] 日志与 Runs 入口只对管理员显示；后端仍拒绝普通用户 API 请求。（验证：管理员/普通用户账号与直接 HTTP 请求）
- [ ] 页面按需加载，首次进入懒加载页面时显示布局稳定的骨架。（验证：禁用缓存后观察 Network 和 UI）

## 动效与可访问性

- [ ] 页面切换、侧栏抽屉、列表选择、Disclosure、Modal 和骨架具有轻量反馈。（验证：逐项操作并观察过渡）
- [ ] 动效不妨碍日志与 Trace 阅读，不造成明显布局跳动。（验证：Performance/Layout Shift 观察）
- [ ] `prefers-reduced-motion: reduce` 下非必要动画关闭或显著缩短。（验证：浏览器模拟媒体特性）
- [ ] 导航、筛选、列表、Disclosure、Modal 和表单可用键盘操作。（验证：Tab、Shift+Tab、Enter、Space、Escape）
- [ ] 所有交互有可见焦点，加载、错误和成功状态有可读文本且不只依赖颜色。（验证：键盘与屏幕阅读语义检查）

## 聊天与会话

- [ ] 会话列表可加载、搜索、选择、新建和删除，Raw 会话保持只读。（验证：管理员与普通 Web/Raw 会话实际操作）
- [ ] 聊天流逐步显示文本，不等待完整响应后一次出现。（验证：执行真实或 fixture NDJSON 流）
- [ ] Tool、Step、Subagent 和 Workspace Diff 活动随流更新并可展开。（验证：使用带工具与 Subagent 的流 fixture）
- [ ] 停止按钮发送停止请求并显示 stopping/完成反馈。（验证：长请求中点击停止）
- [ ] Hybrid、Chat 与管理员 Coding 模式保持现有权限和行为。（验证：逐个切换模式）
- [ ] Markdown 标题、列表、表格、引用、代码和复制按钮可用。（验证：Markdown fixture）
- [ ] 原始 HTML 和危险链接不会执行或生成危险 DOM。（验证：恶意 Markdown fixture）
- [ ] Coding Workspace 偏好保持现有本地恢复行为，普通用户不可使用管理员 Workspace。（验证：保存、刷新和角色切换）

## 日志与 Runs/Trace

- [ ] Logs 与 Runs 共用 Run Detail 缓存，切换页面不重复请求同一 Detail。（验证：观察 Network 请求次数）
- [ ] 日志页使用真实 Trace，Run、关键词与级别筛选可组合并即时更新。（验证：选择真实 Run 并组合筛选）
- [ ] 日志统计、空态、错误态、刷新和 Payload 展开正确。（验证：真实数据与空/失败 fixture）
- [ ] Runs 页展示状态筛选、Run 列表、指标、请求摘要和当前选择。（验证：对照 `/api/runs`、`/api/run`）
- [ ] Trace 展示 Step、模型/工具事件、Subagent、独立 Run Event、Run State 与最终结果。（验证：completed、failed/stopped、工具/Subagent Run）
- [ ] 未知事件、缺失字段、非对象 Payload 和超长内容不会导致 React Error Boundary。（验证：最小及异常 fixture）
- [ ] Trace 中恶意 HTML 只显示为文本，不执行脚本。（验证：注入 fixture 并检查 DOM）

## 设置、文件、分析、记忆与状态

- [ ] 设置页显示六类配置卡片和未接入保存提示。（验证：逐组观察与操作）
- [ ] 设置交互不调用配置 API、不写 `.env`、不持久化敏感字段。（验证：Network/Storage 与 `.env` 校验和）
- [ ] 文件页支持目录、面包屑、上级、上传、新建、重命名、删除、下载和预览 Modal。（验证：临时目录完整操作）
- [ ] 文件 Modal 支持关闭按钮、遮罩和 Escape，焦点不会遗留在隐藏内容。（验证：键盘操作）
- [ ] 分析页支持提交、忙碌、回复、错误状态和记录下载。（验证：成功与失败请求）
- [ ] 记忆页支持加载、选择、刷新、空态和错误态。（验证：真实响应与 fixture）
- [ ] 状态页显示用户、角色、会话、模式、健康、路径、Workspace 和 Run 概览。（验证：对照三个 API）
- [ ] Runtime 健康失败时只影响状态卡，不使其他页面不可用。（验证：模拟 503 后切换页面）

## 响应式与性能

- [ ] 1440×900 下八个页面无页面级横向滚动。（验证：每页比较 `scrollWidth` 与 `clientWidth`）
- [ ] 390×844 下八个页面可导航，表格、Trace、JSON 和长路径滚动受控。（验证：移动视口逐页检查）
- [ ] 长 Run ID、文件路径、Markdown 表格和 Payload 不撑破布局。（验证：超长 fixture）
- [ ] 首屏只加载 Shell 与当前页面代码，其他页面按需请求 chunk。（验证：清缓存后观察 Network）
- [ ] 页面切换和筛选无明显主线程长任务或持续重复请求。（验证：Performance 与 Network 观察）

## Python 回归与浏览器稳定性

- [ ] 现有 Web 认证、会话、标题、上传、流式输出、模式和 Trace 测试通过。（验证：运行相关 pytest 集合）
- [ ] Python 静态 React 集成测试通过。（验证：`pytest -q tests/test_web_react_frontend.py`）
- [ ] 管理员和普通用户的首页、登录、八视图及真实 Runs 冒烟通过。（验证：本地 Python Server + 浏览器）
- [ ] 初始化、八视图切换、真实 Run、错误 fixture 和移动端期间浏览器无未处理异常。（验证：收集 Console/Runtime exceptions）

## 端到端场景

- [ ] 场景 1：管理员登录 → 聊天流式请求 → 展开 Activity → 点击 Run → 查看 Trace → 切换 Logs 并筛选相同 Run。（预期：状态连续、Run ID 一致、无整页刷新）
- [ ] 场景 2：管理员操作文件、分析、记忆、状态和设置预览 → 刷新当前 Hash 页面。（预期：业务可用、合法页面恢复、设置不持久化）
- [ ] 场景 3：普通用户登录 → 使用允许页面 → 手工输入 `#/runs`。（预期：管理员入口隐藏、回退 Chat、API 权限不绕过）
- [ ] 场景 4：启用 reduced-motion + 390×844 → 打开抽屉 → 切换八视图 → 展开长 Trace Payload。（预期：动画克制、内容可读、页面无横向溢出）
- [ ] 场景 5：Runs 空列表、Detail 失败、Runtime 503、未知 Trace Event。（预期：局部空态/错误态，无 Error Boundary 崩溃，其他页面仍可用）

## 2026-07-28 实施验证记录

- `npm ci`：从 lockfile 成功安装 364 个包。
- `npm run typecheck`、`npm run lint`：通过，零 TypeScript/ESLint 错误。
- `npm test -- --run`：5 个测试文件、11 项 React 测试通过。
- `npm run build`：成功生成 `web/static/app/index.html`、favicon、哈希 CSS/JS 与八个懒加载页面 chunk。
- `python -m pytest -q`：638 项 Python 测试通过；仅有 2 条既有 protobuf 弃用警告。
- Python 静态集成：根入口与 SPA fallback 指向 React index，登录原生资源保留，JS/CSS GET 分别返回 `text/javascript` 和 `text/css`。
- Chrome 桌面与移动冒烟：八个 Hash 页面均挂载 React Root；1440×900 与 390×844 下 `documentElement.scrollWidth === clientWidth`。
- 真实 `.runs` 冒烟：Logs/Runs 成功读取现有 Run/Trace；同一 Detail 的并发请求由 Provider 合并为一次。
- 浏览器运行期：未观察到 React 未处理异常；补齐 favicon 后不再依赖不存在的 `/favicon.ico`。
- 设置页仍严格保持预览模式：未新增配置写 API，也未读取或修改 `.env`。
- 未在本轮自动执行会产生业务数据的真实聊天、文件写入/删除和分析提交；这些交互由现有 API 契约与组件测试覆盖，发布前可按上方场景做人工验收。
