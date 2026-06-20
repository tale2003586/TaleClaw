# Legacy 兼容目录清理完成记录

日期：2026-06-10

## 任务背景

Runtime 目录重组后，`core/`、`tasksessions/` 以及 `modes/router.py`、`modes/intent.py`、`modes/execution_plan.py` 只剩旧 import 兼容作用。为了让主目录更清爽，本次将这些无内部依赖的兼容文件移入 `legacy/compat/`。

## 改动范围

- `core/` -> `legacy/compat/core/`
- `tasksessions/` -> `legacy/compat/tasksessions/`
- `modes/router.py` -> `legacy/compat/modes/router.py`
- `modes/intent.py` -> `legacy/compat/modes/intent.py`
- `modes/execution_plan.py` -> `legacy/compat/modes/execution_plan.py`

同时更新：

- `runtime/bootstrap.py`
- `tests/test_memory_scope.py`
- `docs/overview/PROJECT_STRUCTURE.md`
- `docs/workplan/2026-06-10_RUNTIME_DIRECTORY_RESTRUCTURE_COMPLETION.md`

## 核心实现

- 将最后两处旧路径引用改为新路径。
- 主目录不再保留 `core/` 和 `tasksessions/`。
- `modes/` 只保留实际 profile 与 hybrid classifier 文件。
- 旧兼容文件没有删除，统一放在 `legacy/compat/` 作为迁移参考。

## 验证方式

执行全量测试：

```bash
python -m unittest discover -s tests -v
```

结果：

```text
Ran 169 tests
OK
```
