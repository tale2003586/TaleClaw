# 技能热更新与前端设置验收

已补齐设置页的服务端技能管理，以及同一 SkillLoader 的读取、启停、编辑、校验和热刷新链路。默认技能目录固定为项目根目录下的 `skills/`，状态直接保存在原 `SKILL.md`，没有额外的配置数据库、浏览器开关副本或第二个技能管理器。3 个子 Agent 分别负责加载器、前端与对抗式运行时回归，主线程负责 Web API、部署持久化及整体验收。

**完成范围：技能文档管理与热更新。** 管理 API、加载器、前端源码和构建资源与已测版本一致；共享工作区同时存在其他 Coding Task、状态和模型相关开发，后端全量通过结论对应下述冻结版本，不能直接覆盖冻结后的并行修改。

## 用户可用功能

管理员从设置页进入“服务端技能”，可以：

- 查看技能说明、相对路径、适用模式、依赖工具、启停状态及无效文件诊断。
- 启用或停用技能；收到服务端成功响应后更新页面状态。
- 查看并编辑完整 `SKILL.md`，保存时检查读取版本。
- 重新扫描目录，发现文件系统中新增、修改或移除的技能。
- 在保存冲突、校验失败或刷新目录时保留草稿；未保存时避免切换技能或调整启停导致内容丢失。

