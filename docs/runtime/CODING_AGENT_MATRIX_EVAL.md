# Coding Agent Matrix Eval 使用说明

这个脚本用于把同一套 coding benchmark 放到不同 agent / 功能开关组合下跑，最后生成矩阵报告。

## 该用哪个 JSON

真正的任务集 JSON 是：

```text
benchmarks/coding_tasks.json
```

它包含每个任务的 fixture、prompt、allowed tools、step budget、verifier 和 scripted baseline。

矩阵配置 JSON 是：

```text
evaluation/coding_agent_matrix.example.json
```

它决定要跑哪些 agent、哪些功能开关、哪些 task id，并通过 `benchmark_path` 指向任务集：

```json
{
  "benchmark_path": "benchmarks/coding_tasks.json"
}
```

日常使用时，建议复制或生成一份自己的矩阵配置，然后改那份。

## 快速运行

跑示例矩阵：

```bash
python scripts/run_coding_agent_matrix.py \
  --config evaluation/coding_agent_matrix.example.json \
  --output-root .evals/coding_agent_matrix
```

默认会在控制台输出每个 cell 以及每个任务的完成结果，例如：

```text
PASS coding-git-diff-008 category=git steps=6 tools=5 tokens=0 time=86ms
```

如果只想保留最终报告路径，可以加 `--quiet`。

生成一个可编辑模板：

```bash
python scripts/run_coding_agent_matrix.py \
  --write-template evaluation/coding_agent_matrix.local.json
```

先只看会展开成哪些 cell，不真正跑：

```bash
python scripts/run_coding_agent_matrix.py \
  --config evaluation/coding_agent_matrix.example.json \
  --dry-run
```

只跑前 N 个 cell：

```bash
python scripts/run_coding_agent_matrix.py \
  --config evaluation/coding_agent_matrix.example.json \
  --limit-cells 2
```

## 跑哪些任务

在矩阵配置里设置：

```json
{
  "task_ids": ["coding-git-diff-008"]
}
```

如果要跑全部任务，删除 `task_ids` 字段，或用不带 `--config` 的默认矩阵。

也可以用命令行临时覆盖：

```bash
python scripts/run_coding_agent_matrix.py \
  --config evaluation/coding_agent_matrix.example.json \
  --task-id coding-fix-math-001 \
  --task-id coding-git-diff-008
```

## 配置 agent

`agents` 表示不同 coding agent / 模型路线：

```json
{
  "agents": [
    {
      "name": "scripted-baseline",
      "runner": "scripted"
    },
    {
      "name": "real-default",
      "enabled": true,
      "runner": "real",
      "env": {
        "LLM_ROUTE_CODING": "openai_relay,deepseek,mimo"
      }
    }
  ]
}
```

- `scripted`：不用真实模型，验证 harness / verifier / trace 是否稳定。
- `real`：走真实 coding 模型路由，会产生真实 token 消耗。
- `enabled: false`：临时关闭某个 agent。

## 配置功能开关

`feature_sets` 表示要对比的功能组合。脚本会做笛卡尔积：

```text
agents x feature_sets x task_ids x repetitions
```

示例：

```json
{
  "name": "context-budget-off",
  "env": {
    "CONTEXT_ENABLE_SECTION_BUDGET": "0"
  },
  "harness": {
    "no_step_budget": false
  },
  "dimensions": {
    "context_budget": "off",
    "reasoning_step_budget": "on"
  }
}
```

- `env`：传给该 cell 子进程的环境变量，适合控制 runtime 功能开关。
- `harness`：传给 benchmark harness 的参数，适合控制评测方式。
- `dimensions`：只用于报告展示和 CSV 分析。

常用开关：

```json
{
  "CONTEXT_ENABLE_SECTION_BUDGET": "1"
}
```

常用 harness 参数：

```json
{
  "no_step_budget": false,
  "max_reasoning_steps": 80,
  "keep_workspace": false
}
```

## 输出文件

每次运行会生成：

```text
.evals/coding_agent_matrix/matrix_<timestamp>/
```

主要文件：

```text
report.md
report.json
rows.csv
task_rows.csv
expanded_plan.json
cells/<cell_id>/
```

- `report.md`：人看的矩阵报告。
- `report.json`：完整结构化结果。
- `rows.csv`：每个 cell 一行，包含 pass rate、token、耗时、tool/model calls。
- `task_rows.csv`：每个任务一行，便于定位具体失败任务。
- `expanded_plan.json`：矩阵展开后的实际 cell 列表。
- `cells/<cell_id>/`：该 cell 的 stdout、stderr、原始 eval 产物和 workspace。

## 推荐流程

1. 先跑 scripted：

```bash
python scripts/run_coding_agent_matrix.py \
  --config evaluation/coding_agent_matrix.example.json \
  --output-root .evals/coding_agent_matrix
```

2. 确认 `report.md` 全绿。
3. 复制一份配置，打开 `real` agent。
4. 先限制任务和 cell：

```bash
python scripts/run_coding_agent_matrix.py \
  --config evaluation/coding_agent_matrix.local.json \
  --task-id coding-git-diff-008 \
  --limit-cells 2
```

5. 看 `rows.csv` 和 `task_rows.csv`，确认 token、耗时、tool use count 是否符合预期。
