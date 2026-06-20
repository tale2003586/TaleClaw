# 任务完成文档留痕约定

日期：2026-06-09

## 约定

从现在开始，每完成一个明确的代码、配置、文档或架构任务，都需要在 `docs/` 下新增或更新一份说明文档，记录本次任务做了什么。

## 文档要求

任务完成说明至少包含：

- 任务背景：为什么做这件事。
- 改动范围：涉及哪些模块、文件或配置。
- 核心实现：关键设计和实现点。
- 验证方式：运行了哪些测试、检查或手动验证。
- 后续建议：如果还有下一步，应明确写出来。

## 命名规则

文档文件名需要带日期，推荐格式：

```text
docs/workplan/YYYY-MM-DD_<TASK_NAME>_COMPLETION.md
```

如果任务属于明确主题，也可以放到对应目录，例如：

```text
docs/runtime/YYYY-MM-DD_<TASK_NAME>.md
docs/gateways/YYYY-MM-DD_<TASK_NAME>.md
docs/deployment/YYYY-MM-DD_<TASK_NAME>.md
```

## 执行规则

- 小改动也要留痕，但文档可以短。
- 大任务需要单独成文，并在 `docs/README.md` 添加索引。
- 如果任务已有计划文档，完成说明应链接回原计划文档。
- 不在文档中写入 `.env`、API key、token、密码等敏感信息。
