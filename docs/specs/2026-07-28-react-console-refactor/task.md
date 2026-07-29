# TaleClaw Web 控制台 React 重构 Tasks

## 文件清单

| 操作 | 文件/目录 | 职责 |
|---|---|---|
| 新建 | `web/frontend/package.json`、`package-lock.json` | React 工程依赖与脚本 |
| 新建 | `web/frontend/tsconfig*.json`、`vite.config.ts`、`eslint.config.js` | TypeScript、构建、测试与 Lint 配置 |
| 新建 | `web/frontend/index.html`、`src/main.tsx` | React 开发入口 |
| 新建 | `web/frontend/src/app/` | Bootstrap、Shell、路由与 Context |
| 新建 | `web/frontend/src/api/` | 类型化 API Client、DTO 与 Adapter |
| 新建 | `web/frontend/src/hooks/` | 领域资源与交互 Hooks |
| 新建 | `web/frontend/src/pages/` | 八个主视图 |
| 新建 | `web/frontend/src/components/` | 布局、聊天、Trace 与共享 UI |
| 新建 | `web/frontend/src/styles/` | 设计令牌、基础、布局、组件、页面和响应式样式 |
| 新建 | `web/frontend/src/test/` | Vitest/Testing Library 测试与初始化 |
| 生成 | `web/static/app/` | Vite 生产构建产物 |
| 修改 | `web/server.py` | 根入口与 SPA fallback 指向 React 构建 |
| 删除 | `web/static/index.html`、`app.js`、`styles.css` | 移除旧控制台生产入口 |
| 新建 | `tests/test_web_react_frontend.py` | Python 静态资源与服务器集成测试 |
| 删除/替换 | `tests/test_web_console_frontend.py` | 移除旧原生 DOM 契约测试 |

## T1: 建立 React/Vite 工程配置

**文件：** `web/frontend/package.json`、TypeScript/Vite/ESLint 配置、`index.html`

**依赖：** 无

**步骤：**

1. 定义 `dev`、`typecheck`、`lint`、`test`、`build` 脚本。
2. 加入 React、TypeScript、Vite、React Markdown、GFM、Vitest、Testing Library、jsdom 和 ESLint 依赖。
3. 开启 TypeScript strict、DOM 类型、无未使用变量检查和 React JSX。
4. 配置 Vite 开发代理 `/api` 到本地 Python Server。
5. 配置生产 base `/static/app/`、输出目录 `web/static/app/` 和安全清理范围。

**验证：** 在 `web/frontend` 执行 `npm install` 和 `npm run typecheck`，空工程配置可解析。

## T2: 建立 React 测试入口与最小应用

**文件：** `src/main.tsx`、`src/app/App.tsx`、`src/test/setup.ts`、`src/test/app.test.tsx`

**依赖：** T1

**步骤：**

1. 建立 `#root` React 挂载入口。
2. 创建最小 App 骨架和加载占位。
3. 配置 jsdom、Testing Library cleanup 与 matcher。
4. 写首个渲染测试，断言应用品牌和加载状态。

**验证：** `npm test -- --run` 通过，`npm run build` 生成隔离目录。

## T3: 建立 Python 静态集成失败测试

**文件：** `tests/test_web_react_frontend.py`

**依赖：** T2

**步骤：**

1. 断言 React 构建目录含 `index.html` 和哈希 JS/CSS 资源。
2. 断言入口资源路径使用 `/static/app/`。
3. 断言 Python `_static_target("/")` 与无扩展名 fallback 指向 React index。
4. 断言登录页仍指向原生登录资源。
5. 断言旧控制台 `app.js/styles.css` 不再是生产依赖。

**验证：** 在修改 Server 前运行测试，预期入口断言失败，证明测试捕获迁移缺口。

## T4: 定义 API DTO 与领域类型

**文件：** `src/api/types.ts`

**依赖：** T1

**步骤：**

1. 定义用户、健康、会话、消息、记忆、文件和分析响应类型。
2. 定义 Run Summary、Run Detail、Trace Event 和 Subagent 类型。
3. 定义聊天请求、停止响应和流式事件联合类型。
4. 对后端不稳定 payload 使用 `unknown` 与安全 Record 类型，不使用无约束 `any`。

