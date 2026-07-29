# TaleClaw Web 控制台前端改版 Plan

## 架构概览

本次继续使用现有单页应用结构：`index.html` 提供所有视图的语义化骨架，`styles.css` 提供统一控制台外壳及响应式布局，`app.js` 负责状态、API 调用、视图切换和 DOM 渲染。不会引入前端框架、打包器或新的后端配置接口。

主界面分为两层：左侧应用导航与会话区保持常驻，右侧 Workspace 根据 `mainView` 切换聊天、日志、Runs、设置、文件、分析、记忆和状态视图。管理员入口通过现有 `/api/health` 返回的用户角色控制可见性；API 仍执行最终权限校验。

日志页与 Runs/Trace 页共享 Run 数据控制器。Run 列表来自 `/api/runs`；选择某个 Run 后，详情来自 `/api/run?run_id=...`。日志页把该 Run 的 Trace event 投影为日志行；Trace 页把同一份 detail 投影为指标、步骤时间线和 Run State，避免建立第二套数据真源。

设置页仅包含静态前端表单原型。输入控件使用设计默认值或空值，不从 `.env` 获取数据；保存按钮禁用并显示“尚未接入配置保存”。

## 核心数据结构

### `state` 扩展

在现有前端状态对象中增加：

```javascript
{
  mainView: "chat",
  runs: [],
  activeRunId: "",
  runDetailCache: new Map(),
  runsBusy: false,
  runsError: "",
  runStatusFilter: "all",
  logQuery: "",
  logLevel: "all",
  logLiveRefresh: false,
  runtimeHealth: { status: "unknown", error: "" }
}
```

`runDetailCache` 仅存在于当前页面生命周期，不写入 `localStorage`。`mainView` 继续持久化，但恢复时必须经过角色和视图白名单校验。

### Run Summary

直接消费 `/api/runs` 的现有字段：

```javascript
{
  run_id,
  session_id,
  status,
  mode,
  started_at,
  finished_at,
  reasoning_steps,
  model_calls,
  tool_calls
}
```

### Run Detail

直接消费 `/api/run` 的现有顶层结构：

```javascript
{
  run_id,
  run_state,
  report,
  metrics,
  events,
  subagents
}
```

渲染函数必须把非对象 payload、缺失 metrics、未知 status 和缺失时间视为合法输入。

### Log Row

由 Trace event 在浏览器内派生，不写回后端：

```javascript
{
  timestamp,
  level,       // info | warn | error
  source,      // 原始 event 名或 tool/model 标识
  message,     // payload 中的安全摘要
  event,
  step,
  payload
}
```

级别映射规则：失败、error、denied 和停止异常为 `error`；retry、warning、truncated、compressed、guard 等风险事件为 `warn`；其他事件为 `info`。映射只影响展示，不改变事件语义。

### Trace Step View

按 `step` 和 `span_id` 对父 Run 事件进行展示分组：

```javascript
{
  number,
  startedAt,
  finishedAt,
  modelEvents: [],
  toolEvents: [],
  otherEvents: [],
  subagents: []
}
```

无法归属步骤的事件进入“Run 事件”区，不强行绑定到步骤。

## 核心接口

### 视图与权限

```javascript
setMainView(viewName)
applyRoleVisibility()
isViewAllowed(viewName)
loadViewData(viewName)
```

`setMainView` 只激活白名单且当前角色可访问的视图；否则回退到 `chat`。`loadViewData` 在首次进入或用户主动刷新时加载对应数据。

### Runs 与 Trace

```javascript
loadRuns({ force = false } = {})
selectRun(runId, { targetView } = {})
loadRunDetail(runId, { force = false } = {})
renderRuns()
renderRunDetail(detail)
groupTraceSteps(detail)
```

Run 列表只请求一次并可手动强制刷新。Run detail 按 `run_id` 缓存；强制刷新会替换缓存。选择 Run 可在日志和 Trace 两个视图间共享。

### 日志投影

```javascript
traceEventToLogRow(event)
inferLogLevel(event)
summarizeTraceEvent(event)
filteredLogRows(detail)
renderLogs(detail)
```

摘要优先使用既有 preview、error、status、tool/model 等字段，最后才对 payload 做长度受限的 JSON 摘要。所有内容通过 `textContent` 写入，禁止把 payload 当 HTML 注入。

### Runtime 状态

```javascript
loadRuntimeHealth()
renderStatusDashboard()
```

基础用户与 Workspace 信息继续来自 `/api/health`；Runtime 就绪状态来自 `/api/runtime-health`。若健康检查失败，状态显示为异常或不可用，不影响其他只读页面加载。

