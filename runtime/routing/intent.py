from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class IntentCandidate:
    intent: str
    execution: str = "pipeline_bot"
    confidence: float = 1.0
    reason: str = ""
    command: str | None = None


class IntentClassifier:
    """Classify user text into coarse platform intents."""

    def classify(self, user_text: str, session=None) -> IntentCandidate | None:
        text = user_text.strip().lower()

        if text in {"/coding", "进入编程模式", "编程模式"}:
            return IntentCandidate(
                intent="mode_switch",
                execution="direct_reply",
                reason="Explicit coding mode command.",
                command="coding",
            )
        if text in {"/chat", "/bot", "回到聊天模式", "聊天模式"}:
            return IntentCandidate(
                intent="mode_switch",
                execution="direct_reply",
                reason="Explicit bot mode command.",
                command="bot",
            )
        if text in {"/hybrid", "混合模式", "自动模式"}:
            return IntentCandidate(
                intent="mode_switch",
                execution="direct_reply",
                reason="Explicit hybrid mode command.",
                command="hybrid",
            )

        if self._looks_like_strong_coding_request(text):
            return IntentCandidate(
                intent="coding",
                execution="task_session",
                confidence=0.78,
                reason="Request strongly indicates code or project file work.",
            )
        if self._looks_like_storage_request(text):
            return IntentCandidate(
                intent="storage_file",
                execution="pipeline_bot",
                confidence=0.82,
                reason="Request refers to storage file listing, preview, or download.",
            )
        if self._looks_like_memory_request(text):
            return IntentCandidate(
                intent="memory_query",
                execution="pipeline_bot",
                confidence=0.74,
                reason="Request asks about remembered or historical context.",
            )
        if self._looks_like_coding_request(text):
            return IntentCandidate(
                intent="coding",
                execution="task_session",
                confidence=0.62,
                reason="Request contains coding or project-operation indicators.",
            )
        return None

    def _looks_like_storage_request(self, text: str) -> bool:
        keywords = [
            "storage",
            "文件区",
            "网盘",
            "下载文件",
            "查看文件",
            "预览文件",
            "列出文件",
            "/files",
            "/download",
            "/cat",
            "上传的文件",
            "报告文件",
        ]
        return any(keyword in text for keyword in keywords)

    def _looks_like_memory_request(self, text: str) -> bool:
        keywords = [
            "记忆",
            "记忆系统",
            "历史记忆",
            "长期记忆",
            "历史记录",
            "你记得",
            "之前说过",
            "recall_memory",
        ]
        return any(keyword in text for keyword in keywords)

    def _looks_like_strong_coding_request(self, text: str) -> bool:
        if "运行测试" in text or "run tests" in text or "pytest" in text:
            return True
        action_keywords = [
            "修改",
            "修复",
            "改一下",
            "实现",
            "重构",
            "编辑",
            "调试",
            "debug",
            "fix",
            "edit",
            "refactor",
        ]
        artifact_keywords = [
            "代码",
            "bug",
            "函数",
            "class",
            "import",
            ".py",
            ".js",
            ".ts",
            ".css",
            ".html",
            "git",
            "docker",
            "前端",
            "后端",
        ]
        return (
            any(keyword in text for keyword in action_keywords)
            and any(keyword in text for keyword in artifact_keywords)
        )

    def _looks_like_coding_request(self, text: str) -> bool:
        keywords = [
            "代码",
            "报错",
            "bug",
            "实现",
            "重构",
            "测试",
            "运行",
            "文件",
            "函数",
            "class",
            "import",
            "pytest",
            "python",
            "cli.py",
            ".py",
            "git",
            "shell",
        ]
        return any(keyword in text for keyword in keywords)
