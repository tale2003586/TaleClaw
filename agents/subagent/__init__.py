from .result import SubagentResult

__all__ = ["SubagentResult", "TaskSubagentRunner"]


def __getattr__(name: str):
    if name == "TaskSubagentRunner":
        from .runner import TaskSubagentRunner

        return TaskSubagentRunner
    raise AttributeError(name)
