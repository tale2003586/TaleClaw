# TaleClaw 控制台 Untitled 视觉与主题重构 Tasks

## 文件清单

| 操作 | 文件 | 职责 |
|---|---|---|
| 修改 | `web/frontend/package.json`、`web/frontend/package-lock.json` | 锁定 Lucide React 图标依赖 |
| 修改 | `web/frontend/index.html` | 首屏主题预加载与浏览器主题色 |
| 新建 | `web/frontend/src/app/theme.ts` | 主题类型、解析、存储和 DOM 应用函数 |
| 新建 | `web/frontend/src/hooks/useTheme.ts` | 系统主题监听和手动主题状态 |
| 新建 | `web/frontend/src/components/ui/ThemeToggle.tsx` | 可访问的浅色/暗色切换控件 |
| 修改 | `web/frontend/src/components/ui/index.tsx` | IconButton 与共享 UI 语义标记 |
| 修改 | `web/frontend/src/app/AppShell.tsx` | 图标导航、主题入口、侧栏工具区 |
| 修改 | `web/frontend/src/components/chat/TechnicalOutput.tsx` | 主题化技术输出的图标与标记 |
| 修改 | `web/frontend/src/components/trace/TraceEventView.tsx` | Trace 事件视觉钩子与图标 |
| 修改 | `web/frontend/src/pages/ChatPage.tsx` | 消息身份、动作图标和布局钩子 |
| 修改 | `web/frontend/src/pages/LogsPage.tsx` | 紧凑日志区和动作图标 |
| 修改 | `web/frontend/src/pages/RunsPage.tsx` | 紧凑 Trace 布局和动作图标 |
| 修改 | `web/frontend/src/pages/SettingsPage.tsx` | 设置分组图标与视觉结构 |
| 修改 | `web/frontend/src/pages/FilesPage.tsx` | 文件工具图标和列表动作 |
| 修改 | `web/frontend/src/pages/AnalysisPage.tsx` | 分析工具动作图标 |
| 修改 | `web/frontend/src/pages/MemoryPage.tsx` | 记忆刷新动作图标 |
| 修改 | `web/frontend/src/pages/StatusPage.tsx` | 状态动作图标和状态结构 |
| 修改 | `web/frontend/src/styles/tokens.css` | 浅色基础、暗色覆盖和全部语义令牌 |
| 修改 | `web/frontend/src/styles/base.css` | 全局画布、文字、输入、焦点和滚动条 |
| 修改 | `web/frontend/src/styles/layout.css` | 侧栏、工作区、页头和折叠布局 |
| 修改 | `web/frontend/src/styles/components.css` | 按钮、面板、表格、代码和 Modal |
| 修改 | `web/frontend/src/styles/pages.css` | 八个主视图视觉重构 |
| 修改 | `web/frontend/src/styles/responsive.css` | 移动抽屉和页面局部溢出 |
| 新建 | `web/frontend/src/test/theme.test.tsx` | 主题解析、系统监听、切换和恢复测试 |
| 修改 | `web/frontend/src/test/app.test.tsx` | 应用外壳与主题入口集成测试 |
| 修改 | `web/frontend/src/test/setup.ts` | 测试间根主题、Meta 和媒体查询清理 |
| 修改 | `tests/test_web_react_frontend.py` | 生产入口主题预加载契约 |
| 生成 | `web/static/app/` | 更新后的 Vite 生产资源 |

## T1: 安装并锁定图标依赖

**文件：** `web/frontend/package.json`、`web/frontend/package-lock.json`

**依赖：** 无

**步骤：**

1. 使用 `--save-exact` 安装 `lucide-react`，避免浮动版本。
2. 确认只新增该运行时依赖，不升级现有 React、Vite 或测试包。
3. 检查 lockfile 中只出现 Lucide 及其必要元数据。

**验证：** 在 `web/frontend` 运行 `npm ls lucide-react`，期望只解析一个有效版本；运行 `git diff -- package.json package-lock.json`，确认没有无关依赖升级。

## T2: 实现主题核心函数与纯函数测试

