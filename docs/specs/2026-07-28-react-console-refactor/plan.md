# TaleClaw Web 控制台 React 重构 Plan

## 架构概览

新增独立的 `web/frontend/` React + TypeScript + Vite 工程，完整承载控制台应用。生产构建输出到隔离目录 `web/static/app/`，现有 `web/static/login.*` 与认证页面不参与 Vite 清理。Python Server 的根页面与无扩展名 SPA fallback 改为 `web/static/app/index.html`，`/static/app/assets/*` 继续使用现有静态文件处理逻辑。

这是一次性切换，不采用 React Islands。生产入口不再加载旧 `web/static/app.js` 与 `web/static/styles.css`；迁移完成后删除旧控制台入口和对应静态契约测试，只保留登录页原生资源与 React 生产构建。

应用内部使用 Hash 路由语义（`#/chat`、`#/runs` 等）而不依赖路由库，避免与 Python Server 已存在的 `/runs` 兼容 HTML 路由冲突。视图模块通过 `React.lazy` 动态导入；应用外壳、会话区和全局状态保持常驻。

数据访问分为三层：类型化 API Client 处理 HTTP、认证失效和 NDJSON；领域 Adapter 将后端宽松 JSON 转成稳定前端 View Model；React Hooks/Context 管理请求、缓存、刷新和跨页面共享状态。Runs 与日志共享 `RunsProvider`，不会重复建立数据真源。

动效不依赖动画库，使用 CSS transition、keyframes 和 React 状态类完成。所有动画受 `prefers-reduced-motion` 控制。加载反馈采用与最终布局一致的骨架组件，避免内容跳动。

## 核心数据结构

### 路由与用户

```ts
type AppView =
  | "chat"
  | "logs"
  | "runs"
  | "settings"
  | "files"
  | "analysis"
  | "memory"
  | "status";

type UserRole = "admin" | "user";

interface CurrentUser {
  id: string;
  role: UserRole;
}
```

`AppView` 是唯一视图白名单。`logs` 与 `runs` 属于管理员视图；未知 Hash、无权限 Hash 和失效持久值均归一化为 `chat`。

### API 状态

```ts
type AsyncStatus = "idle" | "loading" | "success" | "error";

interface AsyncState<T> {
  status: AsyncStatus;
  data: T | null;
  error: string;
  updatedAt: number | null;
}
```

所有页面使用相同加载、成功、错误模型，页面组件不直接推断裸 Promise 状态。

### Runs 与 Trace

```ts
interface RunSummary {
  runId: string;
  sessionId: string;
  status: string;
  mode: string;
  startedAt: string;
  finishedAt: string;
  reasoningSteps: number;
  modelCalls: number;
  toolCalls: number;
}

interface TraceEvent {
  timestamp: string;
  runId: string;
  event: string;
  sessionId: string;
  requestId: string;
  spanId: string;
  parentSpanId: string;
  step: number | null;
  payload: Record<string, unknown>;
}

interface RunDetail {
  runId: string;
  runState: Record<string, unknown>;
  report: Record<string, unknown>;
  metrics: Record<string, unknown>;
  events: TraceEvent[];
  subagents: SubagentSummary[];
}
```

Adapter 对缺失和类型错误字段提供安全默认值；原始 payload 保持结构但不执行。

### 日志与 Trace 视图模型

```ts
type LogLevel = "info" | "warn" | "error";

interface LogRow {
  id: string;
  timestamp: string;
  level: LogLevel;
  source: string;
  message: string;
  event: string;
  step: number | null;
  payload: Record<string, unknown>;
}

interface TraceStep {
  number: number;
  events: TraceEvent[];
}
```

日志和步骤均由纯函数从 `RunDetail` 派生，便于单元测试与 React memoization。

### 聊天流

```ts
type ChatStreamEvent =
  | { type: "delta"; text: string }
  | { type: "event"; event: string; [key: string]: unknown }
  | { type: "complete"; session?: SessionDto; [key: string]: unknown }
  | { type: "error"; error: string };

interface ActiveTurnState {
  status: "idle" | "streaming" | "stopping" | "error";
  text: string;
  activity: ActivityViewModel;
  error: string;
}
```

