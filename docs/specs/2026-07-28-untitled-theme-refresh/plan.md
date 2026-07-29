# TaleClaw 控制台 Untitled 视觉与主题重构 Plan

## 架构概览

本次重构保留现有 React 页面、数据 Hooks、API Client、Hash 路由和 Python 静态资源集成，只调整应用外壳、共享 UI、页面呈现和主题基础设施。

主题分为两个相互衔接的阶段：HTML 头部的同步预加载脚本在 React 与构建样式执行前解析用户偏好，并把 `data-theme="light|dark"` 写到根元素，消除首次加载的错误主题闪烁；React 挂载后由独立主题 Hook 接管状态、系统偏好监听、手动切换、持久化及 `theme-color` 同步。

视觉层继续使用现有六个 CSS 文件，但改为严格的语义令牌架构。浅色令牌作为基础值，暗色选择器只覆写令牌值；布局、共享组件和页面样式不再直接假设暗色背景。样式重构按“令牌 -> 基础控件 -> 应用外壳 -> 页面”的依赖顺序完成。

图标统一使用 `lucide-react` 的树摇导入。导航、主题、侧栏、删除、刷新、上传等工具型控件使用熟悉图标；明确业务命令保留简短文字，并可组合图标。共享 `IconButton` 统一方形尺寸、可访问名称和原生悬停提示。

## 核心数据结构与接口

### 主题模型

```ts
export type Theme = "light" | "dark";

export interface ThemeController {
  theme: Theme;
  toggleTheme(): void;
}
```

主题持久化键固定为 `taleclawTheme`，只接受 `light` 和 `dark`。键不存在或值无效表示由系统管理；首次主题及系统偏好变化通过 `window.matchMedia("(prefers-color-scheme: dark)")` 解析。用户第一次手动切换后写入明确主题，后续系统变化不再覆盖该选择。

### 主题函数

```ts
export function readStoredTheme(storage?: Storage): Theme | null;
export function resolveTheme(stored: unknown, prefersDark: boolean): Theme;
export function applyTheme(theme: Theme, root?: HTMLElement): void;
export function useTheme(): ThemeController;
```

`applyTheme` 是唯一运行时 DOM 写入口：设置根元素 `data-theme`、`color-scheme` 和 `meta[name="theme-color"]`。函数在缺少 Storage、MediaQueryList 或 Meta 标签时安全降级。

### 图标按钮

```ts
interface IconButtonProps extends ButtonHTMLAttributes<HTMLButtonElement> {
  label: string;
  icon: LucideIcon;
  size?: "sm" | "md";
}
```

`IconButton` 始终提供 `aria-label` 与 `title`，图标设置 `aria-hidden`。方形尺寸固定，不因图标或状态变化造成布局位移。

### 导航描述

```ts
interface NavigationItem {
  view: AppView;
  label: string;
  icon: LucideIcon;
  adminOnly?: boolean;
}
```

现有字符数组替换为类型化导航描述，权限判断仍使用现有用户角色，导航行为仍由 `useAppView` 驱动。

## 模块设计

### HTML 主题预加载

**职责：** 在首屏样式绘制前决定主题，设置根元素属性，并给浏览器工具栏提供匹配的主题色。

**实现：** 在 Vite HTML 入口的 `<head>` 中加入小型同步脚本。脚本使用与运行时一致的存储键和值白名单，Storage 不可用时回退系统偏好。静态 `theme-color` 提供无脚本兜底，脚本会立即更新它。

**覆盖需求：** F7、F8、F9。

### 主题运行时

**职责：** 提供当前主题、手动切换和系统主题变化响应。

**实现：** 新建纯函数与 `useTheme` Hook。Hook 从预加载后的根属性初始化，只有在不存在有效手动偏好时订阅系统变化；切换时持久化明确值并调用 `applyTheme`。主题状态不进入业务 Context，也不触发页面重挂载。

**覆盖需求：** F6、F7、F8、F9。

### 设计令牌

**职责：** 为两个主题提供统一的颜色、尺寸、字体、阴影和交互状态。

**令牌分组：**

- 背景：画布、侧栏、基础表面、弱表面、浮层、控件、代码。
- 文本：主文本、次文本、弱文本、强调色上的文本。
- 交互：边框、强边框、悬停、选中、焦点环、遮罩。
- 品牌：青绿色强调、悬停色、柔和底色。
- 状态：成功、信息、警告、危险各自的前景、背景和边框。
- 反馈：弹层阴影、骨架基础色/高光、滚动条。
- 尺寸：260px 侧栏、4/6/8px 圆角、64px 标准页头和 24px 桌面页边距。
- 字体：界面无衬线字体与日志/Run/Payload 等宽字体。