**文件：** `web/frontend/src/app/theme.ts`、`web/frontend/src/test/theme.test.tsx`

**依赖：** 无

**步骤：**

1. 定义 `Theme`、`THEME_STORAGE_KEY`、浅色/暗色浏览器主题色和合法值判断。
2. 实现 `readStoredTheme`，只接受 `light`、`dark`，并在 Storage 抛错时返回 `null`。
3. 实现 `resolveTheme`，无合法存储时根据 `prefersDark` 返回主题。
4. 实现 `applyTheme`，同步根元素 `data-theme`、`color-scheme` 和 `meta[name="theme-color"]`。
5. 测试有效值、无效值、Storage 异常、系统浅/暗和 DOM 属性更新。

**验证：** 运行 `npm test -- --run src/test/theme.test.tsx`，主题核心测试全部通过。

## T3: 实现运行时 Hook 与主题切换控件

**文件：** `web/frontend/src/hooks/useTheme.ts`、`web/frontend/src/components/ui/ThemeToggle.tsx`、`web/frontend/src/test/theme.test.tsx`、`web/frontend/src/test/setup.ts`

**依赖：** T1、T2

**步骤：**

1. 实现 `useTheme`，从预加载后的根主题初始化状态。
2. 无手动偏好时订阅系统主题变化；组件卸载时移除监听。
3. 手动切换时写入 `taleclawTheme`、应用主题，并停止由系统偏好覆盖。
4. 实现使用 `Sun`/`Moon` 的 `ThemeToggle`，提供动态 `aria-label`、`title` 和固定图标尺寸。
5. 在测试初始化中提供可控 `matchMedia`，并在每项测试后清理根主题、Meta 和监听状态。
6. 增加系统变化、手动切换、持久恢复和可访问名称测试。

**验证：** 运行 `npm test -- --run src/test/theme.test.tsx`，系统管理和手动管理场景全部通过；运行 `npm run typecheck` 无错误。

## T4: 加入首屏主题预加载

**文件：** `web/frontend/index.html`、`tests/test_web_react_frontend.py`

**依赖：** T2

**步骤：**

1. 在 HTML `<head>` 中加入同步预加载脚本，使用相同的存储键和值白名单。
2. 在 Storage 或 `matchMedia` 不可用时安全回退，不阻断应用加载。
3. 在 React 与样式加载前设置根 `data-theme`、`color-scheme` 和 Meta 主题色。
4. 扩展 Python 构建契约测试，断言生产入口包含预加载逻辑且仍不引用旧控制台资源。

**验证：** 运行 `npm run build`，再运行 `pytest -q tests/test_web_react_frontend.py`；构建入口和主题预加载断言全部通过。

## T5: 建立浅色与暗色语义令牌

**文件：** `web/frontend/src/styles/tokens.css`

**依赖：** T2

**步骤：**

1. 以浅色为基础定义画布、侧栏、表面、控件、代码和浮层令牌。
2. 定义主/次/弱文本、边框、悬停、选中、焦点、遮罩和阴影令牌。
3. 定义青绿色品牌令牌及成功、信息、警告、危险的前景/背景/边框三元组。
4. 定义骨架、滚动条、字体、260px 侧栏、4/6/8px 圆角和标准页边距。
5. 在 `:root[data-theme="dark"]` 中只覆写主题值，暗色视觉对齐 `Untitled/` 的深墨蓝和青绿色。
6. 删除或重命名无语义、无调用的旧令牌。

**验证：** 运行 `rg -n '^\s*--' src/styles/tokens.css`，确认两套主题包含完整令牌组；运行 `npm run build`，CSS 构建成功。

## T6: 重构全局基础样式

**文件：** `web/frontend/src/styles/base.css`

**依赖：** T5

**步骤：**

1. 将正文、输入、选择、滚动条、选区和焦点样式全部切换为语义令牌。
2. 移除径向渐变、暗色输入硬编码和荧光黄绿焦点值。
3. 使用令牌化的无衬线与等宽字体，保持字号稳定且不随视口缩放。
4. 确保 `color-scheme`、占位文字、禁用状态和自动填充在两套主题下可读。