聊天流按事件逐行解码；组件只消费稳定的 Active Turn 状态，不直接操作 DOM。

## 核心接口

### API Client

```ts
getJson<T>(path: string, init?: RequestInit): Promise<T>
postJson<TBody, TResponse>(path: string, body: TBody): Promise<TResponse>
deleteJson<TBody, TResponse>(path: string, body: TBody): Promise<TResponse>
streamNdjson<TEvent>(path: string, body: unknown, onEvent: (event: TEvent) => void, signal?: AbortSignal): Promise<void>
```

401 统一跳转 `/login?next=...`；非 2xx 转为具有用户可读消息的 Error。文件上传使用独立 `uploadFormData`。

### 路由与权限

```ts
parseViewHash(hash: string, role: UserRole): AppView
navigate(view: AppView): void
isViewAllowed(view: AppView, role: UserRole): boolean
useAppView(role: UserRole): { view: AppView; navigate: (view: AppView) => void }
```

Hash 是导航事实来源，`localStorage.mainView` 仅作为首次无 Hash 时的恢复候选。

### Runs Context

```ts
interface RunsContextValue {
  runs: AsyncState<RunSummary[]>;
  activeRunId: string;
  activeDetail: AsyncState<RunDetail>;
  selectRun(runId: string): Promise<void>;
  refreshRuns(): Promise<void>;
  refreshActiveRun(): Promise<void>;
}
```

Context 内部维护 `Map<string, RunDetail>` 缓存；强制刷新只失效目标数据。

### 页面资源 Hook

```ts
useHealth(): HealthResource
useSessions(): SessionsResource
useMemory(): MemoryResource
useFiles(): FilesResource
useRuntimeHealth(enabled: boolean): RuntimeHealthResource
useChatStream(): ChatStreamController
```

每个 Hook 返回类型化数据、状态、错误、刷新和领域操作。页面不直接拼 URL 或处理响应字段兼容。

## 组件设计

### `AppBootstrap`

**职责：** 初始化 `/api/health`、处理未认证、提供 User/Workspace Context、显示首屏骨架。

**依赖：** API Client、`AppShell`。

### `AppShell`

**职责：** 组合 `Sidebar`、`ConversationPanel`、`AccountFooter`、`ViewOutlet` 和移动遮罩；维护侧栏折叠/抽屉状态。

**依赖：** User Context、Sessions Hook、路由 Hook。

### `ViewOutlet`

**职责：** 使用 `React.lazy` 与 `Suspense` 按视图加载页面；对切换中的页面添加淡入与骨架反馈。

**依赖：** 当前 `AppView`。

### `ChatPage`

**职责：** 组合 Header、MessageList、ActivityTimeline 和 Composer；通过 Chat Hook 驱动流式状态，使用安全 Markdown 组件渲染助手内容。

**依赖：** Sessions、Workspace、Chat Stream。

### `LogsPage`

**职责：** 组合指标、筛选栏、日志表格和 Payload Disclosure；日志行由 Adapter 派生并使用 memoization 过滤。

**依赖：** Runs Context。

### `RunsPage`

**职责：** 组合可筛选 Run Index、Trace Header、Metrics、Request、Timeline、Subagents、Run State 和 Final Answer。

**依赖：** Runs Context、Trace Adapter。

### `SettingsPage`

**职责：** 展示六类配置卡片与 Preview Notice；表单状态仅存在于组件生命周期，提交只显示未接入提示。

**依赖：** 无 API。

### `FilesPage`、`AnalysisPage`、`MemoryPage`、`StatusPage`

**职责：** 分别封装现有领域行为，使用共享 Page Header、Card、EmptyState、ErrorState、Skeleton 和 Disclosure 组件。

**依赖：** 对应资源 Hook；Status 额外依赖 Health 与 Runs Context。

### 共享 UI

`Button`、`IconButton`、`Badge`、`Card`、`PageHeader`、`Field`、`Select`、`Skeleton`、`EmptyState`、`ErrorState`、`Disclosure`、`Modal` 形成轻量组件层。组件只封装项目实际使用的变体，不建设通用组件库。

## 模块交互