**验证：** `npm run typecheck` 通过。

## T5: 实现并测试数据 Adapter

**文件：** `src/api/adapters.ts`、`src/test/adapters.test.ts`

**依赖：** T4

**步骤：**

1. 实现 Run 列表和 Detail 的安全字段归一化。
2. 实现 Trace Event、Subagent、日志级别、日志摘要和 Step 分组纯函数。
3. 对未知、缺失、错误类型和超长字段提供默认值与长度限制。
4. 使用恶意 HTML 字符串 fixture 验证 Adapter 不生成 HTML。

**验证：** Adapter 测试覆盖 completed、failed/stopped、未知 Event、缺字段和恶意文本并通过。

## T6: 实现类型化 API Client

**文件：** `src/api/client.ts`、`src/test/client.test.ts`

**依赖：** T4

**步骤：**

1. 实现 JSON GET/POST/DELETE 与错误消息解析。
2. 实现 401 登录跳转逻辑。
3. 实现 FormData 上传且不错误设置 JSON Content-Type。
4. 实现可取消 NDJSON 流读取，处理跨 chunk 行和尾部残行。
5. 测试成功、非 JSON 错误、401、流拆包和取消。

**验证：** Client 单元测试和 TypeScript 检查通过。

## T7: 实现 Hash 路由与权限

**文件：** `src/app/routing.ts`、`src/hooks/useAppView.ts`、`src/test/routing.test.ts`

**依赖：** T4

**步骤：**

1. 定义八视图白名单和管理员视图集合。
2. 实现 Hash 解析、首次本地偏好恢复和导航。
3. 实现未知视图与无权限视图回退 Chat。
4. 监听 `hashchange` 并同步 React 状态与合法本地偏好。
5. 测试管理员、普通用户、未知 Hash 和偏好恢复。

**验证：** 路由测试与 Lint 通过。

## T8: 建立全局 Context 与资源状态工具

**文件：** `src/app/contexts.tsx`、`src/hooks/useAsyncResource.ts`

**依赖：** T6

**步骤：**

1. 建立 User/Workspace Context 及严格消费 Hook。
2. 实现统一 AsyncState、加载、成功、错误和刷新工具。
3. 避免组件卸载后写状态，支持 AbortController。
4. 写 Hook 测试覆盖成功、错误和重新加载。

**验证：** Hook 测试、TypeScript 与 Lint 通过。

## T9: 建立共享 UI 与动效基础

**文件：** `src/components/ui/`、`src/styles/tokens.css`、`base.css`、`components.css`

**依赖：** T2

**步骤：**

1. 实现 Button、Badge、Card、Field、Select、PageHeader。
2. 实现 Skeleton、EmptyState、ErrorState、Disclosure 和 Modal。
3. 为交互组件添加语义、可见焦点和键盘行为。
4. 定义颜色、间距、圆角、阴影、时间和缓动令牌。
5. 添加 `prefers-reduced-motion` 全局覆盖。

**验证：** 组件测试通过；减少动态效果媒体查询存在且覆盖动画时长。

## T10: 实现 AppBootstrap 与应用外壳

**文件：** `src/app/AppBootstrap.tsx`、`AppShell.tsx`、`components/layout/`

**依赖：** T7、T8、T9

**步骤：**

1. 加载 `/api/health` 并建立用户/Workspace Context。
2. 实现首屏骨架、错误态和 401 行为。
3. 实现桌面侧栏、折叠、移动抽屉、遮罩和账号退出。
4. 实现角色感知导航与当前页面过渡状态。
5. 使用 `React.lazy`/`Suspense` 建立八视图 Outlet。

**验证：** App 测试覆盖加载、管理员导航、普通用户隐藏、移动抽屉和视图切换。

## T11: 实现会话资源与会话面板

**文件：** `src/hooks/useSessions.ts`、`components/layout/ConversationPanel.tsx`

**依赖：** T6、T10

**步骤：**

1. 加载、搜索和选择会话。
2. 实现新会话本地初始化与当前会话刷新。
3. 实现删除确认、忙碌禁用及删除后选择回退。
4. 区分 Web 可写会话与 Raw 只读会话。
5. 测试搜索、选择、删除和空态。

**验证：** 会话组件测试通过，现有 Python 会话测试通过。

