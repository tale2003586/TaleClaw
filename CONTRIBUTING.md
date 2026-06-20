# 贡献指南

感谢你对 taleclaw 的关注。本文档说明如何参与开发。

## 开发环境

推荐使用 [uv](https://github.com/astral-sh/uv) 管理依赖：

```bash
uv venv
uv pip install -e ".[dev]"
cp .env.example .env   # 填好本地所需的 provider / 凭证
```

Python 版本要求 `>=3.12`。

## 代码风格

- 遵循 PEP 8，函数尽量带类型注解与 docstring。
- 单个文件控制在合理规模，超大文件请拆分为多个模块。
- 不要把密钥、token、真实 `.env` 提交到仓库。

## 测试

提交前请确保测试通过：

```bash
pytest -q
```

新增功能或修复缺陷时，请补充相应测试。

## 提交规范

使用 [Conventional Commits](https://www.conventionalcommits.org/)：

```text
feat:     新功能
fix:      缺陷修复
docs:     文档
refactor: 重构（无功能变更）
test:     测试
chore:    构建 / 杂项
```

- 保持单次提交聚焦一件事，diff 小而可审查。
- 不要在一个提交里混入无关改动。

## Pull Request

1. 从 `main` 切出特性分支。
2. 完成改动并通过本地测试。
3. 提交 PR，在描述中说明：改了什么、如何验证、是否有遗留项。