**验证：** 运行 `rg -n '#[0-9a-fA-F]{3,8}|rgba?\(' src/styles/base.css`，期望没有颜色字面量；运行 `npm run build` 成功。

## T7: 重构共享 UI 与图标按钮

**文件：** `web/frontend/src/components/ui/index.tsx`、`web/frontend/src/styles/components.css`

**依赖：** T1、T5、T6

**步骤：**

1. 实现 `IconButton`，统一 `aria-label`、`title`、图标尺寸和方形布局。
2. 保持 `Button`、Badge、Card、PageHeader、Skeleton、EmptyState、ErrorState、Disclosure 和 Modal 的现有调用接口。
3. 将 PageHeader 收紧为工作台层级，将 Card、Modal 和工具面板圆角限制在 8px 内。
4. 用语义令牌重写按钮、状态 Badge、骨架、错误态、代码、技术输出、筛选栏和表格样式。
5. 移除渐变卡片、大阴影、悬停位移和所有暗色专用颜色字面量。

**验证：** 运行 `npm run typecheck` 和 `npm test -- --run src/test/app.test.tsx src/test/technical-output.test.tsx`；运行颜色字面量扫描，`components.css` 中除无颜色关键字外不应有硬编码颜色。

## T8: 重构应用外壳结构与交互

**文件：** `web/frontend/src/app/AppShell.tsx`、`web/frontend/src/test/app.test.tsx`

**依赖：** T1、T3、T7

**步骤：**

1. 把字符导航配置替换为类型化 Lucide 导航描述，并保留管理员权限过滤。
2. 使用标准图标替换侧栏折叠、移动菜单、新会话、删除会话和退出工具字符。
3. 在账户工具区接入 `ThemeToggle`，确保折叠侧栏仍显示主题、退出和展开控件。
4. 保持侧栏折叠持久化、移动抽屉 Escape/遮罩关闭、会话选择和退出行为不变。
5. 扩展应用测试，断言管理员导航、主题控件、工具提示和折叠前后的可访问名称。

**验证：** 运行 `npm test -- --run src/test/app.test.tsx` 和 `npm run typecheck`，Shell 行为与主题入口测试通过。

## T9: 重构外壳、页头与移动抽屉样式

**文件：** `web/frontend/src/styles/layout.css`、`web/frontend/src/styles/responsive.css`

**依赖：** T5、T8

**步骤：**

1. 将桌面侧栏设为 260px，并用细边框分隔品牌、导航、会话和账户工具区。
2. 实现蓝灰弱表面导航选中态与青绿色图标，移除左侧荧光条和位移动效。
3. 将标准 PageHeader 压缩到约 64px，页面间距收敛到 20–24px。
4. 为折叠态定义稳定的 64–72px 图标轨道，工具按钮不被隐藏。
5. 保持 900px 以下抽屉布局，使用主题化遮罩和菜单按钮，修正安全区与层级。

**验证：** 运行 `npm run build`；使用 CSS 搜索确认 `layout.css`、`responsive.css` 没有颜色字面量，且存在折叠态与移动抽屉规则。

## T10: 重构聊天页视觉

**文件：** `web/frontend/src/pages/ChatPage.tsx`、`web/frontend/src/components/chat/TechnicalOutput.tsx`、`web/frontend/src/styles/pages.css`

**依赖：** T5、T7、T9

**步骤：**

1. 为消息加入可样式化的身份头，保留所有消息角色、流式内容和滚动锚点。
2. 将用户消息呈现为右侧弱表面气泡，助手消息呈现为带青绿色左边线的低圆角面板。
3. 用 Lucide 图标完善发送、停止、展开/收起和复制等工具动作，保留明确文字命令。
4. 收紧聊天顶栏、模式分段控件、技术输出、Markdown 代码面和 Composer。
5. 让长 Markdown、表格、代码和工具 Payload 只在自己的容器内换行或滚动。

**验证：** 运行 `npm test -- --run src/test/markdown.test.tsx src/test/technical-output.test.tsx src/test/app.test.tsx` 和 `npm run typecheck`，聊天安全渲染与 Shell 冒烟通过。

