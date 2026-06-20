# Coding Agent Instructions

## Core Workflow

- 先判断任务范围：单文件、少量明确文件、或窄修复任务直接处理；跨模块、大仓库、多线索任务先做证据收集和任务拆分。
- 修改前读取相关代码、测试和配置；不要只靠文件名猜测。
- 优先使用 `rg` 搜索文件、符号和错误信息，再用定向读取查看上下文。
- 保持改动聚焦，不做无关重构，不替用户清理无关文件。
- 修改后运行相关测试或验证命令；如果无法运行，说明原因和剩余风险。
- 遇到失败时先定位原因，再改变方法；不要重复同一个失败动作。

## Workspace

- 只能在当前绑定 workspace 内读写文件，所有文件工具路径都应是 workspace-relative。
- 不允许路径逃逸，不要猜测或硬写 workspace 外的绝对路径。
- 不覆盖用户未要求修改的文件。
- 真实 workspace 改动需要能通过 diff 复盘。

## Large Files And Tool Use

- 不要一次性读取超大文件全文。
- 先用 `repo_map`、`code_outline`、`rg`、`git_diff`、`git_status` 或分段读取定位相关区域。
- 如果工具返回内容被截断，要根据路径、行号、符号继续精确读取。
- 当 `read_file` 或 `list_files` 返回 truncation/offset 信息时，沿着 offset 继续读取，不要反复读取同一段。
- 大输出命令要过滤、分页或摘要，避免把上下文塞满。

## Tools

- 文件修改优先使用 `edit_file` 或受控写文件工具。
- `git_add`、`git_commit` 只有在用户明确要求时使用。
- shell 命令要尽量具体，避免破坏性操作。
- 需要项目约定、测试偏好或历史架构选择时，先用 `recall_memory`；只有稳定事实或长期约定才用 `memorize`。

## Broad Repository Work

- 对架构梳理、跨子系统检查、多线索只读分析，先用 `repo_map` 建立确定性的文件地图，再决定是否继续下钻。
- 使用 `repo_map(path=...)` 逐层缩小范围，避免用重复的 `list_files` 扫整个仓库。
- 对大文件先用 `code_outline` 查看符号、入口和行号，再按窗口读取关键片段。
- 把任务拆成可验证的线索：每条线索应有明确目标、文件范围和交付格式。
- 父 agent 负责跨线索综合；子 agent 只负责定位、提取和报告局部事实。

## Subagent Orchestration

- 只有宽任务、多线索任务或独立事实抽取任务才使用子 agent；窄修复和单文件任务直接处理。
- `parallel_tasks` / `task` 只用于短时 scout 工作：定位、列举、从明确文件中提取局部事实。
- `spawn_teammate` 用于跨文件综合、设计分析、实现方案或需要多轮迭代的复杂工作。
- 调用 `task` 或 `parallel_tasks` 前必须满足：
  - 已经通过 `repo_map`、`list_files`、`rg` 或 `code_outline` 验证目标文件存在。
  - 每个子任务的 `scope.files` 最多包含 5 个具体 workspace-relative 文件。
  - `scope.files` 必须来自已验证结果，不能只在 prompt 里写文件名。
  - 子任务目标使用 locate/extract/report 这类窄动作，不能要求宽泛总结整个目录。
  - 大文件任务必须 code_outline-first，不能要求子 agent 读取整个大文件。
- 禁止给子 agent 派发这类模糊任务：`investigate this path`、`summarize this subsystem`、`for each directory describe`，除非已经附带具体文件列表和输出限制。

## Subagent Failure Handling

- 子 agent 返回失败时，先读取 `failure_reason`、`recoverable`、`retry_hint`、`status`、`evidence` 和 `findings`，再决定下一步。
- `subagent_step_limit` 或 `subagent_scope_too_broad`：只允许针对该线索重试一次，并且必须缩小文件范围或改成 code_outline-first。
- `subagent_tool_error`：只有改变方法时才重试，例如换文件、减少 scope、先做 outline；不要原样重发。
- `subagent_missing_required_files`、`subagent_empty_findings` 或线索本身不可行：记录原因，继续其他线索，不要盲目重试。
- 一次 targeted retry 后仍失败，就停止对子任务继续 fan-out；改为父 agent 小范围直接处理，或诚实报告 incomplete reason。
- 不要忽略 `retry_hint` 后退回大范围 `read_file` / `list_files` 扫描，这会消耗主预算并可能触发循环保护。

## Reporting

- coding task 完成后说明改了什么、验证了什么。
- 有 workspace diff 或 run trace 时，优先引用这些事实。
- 重要架构或行为改动需要写入 `docs/workplan/` 完成记录。
- 架构梳理类任务的完成标准是：模块关系、关键入口文件/函数、推荐验证命令都已覆盖。
- 行号和精确引用是增强项，不是阻塞项；不要为了补齐每个行号反复调用 `nl`、`rg`、`read_file` 或 `bash`。
- 如果核心线索已经能回答，直接收尾；缺少非关键行号时可写“约在相关入口附近”或省略。
- 如果某条线索没有完成，说明缺口、原因和已经验证过的证据，不要把未完成说成已完成。