暗色主题以参考图的深墨蓝表面和青绿色强调色为基准；浅色主题使用冷白画布、中性灰蓝层级与更深的青绿色，以保证强调控件和文字对比。移除正文径向渐变、卡片渐变、大范围玻璃模糊、发光装饰和悬停位移。

**覆盖需求：** F1-F5、F8、F10。

### 共享 UI

**职责：** 统一按钮、图标按钮、Badge、Card、PageHeader、输入、骨架、错误态、Disclosure、代码块、表格和 Modal。

**实现：** 新增 `IconButton`，现有 `Button` 保持 API 兼容。Card 最大圆角降为 8px，PageHeader 收紧到约 64px 并使用固定的紧凑标题层级。所有固定颜色替换为语义令牌；Badge 与日志级别使用完整状态令牌组。Modal 保留现有键盘关闭行为，只调整主题表面和遮罩。

**覆盖需求：** F3、F5、F6、F8。

### 应用外壳

**职责：** 复刻参考图的固定侧栏分区，并承载主题切换入口。

**实现：** 侧栏宽度收至 260px，品牌区、导航区、会话区和账户工具区使用细描边分隔。导航使用 Lucide 图标、弱表面选中态和青绿色图标，不再使用字符图标或左侧荧光条。账户区放置主题与退出工具；折叠后保留品牌、导航图标、主题、退出和展开控制。移动端沿用同一侧栏 DOM 作为抽屉，因此主题入口自然保持一致。

**覆盖需求：** F1、F6、F10。

### 聊天页

**职责：** 对齐参考图的紧凑顶栏、消息层级和底部输入体验。

**实现：** 保留当前流式状态、滚动锚点、模式逻辑和技术输出组件。通过少量语义结构与 CSS，将用户消息呈现为右侧弱表面气泡，将助手消息呈现为带青绿色左边线的低圆角面板；角色标识收进消息头，技术输出与 Markdown 使用主题化代码表面。Composer 降低圆角和阴影，保持稳定宽度与固定底部区域。

**覆盖需求：** F2、F8、F10。

### 日志页

**职责：** 对齐参考图的高密度日志扫描体验。

**实现：** 保留真实 Run、关键词和级别筛选逻辑。指标改为紧凑胶囊，筛选区与日志内容形成连续工具区；表头、行、时间、来源和 Payload 使用紧凑间距与等宽字体。小视口下表格只在自身容器滚动。

**覆盖需求：** F3、F8、F10。

### Runs / Trace 页

**职责：** 强化运行摘要、指标带和纵向执行时间线。

**实现：** 保留 Run 索引与共享缓存。收紧索引宽度、详情页头、指标和 Trace Section；步骤标记、连线和面板采用青绿色与细描边，模型、工具、警告和错误事件使用语义状态色。Run ID、指标、Payload 与最终结果使用等宽字体并限制局部溢出。

**覆盖需求：** F4、F8、F10。

### 设置及其余页面

**职责：** 把同一视觉语言扩展到设置、文件、分析、记忆和状态页面。

**实现：** 设置页保留六组预览配置，改为紧凑双栏低圆角面板和主题化 Toggle；文件页使用平面工具栏与列表行；分析页保留双栏工具面；记忆页保留主从视图并强化等宽内容；状态页移除渐变和发光装饰，改用清晰状态边框与指标块。页面数据、表单行为和 API 调用不变。

**覆盖需求：** F5、F8、F10。

### 测试与视觉验证

**职责：** 验证主题决策、持久化、首屏初始化、视觉完整性及无业务回归。

**实现：** 新增主题纯函数与组件测试，覆盖系统浅/暗、有效/无效存储、手动切换、刷新恢复和系统变化；扩展 App 测试验证可访问主题控件。生产构建测试检查预加载脚本和哈希资源。构建后启动 Python Server，用真实认证和现有 API 数据对八个视图进行桌面/移动、浅色/暗色浏览器冒烟，并保存关键页面截图进行视觉与像素非空检查。

**覆盖需求：** F6-F10 及全部验收标准。

## 模块交互

```text
浏览器解析 HTML
  -> 主题预加载脚本读取 taleclawTheme
  -> 有效手动值：应用 light/dark
  -> 无有效值：读取 prefers-color-scheme
  -> 写 html[data-theme] + meta theme-color
  -> CSS 按语义令牌完成首帧
  -> React 挂载
  -> useTheme 读取已应用主题
  -> AppShell / ThemeToggle 展示对应图标和名称

用户点击主题按钮
  -> useTheme 计算另一主题
  -> 写入 taleclawTheme
  -> applyTheme 更新根属性、color-scheme、theme-color
  -> 所有 CSS 令牌同步变化
  -> 当前 Hash、页面组件和业务状态保持不变

系统主题变化
  -> 没有手动偏好：useTheme 接收 media change 并应用
  -> 已有手动偏好：忽略系统变化
```