### 设置原型

```javascript
initializeSettingsPreview()
showSettingsPreviewNotice()
```

设置控件不调用 API，不使用 `localStorage`，提交事件只阻止默认行为并显示未接入提示。

## 模块设计

### 应用外壳与导航

**职责：** 提供品牌、主导航、会话列表、账号信息、桌面折叠与移动抽屉；驱动右侧主视图切换。

**依赖：** 当前用户角色、`mainView` 状态。

### 聊天视图

**职责：** 保留现有消息渲染、Markdown、流式 Trace activity、Composer、停止、模式切换和会话操作，仅调整 DOM 容器与视觉样式。

**依赖：** 现有聊天与会话 API；现有活动时间线函数。

### Run 数据控制器

**职责：** 统一请求、缓存和选择 Run 数据，同时向日志、Runs/Trace、状态概览提供数据。

**依赖：** `/api/runs`、`/api/run`、管理员角色。

### 日志视图

**职责：** 将选中 Run 的 Trace events 转为日志表格，提供 Run、级别和关键词筛选以及 payload 展开。

**依赖：** Run 数据控制器。

### Runs / Trace 视图

**职责：** 左侧/顶部展示可筛选 Run 列表，详情区展示指标卡、用户请求、步骤时间线、Subagent、Run State 与最终结果。

**依赖：** Run 数据控制器。

### 设置视图

**职责：** 展示 Provider/API、路由、上下文、记忆/RAG、反思和 Subagent 配置分组与禁用保存状态。

**依赖：** 无后端依赖。

### 文件、分析、记忆与状态视图

**职责：** 将现有功能迁移到统一主视图区；复用既有 API 和行为。状态视图额外消费 Runtime 健康与 Run Summary。

**依赖：** 现有文件、分析、记忆、健康及 Runs API。

## 模块交互

```text
页面初始化
  -> 读取 /api/health
  -> 应用角色可见性并校验保存的 mainView
  -> 并行加载会话、记忆、文件
  -> 进入当前主视图并按需加载数据

管理员进入 Logs 或 Runs
  -> loadRuns()
  -> 选择保存的 activeRunId，或默认最新 Run
  -> loadRunDetail(runId)
  -> Logs: Trace event -> Log Row -> 过滤 -> 表格
  -> Runs: Detail -> Metrics + Steps + Subagents + Run State

用户进入 Status
  -> /api/health + /api/runtime-health
  -> 管理员额外复用 Run Summary
  -> 渲染真实状态；失败项保留错误态

用户进入 Settings
  -> 渲染前端预览
  -> 尝试提交时显示“尚未接入保存”
  -> 不发生网络请求或持久化
```

## 文件组织

```text
web/static/
├── index.html      — 重组控制台外壳并新增 Logs、Runs/Trace、Settings、Memory、Status 主视图
├── app.js          — 扩展导航、权限、Run 数据、日志投影、Trace、状态与设置原型逻辑
└── styles.css      — 统一设计令牌、页面组件、日志表格、Trace 时间线和响应式样式

tests/
└── test_web_console_frontend.py
                    — 静态结构、权限标记、页面入口与关键前端契约回归测试

docs/specs/2026-07-28-web-console-refresh/
├── spec.md
├── plan.md
├── task.md
└── checklist.md
```

## 技术决策

| 决策点 | 选择 | 理由 |
|---|---|---|
| 前端技术 | 保留原生 HTML/CSS/JS | 与当前项目一致，避免引入构建与迁移风险 |
| 页面组织 | 单 HTML 多主视图 | 复用现有状态和 API 层，页面切换无需刷新 |
| 日志数据源 | 选中 Run 的 `events` | 当前没有独立系统日志 API，Trace 是可靠事实来源 |
| Run 详情 | 复用 `/api/runs` 与 `/api/run` | 避免修改后端契约和 Trace 工件 |
| Run 加载 | 列表按需加载、详情按 Run 缓存 | 避免日志页对全部 Run 发出 N+1 请求 |
| Trace 分组 | 浏览器端按 step/span 投影 | 不改变原始事件；未知事件仍可见 |
| 敏感配置 | 设置页不读取、不保存 | 满足本期仅搭页面的范围并避免泄密 |
| 权限 | 前端隐藏 + 后端继续校验 | 改善体验，但不把前端隐藏当安全边界 |
| 内容安全 | 动态内容统一使用 `textContent` | 防止 Trace payload 或文件名形成 HTML 注入 |
| 测试 | Node 语法检查 + Python 静态契约测试 + 现有 Web 测试 | 当前没有浏览器测试链，先覆盖结构与回归风险 |