普通账号看到管理权限说明，不请求管理 API；服务端同样要求管理员。页面明确区别浏览器主题/工作区偏好与影响所有账号的服务端技能设置。操作说明及接口合同维护在 [Web 使用与部署文档](../../web/README.md#技能设置与热更新)。

更新影响后续 `tool_search` 和 `load_skill`，不需要重启服务；已经进入现有会话的技能文本不会被自动删除。需要完整采用新流程时应新建会话。本轮没有将 Python 插件、模型客户端或 `.env` 参数描述成支持热卸载/热重建。

## 实现取舍

| 原有断点 | 本次实现 |
|---|---|
| 文件自动刷新存在，但没有可管理的启停状态 | 同一 SKILL.md 的 `enabled` 布尔字段，默认启用；运行时过滤禁用项 |
| 重名技能静默覆盖，无效 YAML 被当空 metadata | 重名全部隔离，无效文件显示诊断；合法技能继续可用 |
| 管理页与运行时可能各自维护配置 | API 直接复用运行时 singleton，无新目录缓存或持久配置来源 |
| 修改技能可能覆盖他人刚保存的内容 | SHA-256 revision 校验、进程内串行保存、临时文件加原子替换 |
| 文件枚举和写入没有专门边界 | 逐层目录 fd 与 no-follow；拒绝越界、符号链接、FIFO及其他非普通文件 |
| 文件内容可能过大或异常 | 单文档 256 KiB 限额；无效 UTF-8、深嵌套 YAML 等隔离 |
| 前端只有本地主题和工作区偏好 | 在既有设置页内增加技能列表与编辑器，保留现有布局和权限模型 |
| Docker 重建可能丢失在线编辑 | Web、Telegram、Feishu 均挂载 `./skills:/app/skills` |
| cwd 决定技能目录，Web 可先于 runtime 加载 | 目录锚定技能模块所在项目；移除无人消费的 config 目录别名 |

目录版本来自内容和诊断状态，文件版本来自原始 bytes；同大小、同 mtime 的外部内容修改也能发现。管理 API 包括列表、详情、强制刷新和带版本更新；正文与启停恰选一项。管理员校验发生在读写之前，写入要求 JSON，并拒绝浏览器跨来源请求；目录、用户角色和根路径不能由 body 改写。

保留既有渐进发现：技能全文不会重新塞进每一轮 system prompt。`requires_tools` 和 `safety` 是描述，不能授予 Agent 原本没有的工具权限；AgentSpec 技能范围、模式、include_skills 以及工具 allow/deny/admin 策略仍生效。已经被搜索解锁的 load_skill 也必须读取当前有效版本。

技能文档不承担 Python 插件生命周期管理。现有插件注册没有完整的撤销、teardown 与失败回滚，不能仅在设置页增加开关就宣称可靠热卸载。

## 对抗回归与修复

加载器新增管理测试先在旧实现失败，覆盖实际启停持久化、并发编辑冲突、外部修改、重复名称、坏文件隔离、目录越界、符号链接、FIFO、超限文件、失败写入清理和固定目录来源。补充对抗输入包括非 mapping frontmatter、非法 enabled、无穷大 priority、深层 YAML 与 safety 混合键；最终加载器组合 28 项通过。

独立 Agent 用真实 ToolRegistry 验证了先搜索解锁、再停用、随后加载必须失败；重新启用、编辑正文、外部删除和不同长期 loader 之间的变化传播也被覆盖。修改 metadata 不能绕过 AgentSpec 或工具权限。移除禁用过滤的故障注入使关键测试如期失败，说明这些检查能发现错误实现。运行时、API 与既有能力组合为 38 passed、25 subtests passed。

第一次全量回归发现 3 个失败，均来自本轮尝试在 config 中复用 SkillLoader 目录常量而提前导入可选模块。删除这个没有消费者的别名后，最小上下文导入与独立配置加载均恢复通过。没有通过放宽测试或重新引入重复目录权威解决。

前端 11 个技能交互检查覆盖非管理员不请求接口、无效配置、非乐观启停、403、保存版本、400/409 草稿保留、刷新/删除后的草稿保留，以及保存成功但列表刷新失败的准确提示。

阶段定向结果互有重叠，不能累加为独立测试或缺陷数。

## 最终验证

| 检查 | 结果 |
|---|---|
| 完整后端，项目 .venv 与全新 PostgreSQL 16 | 1125 passed，另有 27 subtests passed，0 failed，0 skipped |
| 前端 TypeScript | 通过 |
| 前端 ESLint | 通过 |
| 全前端 Vitest | 12 个文件、38 项通过 |
| 前端生产构建 | 通过，已更新实际提供服务的 web/static/app |
| Chrome 真实交互 | 实际启停、保存、版本冲突、草稿与磁盘保留均通过 |
| 视觉检查 | 桌面、版本冲突和 390px 移动端截图已检查 |
| Python AST 与 git diff --check | 通过 |

冻结源聚合 SHA-256：`ca0bd431cffeba7186d3ea52f1d9eb39daaea5fc7abfb115b1e81c74212cdb99`，共 826 个文件。后端 pytest 用时 35.31 秒。测试使用不含真实 .env 的源码副本、私有 Unix socket PostgreSQL 和 Python 网络/数据库 guard；未连接既有数据库、调用模型或安装依赖。临时 PostgreSQL 正常停止，socket 与 pidfile 均已清理。

浏览器测试使用该冻结源的生产构建与真实技能 API，但身份和会话数据由仅绑定 127.0.0.1 的临时预览提供，技能操作只修改临时目录。验证实际 UI→API→文件的路径，没有冒充真实登录会话验收；真实鉴权检查由 API/既有身份测试覆盖。浏览器及预览进程均已退出。首次浏览器自动化选中了隐藏聊天输入框，后改为技能编辑器精确选择器并通过；移动端截图等待侧栏动画结束后检查。

证据：

- [完整后端日志](evidence/2026-10-03-skill-settings/backend-tests.txt)、[JUnit](evidence/2026-10-03-skill-settings/backend-tests.xml)、[验证回执](evidence/2026-10-03-skill-settings/backend-validation.json)、[环境与 guard](evidence/2026-10-03-skill-settings/backend-environment.json)、[数据库清理](evidence/2026-10-03-skill-settings/backend-cleanup.json)。
- [前端检查](evidence/2026-10-03-skill-settings/frontend-results.json)、[前端完整测试](evidence/2026-10-03-skill-settings/frontend-tests.txt)。
- [浏览器结果](evidence/2026-10-03-skill-settings/browser-results.json)、[桌面截图](evidence/2026-10-03-skill-settings/desktop.png)、[冲突截图](evidence/2026-10-03-skill-settings/conflict.png)、[移动端截图](evidence/2026-10-03-skill-settings/mobile.png)。
- [加载器旧实现](evidence/2026-10-03-skill-settings/loader-before.txt)、[异常输入反例](evidence/2026-10-03-skill-settings/loader-edge-before.txt)、[加载器修复](evidence/2026-10-03-skill-settings/loader-after.txt)。
- [运行时旧实现](evidence/2026-10-03-skill-settings/runtime-before.txt)、[红测环境说明](evidence/2026-10-03-skill-settings/runtime-red-setup.txt)、[运行时修复](evidence/2026-10-03-skill-settings/runtime-after.txt)、[禁用过滤故障注入](evidence/2026-10-03-skill-settings/disabled-filter-counterexample.txt)。
- [集成导入失败](evidence/2026-10-03-skill-settings/integration-before-import-fix.txt)、[最终源码清单](evidence/2026-10-03-skill-settings/source-manifest.json)、[当前技能实现身份与并行差异](evidence/2026-10-03-skill-settings/integrity.json)。

可复用的隔离后端验证命令：

```bash
.venv/bin/python docs/workplan/evidence/2026-10-03-skill-settings/validate_isolated_pg.py --source . --python .venv/bin/python
```

## 明确边界

文件 revision 与进程锁不能为任意外部编辑器、多个写进程提供严格跨进程事务；原子替换保证不会由本接口发布半写文件。技能加载已返回的正文也不能在模型既有上下文中远程撤回。

设置页管理现有技能文档；新增和移除目录通过文件系统完成，再自动或手动发现。没有加入远程包下载、依赖安装、脚本执行或删除用户技能目录的 API。原有显式匿名 local/admin 模式沿用已有权限定义。

容器部署第一次采用新挂载需要按更新后的 Compose 重建应用容器，后续技能文档变化无需重建。本轮只修改项目文件和构建资源，没有启动真实部署或修改实际 skills 内容。

最终完整性检查发现冻结后还有其他文件修改。其中 web/server.py 的变化属于 Coding Task 参数校验，与本轮技能路由、鉴权及处理方法无交叉；相关方法经 AST 逐项核对一致，技能模块、前端源码和静态资源也一致。并行变化记录带时间戳，不能把其数量当作持续固定的当前状态，也不把 1125 项通过扩张为对未冻结新代码的背书。