## 文件组织

```text
web/frontend/
├── index.html                         # 主题预加载与 theme-color
├── package.json                       # lucide-react 依赖
├── package-lock.json                  # 可重复依赖锁定
└── src/
    ├── app/
    │   ├── AppShell.tsx               # 图标导航、侧栏工具、ThemeToggle 接入
    │   └── theme.ts                   # 主题类型、纯函数、DOM 应用
    ├── hooks/
    │   └── useTheme.ts                # 系统监听、手动切换、持久化
    ├── components/
    │   ├── chat/                      # 必要的消息语义结构调整
    │   └── ui/
    │       ├── index.tsx              # IconButton 与共享组件标记
    │       └── ThemeToggle.tsx        # 太阳/月亮主题控件
    ├── pages/
    │   ├── ChatPage.tsx               # 消息头和重点布局钩子
    │   ├── LogsPage.tsx               # 日志页面样式钩子
    │   ├── RunsPage.tsx               # Trace 紧凑结构和图标动作
    │   ├── SettingsPage.tsx           # 统一图标和表单分组
    │   └── 其他页面                    # 仅必要的图标/样式钩子
    ├── styles/
    │   ├── tokens.css                 # 浅色基础、暗色覆盖、语义令牌
    │   ├── base.css                   # 全局表面、文字、输入、焦点
    │   ├── layout.css                 # 外壳、侧栏、紧凑页头
    │   ├── components.css             # 共享控件、表格、Modal、代码
    │   ├── pages.css                  # 八视图视觉重构
    │   └── responsive.css             # 移动抽屉与局部溢出
    └── test/
        ├── theme.test.tsx             # 主题纯函数与交互测试
        ├── app.test.tsx               # Shell 与主题入口集成
        └── setup.ts                   # matchMedia、根主题清理
tests/
└── test_web_react_frontend.py         # 生产入口预加载与构建集成
web/static/app/                         # 重新生成的生产资源
```

## 技术决策

| 决策点 | 选择 | 理由 |
|---|---|---|
| 主题表达 | `html[data-theme]` + CSS 语义令牌 | 主题全局一致，组件无需分支渲染，SSR 不在当前范围 |
| 初始主题 | HTML 同步预加载脚本 | 可以在 React 与样式绘制前应用持久偏好，避免主题闪烁 |
| 默认行为 | 无持久值时实时跟随系统；首次手动切换后固定 | 与已批准的用户行为一致，同时保持实现和界面简单 |
| 状态所有权 | 独立 `useTheme`，不加入业务 Context | 只有 Shell 需要控制入口，根属性已经向所有组件广播视觉状态 |
| 浅/暗令牌 | 浅色基础值，暗色选择器覆盖 | 无主题属性时有可读兜底，减少重复的组件规则 |
| 图标 | 新增 `lucide-react` 并树摇导入 | 提供一致、可访问的标准工具图标，替代风格混杂的字符符号 |
| 布局重构 | 保留页面 DOM 主结构，以 CSS 与少量语义标记调整 | 降低业务逻辑回归风险，同时足以实现参考图的视觉层级 |
| 视觉还原 | 复刻视觉系统，不逐像素复制示例数据 | 参考图分辨率和内容与真实应用不同，现有业务信息优先 |
| 动效 | 只保留淡入、Disclosure 与抽屉过渡，移除悬停位移 | 更符合运维工作台的稳定扫描需求，并减少布局扰动 |
| 验证 | 单测 + 构建 + Python 回归 + 四种主题/视口浏览器冒烟 | 同时覆盖状态逻辑、集成契约、响应式和真实视觉结果 |

## Spec 覆盖检查

| 需求 | 设计归属 |
|---|---|
| F1 | 应用外壳、设计令牌 |
| F2 | 聊天页、共享 UI |
| F3 | 日志页、状态令牌 |
| F4 | Runs / Trace 页、状态令牌 |
| F5 | 设置及其余页面、共享 UI |
| F6 | ThemeToggle、IconButton、应用外壳 |
| F7 | HTML 主题预加载、主题运行时 |
| F8 | 设计令牌、共享 UI、全部页面 |
| F9 | HTML 主题预加载、主题运行时 |
| F10 | 应用外壳、响应式样式、全部页面 |

