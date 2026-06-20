# Markdown 转 PDF 插件改动记录

## 一、目标

新增一个可以由 Bot 模式和 Coding 模式直接调用的工具：

```text
markdown_to_pdf
```

工具读取工作区中的 Markdown 文件，生成 PDF 文件。默认输出到：

```text
storage/generated/<Markdown 文件名>.pdf
```

这样生成文件会进入现有私有文件区，并被 Docker 的 `./storage:/app/storage`
挂载持久化。

## 二、调用方式

最简调用：

```json
{
  "input_path": "docs/report.md"
}
```

指定输出位置和标题：

```json
{
  "input_path": "storage/reports/daily-ai.md",
  "output_path": "storage/generated/daily-ai.pdf",
  "title": "每日 AI 资料分析",
  "overwrite": true
}
```

重复写入同一个 PDF 时默认拒绝覆盖。只有显式设置：

```json
{
  "overwrite": true
}
```

才会替换旧文件。

PDF 会先生成到同目录临时文件，完成后再原子替换目标文件。转换失败时不会留下半成品。

## 三、支持范围

当前支持：

- 中文与英文文本
- 一级到多级标题
- 粗体、斜体、行内代码
- 有序列表、无序列表
- 引用块
- 代码块
- 分隔线
- `http`、`https` 和 `mailto` 链接
- Markdown 文件所在目录下引用的工作区内本地图片

远程图片不会自动下载。工作区以外的图片也不会读取；PDF 中会留下跳过提示。
这避免了转换文档时发生隐式网络请求或读取服务器其他目录。

## 四、安全边界

插件只允许：

- 输入 `.md` 或 `.markdown` 文件
- 输出 `.pdf` 文件
- 使用工作区相对路径
- 读取工作区内部文件
- 写入工作区内部路径

输入文件默认最大为 `2 MiB`。可以在 `.env` 中调整：

```bash
MARKDOWN_PDF_MAX_BYTES=2097152
```

工具风险级别是 `normal`。在普通 Bot 和 Coding 对话中首轮可见；若以后用于自主定时
任务，仍应进入现有能力审查流程。

## 五、依赖

新增两个 Python 依赖：

```text
mistune==3.2.1
reportlab==4.5.1
```

`mistune` 负责把 Markdown 解析为 AST，`reportlab` 负责生成 PDF。转换器使用
ReportLab 内置 CID 字体 `STSong-Light`，因此 Docker 镜像不需要额外安装中文字体。

## 六、文件改动

新增：

```text
plugins/markdown_pdf/__init__.py
plugins/markdown_pdf/plugin.py
plugins/markdown_pdf/renderer.py
tests/test_markdown_pdf_plugin.py
docs/MARKDOWN_PDF_PLUGIN.md
```

修改：

```text
core/bootstrap.py
requirements.txt
.env.example
```

## 七、部署更新

服务器拉取代码后重新构建镜像：

```bash
git pull --rebase
sudo docker compose up -d --build
```

因为 `requirements.txt` 新增了依赖，不能只重启旧容器。

查看服务状态：

```bash
sudo docker compose ps
sudo docker compose logs --tail=100 agent-console
```

## 八、手工验证

准备 Markdown：

```bash
mkdir -p storage/reports
printf '# 测试报告\n\n这是一段中文内容。\n' > storage/reports/test.md
```

在 CLI 或 Web 对话中发送：

```text
请把 storage/reports/test.md 转成 PDF。
```

生成文件默认位于：

```text
storage/generated/test.pdf
```

## 九、测试

运行：

```bash
python -m unittest discover -s tests -v
```

新增测试覆盖：

- Bot 与 Coding 模式可见性
- 越界路径拒绝
- 默认禁止覆盖已有 PDF
- 输入文件大小限制
- 中文 Markdown 实际生成 PDF
- 工作区内本地图片嵌入与远程图片跳过
- 转换失败时临时文件清理
