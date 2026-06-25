# SWE-bench Verified 10 Task Eval

这个入口用于从 SWE-bench Verified 里取 10 个任务，逐个交给当前 coding runtime 跑，并输出批量报告和合并后的 `predictions.jsonl`。

## 准备依赖

只需要加载 Hugging Face 数据集时，安装：

```bash
python -m pip install -r requirements-swebench.txt
```

如果要跑官方判题，还需要单独准备 SWE-bench 官方 repo 和 Docker 评测环境。

## 先看会选哪 10 个

```bash
python scripts/run_swebench_verified.py --dry-run
```

默认数据集是：

```text
SWE-bench/SWE-bench_Verified
```

脚本也接受这些别名：

```bash
python scripts/run_swebench_verified.py --dataset-name verified --dry-run
python scripts/run_swebench_verified.py --dataset-name swe-verified --dry-run
```

如果当前官方命名加载失败，脚本会 fallback 到：

```text
princeton-nlp/SWE-bench_Verified
```

## 跑 10 个任务

```bash
python scripts/run_swebench_verified.py \
  --limit 10 \
  --max-reasoning-steps 80
```

默认会使用本地 repo mirror cache：

```text
.evals/swebench_repo_cache/
```

第一次遇到某个 GitHub repo 时会执行 `git clone --mirror`，后续同 repo 的任务会从本地 mirror clone，避免每个 instance 都重新拉完整仓库。clone 失败时，控制台和 `rows.json` 会带上 git stderr。

输出目录：

```text
.evals/swebench_verified/swe_verified_<timestamp>/
```

主要文件：

```text
summary.md
summary.json
rows.json
selected_instances.json
predictions.jsonl
tasks/
```

- `summary.md`：批量人读报告。
- `rows.json`：每个 instance 的 agent run 状态、patch bytes、token、tool calls、run_dir。
- `predictions.jsonl`：可交给官方 SWE-bench harness 的合并预测文件。
- `tasks/`：每个任务自己的 trace / metrics / result。

控制台会逐个输出：

```text
[01/10] START astropy__astropy-...
[01/10] PASS astropy__astropy-... patch=1234B tokens=56789 tools=42 time=120000ms
```

这里的 `PASS` 表示 agent 成功完成运行并产出 patch，不等同于官方判题 resolved。

## 指定 10 个任务

```bash
python scripts/run_swebench_verified.py \
  --instance-id astropy__astropy-12907 \
  --instance-id django__django-11099 \
  --instance-id sympy__sympy-20590
```

也可以逗号分隔：

```bash
python scripts/run_swebench_verified.py \
  --instance-id astropy__astropy-12907,django__django-11099
```

## 换一段 deterministic slice

默认取前 10 个。要跳过前 20 个再取 10 个：

```bash
python scripts/run_swebench_verified.py --offset 20 --limit 10
```

## clone 网络不稳定时

可以提高 retry 和 timeout：

```bash
python scripts/run_swebench_verified.py \
  --limit 1 \
  --clone-retries 4 \
  --git-timeout-seconds 1200
```

也可以指定 cache 目录：

```bash
python scripts/run_swebench_verified.py \
  --limit 1 \
  --repo-cache-root .evals/swebench_repo_cache
```

## 生成官方评测命令

如果你有官方 SWE-bench repo：

```bash
python scripts/run_swebench_verified.py \
  --limit 10 \
  --swebench-repo /home/tale/kaggle/bench/SWE-bench
```

报告里会出现类似：

```bash
python -m swebench.harness.run_evaluation ...
```

## 直接跑官方判题

```bash
python scripts/run_swebench_verified.py \
  --limit 10 \
  --swebench-repo /home/tale/kaggle/bench/SWE-bench \
  --evaluate
```

官方判题需要 Docker，并且会构建/运行每个实例对应的测试环境，耗时会明显更长。