## T12: 实现安全 Markdown 与聊天消息

**文件：** `components/chat/SafeMarkdown.tsx`、`MessageList.tsx`

**依赖：** T9、T11

**步骤：**

1. 使用 React Markdown + GFM 渲染标题、列表、表格、引用和代码块。
2. 禁止原始 HTML，限制链接协议并为外链增加安全属性。
3. 实现代码复制与反馈状态。
4. 渲染 user/assistant/tool/system 的不同消息外观与只读提示。
5. 测试恶意 HTML、危险链接、表格和代码复制。

**验证：** Markdown 组件测试通过，不出现 `dangerouslySetInnerHTML`。

## T13: 实现聊天流、活动时间线和 Composer

**文件：** `src/hooks/useChatStream.ts`、`components/chat/ActivityTimeline.tsx`、`Composer.tsx`、`pages/ChatPage.tsx`

**依赖：** T6、T11、T12

**步骤：**

1. 实现发送、增量文本、Trace activity、完成、错误和停止状态机。
2. 将 Step、Tool、Subagent、Workspace Diff 和 Run Link 投影为活动模型。
3. 实现 Composer 自适应高度、Enter/Shift+Enter、发送/停止和模式按钮。
4. 完成后刷新会话内容与列表，Run Link 导向 React Runs 页面。
5. 测试流事件序列、停止、错误和 Activity 展开。

**验证：** 聊天 React 测试与现有 Python streaming/mode tests 通过。

## T14: 实现 RunsProvider

**文件：** `src/hooks/useRuns.tsx`、`src/test/runs.test.tsx`

**依赖：** T5、T6、T8

**步骤：**

1. 加载 `/api/runs` 并默认选择最新 Run。
2. 按 Run ID 加载 `/api/run` 并缓存 Detail。
3. 支持选择、列表刷新、当前 Detail 刷新和缓存失效。
4. 仅在管理员 App 内挂载并请求。
5. 测试共享缓存、强制刷新、空列表和错误。

**验证：** RunsProvider 测试通过，重复页面访问不产生重复 Detail 请求。

## T15: 实现日志页

**文件：** `src/pages/LogsPage.tsx`、`components/trace/LogTable.tsx`

**依赖：** T5、T9、T14

**步骤：**

1. 实现 Run、关键词和级别筛选的受控状态。
2. 使用 memoization 派生统计与可见日志行。
3. 实现骨架、空态、错误态、刷新和 Payload Disclosure。
4. 为筛选结果变化加入轻量淡入，不影响表格阅读。
5. 测试组合筛选、未知 Event、长来源与 Payload 文本安全。

**验证：** 日志页面测试与真实 Runs API 浏览器检查通过。

## T16: 实现 Runs/Trace 页

**文件：** `src/pages/RunsPage.tsx`、`components/trace/`

**依赖：** T5、T9、T14

**步骤：**

1. 实现状态筛选与 Run 主列表选择过渡。
2. 实现 Header、Metrics、Request 和 Run Status。
3. 实现 Step Timeline、Event、Subagent、Run State 和 Final Answer。
4. 无法归属的事件进入 Run Events 区。
5. 处理缺字段、超长内容、空 Trace 和失败 Detail。
6. 测试 completed、stopped/failed、带工具/Subagent 和最小 fixture。

**验证：** Trace 组件测试与真实 `.runs` 浏览器检查通过。

## T17: 实现设置预览页

**文件：** `src/pages/SettingsPage.tsx`

**依赖：** T9

**步骤：**

1. 实现 Provider/API、路由、上下文、记忆/RAG、反思和 Subagent 卡片。
2. 表单状态仅保存在组件 state。
3. 提交显示未接入提示，不调用 API 或 localStorage。
4. 密钥输入不预填真实值。
5. 测试交互反馈和零配置请求。

**验证：** 设置页测试通过，`.env` 操作前后校验和一致。

## T18: 实现文件页

**文件：** `src/hooks/useFiles.ts`、`src/pages/FilesPage.tsx`、文件组件

**依赖：** T6、T9

**步骤：**

1. 实现目录加载、面包屑和上级导航。
2. 实现上传、新建目录、重命名和删除确认。
3. 实现文本预览 Modal、下载和不可预览空态。
4. 对操作加载与错误提供局部反馈。
5. 测试导航、操作请求、预览和 Modal 键盘关闭。