## T11: 重构日志页视觉

**文件：** `web/frontend/src/pages/LogsPage.tsx`、`web/frontend/src/styles/pages.css`

**依赖：** T5、T7、T9

**步骤：**

1. 增加日志页面专用类名，并为刷新动作加入标准图标。
2. 将总计、错误和警告改为紧凑指标胶囊。
3. 收紧 Run、关键词、级别筛选栏和日志表格间距。
4. 将时间、级别、来源和消息使用适合扫描的等宽层级与状态令牌。
5. 保持筛选、刷新、Payload Disclosure、加载、空态和错误逻辑不变。

**验证：** 运行 `npm run typecheck`、`npm test -- --run src/test/adapters.test.ts` 和 `npm run build`；日志 Adapter 与页面编译通过。

## T12: 重构 Runs / Trace 视觉

**文件：** `web/frontend/src/pages/RunsPage.tsx`、`web/frontend/src/components/trace/TraceEventView.tsx`、`web/frontend/src/styles/pages.css`

**依赖：** T5、T7、T9

**步骤：**

1. 为刷新、筛选和事件类型加入一致图标，保持状态筛选与共享缓存行为。
2. 收紧 Run 索引宽度、详情页头和单行指标带。
3. 使用青绿色步骤圆点、细连线和低圆角步骤面板重构纵向时间线。
4. 使用语义状态令牌区分模型、工具、警告、错误和独立 Run 事件。
5. 对 Run ID、Payload、Subagent、Run State 与最终结果设置等宽字体和局部溢出边界。

**验证：** 运行 `npm test -- --run src/test/adapters.test.ts src/test/app.test.tsx`、`npm run typecheck` 和 `npm run build`，Trace 数据适配与页面编译通过。

## T13: 重构设置页视觉

**文件：** `web/frontend/src/pages/SettingsPage.tsx`、`web/frontend/src/styles/pages.css`

**依赖：** T1、T5、T7、T9

**步骤：**

1. 用 Lucide 图标替换六个设置分组中的字符符号。
2. 将设置网格改为紧凑双栏、8px 以下圆角和主题化控件表面。
3. 使用语义警告令牌重构预览提示，保持“不读取、不保存”的文案和行为。
4. 主题化 Toggle 的轨道、圆点、焦点和禁用状态。
5. 保持所有字段本地状态和提交拦截，不增加配置 API 请求。

**验证：** 运行 `npm run typecheck` 和 `npm test -- --run src/test/app.test.tsx`；代码搜索确认 `SettingsPage` 未新增 API Client 导入。

## T14: 重构文件与分析页视觉

**文件：** `web/frontend/src/pages/FilesPage.tsx`、`web/frontend/src/pages/AnalysisPage.tsx`、`web/frontend/src/styles/pages.css`

**依赖：** T1、T5、T7、T9

**步骤：**

1. 为上级目录、新建、上传、打开、预览、下载、重命名和删除动作加入合适图标。
2. 将文件工具栏、面包屑、文件行和预览 Modal 改为平面细描边结构。
3. 确保长文件名、路径和预览文本使用局部换行或滚动。
4. 为分析下载与提交动作加入图标，收紧输入/输出双栏工具面。
5. 保持文件和分析的所有 API、确认、忙碌与错误行为不变。

**验证：** 运行 `npm run typecheck`、`npm test -- --run src/test/app.test.tsx` 和 `npm run build`，两个页面编译且现有应用冒烟通过。

## T15: 重构记忆、状态与启动反馈

**文件：** `web/frontend/src/pages/MemoryPage.tsx`、`web/frontend/src/pages/StatusPage.tsx`、`web/frontend/src/app/AppBootstrap.tsx`、`web/frontend/src/styles/pages.css`

**依赖：** T1、T5、T7、T9

**步骤：**

1. 为记忆和状态刷新动作加入标准图标，收紧记忆主从视图与状态指标网格。
2. 将记忆正文、路径和运行信息切换到主题化等宽文本。
3. 移除状态页渐变与发光圆点，用状态前景/背景/边框表达就绪和异常。
4. 将启动品牌反馈改为稳定、低圆角、无跳动的主题化加载状态。
5. 保持 Runtime 失败局部化、Workspace 编辑和记忆选择行为不变。

