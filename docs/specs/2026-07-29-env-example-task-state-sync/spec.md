# `.env.example` TaskState 配置同步 Spec

## 背景

最近一次提交为运行时新增了 TaskState 上下文、语义压缩、长内容 Artifact 外置和动态 Prompt 预算配置，并改变了旧 Coding Context 开关的默认值；`.env.example` 仍保留旧说明和旧默认值，且缺少新增配置，容易让部署者得到与代码实际行为不一致的配置。

## 目标

- 让 `.env.example` 中本轮上下文架构相关配置与当前运行时代码的配置名、默认值和语义一致。
- 删除已不再影响运行行为的旧固定压缩参数，清楚区分新的权威配置与仍需迁移兼容的旧开关。
- 为 Artifact 存储路径、动态预算比例和长内容阈值提供可直接复制的示例。

## 功能需求

- F1: 示例文件必须列出当前 TaskState 上下文主路径的四个功能开关，且默认值与代码一致。
- F2: 示例文件必须列出动态 Prompt 预算的比例、安全余量配置，且默认值与代码一致。
- F3: 示例文件必须列出长内容外置的 token、字符、字节阈值及 Artifact 根目录，且默认值与代码一致。
- F4: `CODING_CONTEXT_STATE_ENABLED` 必须标明为迁移兼容开关，并把默认值从关闭改为与代码一致的开启状态。
- F5: 相关注释必须说明 Coding 主路径不再以旧固定阈值作为最终模型调用限制，避免误导部署者继续只调旧预算。
- F6: 从示例文件删除已经被 TaskState 动态预算取代、且当前实现明确忽略的 `CODING_CONTEXT_COMPACTION_TRIGGER_TOKENS`、`CODING_CONTEXT_COMPACTION_TARGET_TOKENS` 和 `CODING_CONTEXT_RECENT_GROUPS`。

## 非功能需求

- N1: 只修改示例配置与配套说明，不改变 Python 运行逻辑或任何真实 `.env`。
- N2: 不在示例中加入密钥、真实凭据或机器特有的 Artifact 绝对路径。
- N3: 保持 `.env.example` 现有章节结构和注释风格，新增内容集中在上下文配置区域。

## 不做的事

- 不让 Web 设置页保存或读取 `.env`；当前页面仍是预览模式。
- 不从 Python 运行代码删除旧配置读取；兼容读取仍保留一个迁移周期。
- 不删除仍被非 Coding 上下文路径使用的 `CONTEXT_*` 分区预算配置。
- 不调整现有默认阈值、预算算法、数据库或 Artifact 实现。
- 不全面审计仓库所有模块的环境变量，仅同步最近 TaskState 上下文改动直接涉及的配置。

## 验收标准

- AC1: 对比 `config.py`，本轮新增的 12 个上下文相关环境变量均能在 `.env.example` 中找到，名称和默认值一致。
- AC2: `CODING_CONTEXT_STATE_ENABLED` 在示例中为 `1`，并明确标注为迁移兼容配置。
- AC3: `CONTEXT_ARTIFACT_ROOT` 使用相对项目目录的安全示例，不含开发机绝对路径。
- AC4: 注释明确说明动态预算的默认比例为 70% / 45% / 92%，安全余量填 `0` 时由运行时采用模型窗口的默认余量策略。
- AC5: 三个已被忽略的旧固定压缩参数不再出现在 `.env.example`，但仍被实际使用的分区预算配置保持不变。
- AC6: Git diff 只包含规格文档与 `.env.example` 的目标修改，没有运行逻辑变化。