**验证：** 文件组件测试与现有上传解析测试通过。

## T19: 实现分析与记忆页

**文件：** `src/pages/AnalysisPage.tsx`、`MemoryPage.tsx`、`src/hooks/useMemory.ts`

**依赖：** T6、T9

**步骤：**

1. 分析页实现提交、忙碌、回复、错误和下载链接。
2. 记忆 Hook 实现加载、刷新与当前文件回退。
3. 记忆页实现文件选择、内容、骨架、空态和错误态。
4. 测试分析成功/失败和记忆选择/空数据。

**验证：** 页面测试与现有 Python 分析/记忆相关测试通过。

## T20: 实现状态页

**文件：** `src/hooks/useRuntimeHealth.ts`、`src/pages/StatusPage.tsx`

**依赖：** T8、T14

**步骤：**

1. 按需加载 `/api/runtime-health`，区分检查中、就绪和异常。
2. 汇总用户、会话、模式、记忆、路径、Workspace 和 Run 概览。
3. 普通用户不请求 Runs 且显示无权限。
4. Workspace 表单继续使用既有本地偏好逻辑。
5. 测试健康成功/失败与角色差异。

**验证：** 状态页测试和浏览器错误态检查通过。

## T21: 完成视觉、响应式与页面动效

**文件：** `src/styles/layout.css`、`pages.css`、`responsive.css`

**依赖：** T10–T20

**步骤：**

1. 实现设计图方向的深色导航、卡片、表格、时间线和表单布局。
2. 加入页面淡入、列表选择、Disclosure、Modal、抽屉和骨架动画。
3. 在 reduced-motion 下关闭位移、闪烁和非必要过渡。
4. 适配 1440×900、860px 断点和 390×844。
5. 处理长 Run ID、路径、JSON、Markdown 表格与 Payload 滚动。

**验证：** 桌面和移动浏览器截图检查，页面级 scrollWidth 不超过 clientWidth。

## T22: 切换 Python 生产入口

**文件：** `web/server.py`、`tests/test_web_react_frontend.py`

**依赖：** T10–T21

**步骤：**

1. 根请求与 SPA fallback 指向 `static/app/index.html`。
2. 保持 `/static/app/assets/*`、登录资源和旧 `/runs` HTML 页面可访问。
3. 为哈希资源返回正确 MIME 类型。
4. 运行 T3 的失败测试并修到通过。

**验证：** Python 静态集成测试和 HTTP 冒烟通过。

## T23: 删除旧控制台入口并更新契约

**文件：** 删除旧 `web/static/index.html`、`app.js`、`styles.css` 和旧契约测试；更新文档/忽略规则（如需）

**依赖：** T22

**步骤：**

1. 确认 React 生产入口已通过测试后删除旧控制台资源。
2. 删除或替换只针对旧 DOM 的测试。
3. 保留 `login.html`、`login.js`、`auth.css` 与其他非控制台资源。
4. 搜索生产代码，确认没有旧资源引用。

**验证：** `rg` 无旧入口引用，Python Server 仍能加载首页和登录页。

## T24: 完整构建、回归与验收

**文件：** 全部本次修改文件、`checklist.md`

**依赖：** T1–T23

**步骤：**

1. 运行 npm TypeScript、Lint、React 测试和生产构建。
2. 运行 Python 静态集成及现有 Web/Trace 测试。
3. 启动 Python Server，用管理员和普通用户进行 HTTP/浏览器冒烟。
4. 检查八视图、真实 Run、聊天流 fixture、错误态、移动端与 reduced-motion。
5. 按 checklist 记录实际证据，修复失败并重跑。

**验证：** 所有 checklist 条目通过并形成验收报告。

## 执行顺序

```text
T1 -> T2 -> T3
 |     |
 |     +-> T9 ---------------------------> T17
 +-> T4 -> T5 -> T14 -> T15 -> T16 ------+
      \-> T6 -> T7 -> T8 -> T10 -> T11 -> T12 -> T13
                         \-> T18
                         \-> T19
                              T14 -> T20

T10–T20 -> T21 -> T22 -> T23 -> T24
```