```text
浏览器请求 /
  -> Python Server 返回 web/static/app/index.html
  -> React AppBootstrap 请求 /api/health
  -> 成功：建立 User/Workspace Context
  -> 解析 Hash + 权限 -> AppShell + lazy View
  -> 失败 401：跳转登录

进入 Logs / Runs
  -> RunsProvider 加载 /api/runs
  -> 默认选择最新 Run
  -> 按需加载 /api/run?run_id=...
  -> Adapter -> RunDetail
  -> Logs: deriveLogRows -> filters -> table
  -> Runs: groupTraceSteps -> timeline + detail sections

发送聊天
  -> Composer 调用 useChatStream.send
  -> streamNdjson 解码 delta/activity/complete/error
  -> React state 增量更新 Message + Activity
  -> complete 后刷新 session 与 session list

生产构建
  -> npm run build
  -> TypeScript + Vite 输出 web/static/app/
  -> Python 静态测试确认 index 与哈希 assets 可访问
```

## 文件组织

```text
web/
├── frontend/
│   ├── package.json
│   ├── package-lock.json
│   ├── tsconfig.json
│   ├── vite.config.ts
│   ├── eslint.config.js
│   ├── index.html
│   └── src/
│       ├── main.tsx
│       ├── app/
│       │   ├── App.tsx
│       │   ├── AppBootstrap.tsx
│       │   ├── AppShell.tsx
│       │   ├── routing.ts
│       │   └── contexts.tsx
│       ├── api/
│       │   ├── client.ts
│       │   ├── types.ts
│       │   └── adapters.ts
│       ├── hooks/
│       │   ├── useSessions.ts
│       │   ├── useChatStream.ts
│       │   ├── useFiles.ts
│       │   ├── useMemory.ts
│       │   └── useRuns.tsx
│       ├── pages/
│       │   ├── ChatPage.tsx
│       │   ├── LogsPage.tsx
│       │   ├── RunsPage.tsx
│       │   ├── SettingsPage.tsx
│       │   ├── FilesPage.tsx
│       │   ├── AnalysisPage.tsx
│       │   ├── MemoryPage.tsx
│       │   └── StatusPage.tsx
│       ├── components/
│       │   ├── layout/
│       │   ├── chat/
│       │   ├── trace/
│       │   └── ui/
│       ├── styles/
│       │   ├── tokens.css
│       │   ├── base.css
│       │   ├── layout.css
│       │   ├── components.css
│       │   ├── pages.css
│       │   └── responsive.css
│       └── test/
│           ├── setup.ts
│           ├── adapters.test.ts
│           ├── routing.test.ts
│           └── app.test.tsx
├── static/
│   ├── app/                 — Vite 生产构建产物
│   ├── login.html
│   ├── login.js
│   ├── auth.css
│   └── ...
└── server.py                — 根入口与 SPA fallback 指向 static/app/index.html

tests/
├── test_web_react_frontend.py
└── ... existing Python Web/Trace tests
```

## 技术决策

| 决策点 | 选择 | 理由 |
|---|---|---|
| 迁移方式 | 一次性完整 React 切换 | 避免原生与 React 双状态长期共存 |
| 语言 | TypeScript strict | 降低宽松 API 数据与 UI 状态错配风险 |
| 构建 | Vite，输出 `static/app/` | 构建快速，且隔离登录静态资源与清理范围 |
| 路由 | 自有 Hash 路由 | 避免新增路由依赖和 `/runs` 服务器兼容路由冲突 |
| 数据状态 | 类型化 Hooks + Context | 规模可控，避免为当前体量引入大型状态库 |
| Runs 缓存 | Context 内 Map | 日志和 Trace 共享详情，支持目标刷新 |
| Markdown | React Markdown + GFM 插件 | 不使用危险 HTML 注入并保留表格/代码体验 |
| 动效 | CSS transition/keyframes | 满足轻量反馈，不引入动画引擎 |
| 代码拆分 | 页面级 `React.lazy` | 控制首屏资源并保持实现简单 |
| 测试 | Vitest + Testing Library + jsdom | 覆盖纯 Adapter、路由、组件和关键交互 |
| 生产交付 | 提交 `static/app` 构建产物 | 保持现有 Python-only 启动方式可用 |
| 旧资源 | 删除旧控制台 index/app.js/styles.css | 确保生产入口完全使用 React；登录资源保留 |