**验证：** 运行 `npm run typecheck`、`npm test -- --run src/test/app.test.tsx` 和 `npm run build`，页面与启动流程编译通过。

## T16: 完成响应式和颜色硬编码审计

**文件：** `web/frontend/src/styles/base.css`、`layout.css`、`components.css`、`pages.css`、`responsive.css`

**依赖：** T10、T11、T12、T13、T14、T15

**步骤：**

1. 扫描五个消费端样式文件，把剩余 Hex、RGB、RGBA 和暗色专用文本全部替换为令牌。
2. 检查所有圆角，除胶囊、开关和圆形状态点外不超过 8px。
3. 检查 1440px 桌面布局的侧栏、页头、表格、Trace 和固定 Composer 边界。
4. 检查 390px 布局的抽屉、按钮换行、设置单列、分析单列和状态网格。
5. 为长 Run ID、路径、Markdown 表格、JSON、代码和 Payload 设置明确的 `min-width: 0`、换行或局部滚动。
6. 确认减少动态效果规则覆盖淡入、抽屉、Disclosure、骨架和主题相关过渡。

**验证：** 运行 `rg -n '#[0-9a-fA-F]{3,8}|rgba?\(' src/styles/{base,layout,components,pages,responsive}.css`，期望无结果；运行 `npm run lint` 和 `npm run build` 通过。

## T17: 完成主题和前端回归测试

**文件：** `web/frontend/src/test/theme.test.tsx`、`web/frontend/src/test/app.test.tsx`、`web/frontend/src/test/setup.ts`

**依赖：** T16

**步骤：**

1. 补齐系统浅色、系统暗色、无效存储、手动切换、持久恢复和系统变化测试。
2. 断言主题切换不改变当前 Hash，且应用 Root 不重新挂载。
3. 断言桌面与抽屉共用的主题入口具有图标、动态名称、Tooltip 和键盘可操作性。
4. 运行所有现有 Markdown、Technical Output、Adapter、Client 和路由测试，修复视觉标记造成的断言回归。

**验证：** 在 `web/frontend` 依次运行 `npm run typecheck`、`npm run lint`、`npm test -- --run`，全部通过且无未处理测试警告。

## T18: 构建、Python 回归与浏览器视觉验收

**文件：** `web/static/app/`、`tests/test_web_react_frontend.py`

**依赖：** T4、T17

**步骤：**

1. 运行生产构建，确认入口包含主题预加载且新 JS/CSS 资源使用哈希路径。
2. 运行 React 静态集成测试和现有相关 Web Python 测试，确认 API、认证和 SPA fallback 未回退。
3. 启动本地 Python Server，通过真实登录态打开应用。
4. 在浅色/暗色与 1440x900/390x844 四种组合下切换八个 Hash 视图。
5. 保存聊天、日志、Runs 和设置重点截图，与 `Untitled/` 对比外壳、密度、颜色、圆角和层级。
6. 对每个视图检查 `documentElement.scrollWidth === clientWidth`、控制台异常、主题属性和主要内容非空。
7. 检查首次系统主题、手动切换、刷新恢复、折叠侧栏和移动抽屉完整流程；发现问题后回到对应任务修复并重跑。

**验证：** `npm run build`、`pytest -q tests/test_web_react_frontend.py` 及相关 Web 回归测试通过；四种浏览器组合的八视图无页面级横向溢出或未处理异常，重点截图内容非空且视觉符合参考图。

## 执行顺序

```text
T1 -> T3 -> T8 -> T9 ─┬-> T10 ─┐
T2 -> T3              ├-> T11  │
 └-> T4               ├-> T12  │
 └-> T5 -> T6 -> T7 --├-> T13  ├-> T16 -> T17 -> T18
                      ├-> T14  │
                      └-> T15 ─┘
```

T10-T15 在依赖完成后可按文件边界独立执行；当前工作区存在用户未提交改动，执行期间只修改文件清单中的目标，不提交或清理其他变更。

