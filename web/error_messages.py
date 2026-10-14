"""Public runtime errors must never copy an upstream response or credential."""
from __future__ import annotations


def public_runtime_error(exc: BaseException) -> str:
    classes = {(base.__module__, base.__name__) for base in type(exc).__mro__}
    if any(module.startswith("openai") and name == "APIError" for module, name in classes):
        status = getattr(exc, "status_code", None)
        if status in {401, 403}:
            return "模型服务拒绝鉴权，请联系管理员检查 Provider 与 API Key。"
        if status == 429:
            return "模型服务请求受限，请稍后重试或联系管理员检查配额。"
        return "模型服务请求失败，请稍后重试或联系管理员检查连接。"
    if ("models.model_pool", "ModelRouteError") in classes:
        return "当前模型路由请求失败，请稍后重试或联系管理员检查服务状态。"
    if any(module.startswith("httpx") and name == "HTTPError" for module, name in classes):
        return "模型服务连接失败或超时，请稍后重试。"
    if isinstance(exc, KeyError) and exc.args == ("DEEPSEEK_API_KEY",):
        return "缺少 DEEPSEEK_API_KEY。请在环境变量或模型设置中配置后再使用聊天功能。"
    if isinstance(exc, ModuleNotFoundError):
        return f"缺少 Python 模块：{exc.name}。请联系管理员安装完整依赖。"
    if isinstance(exc, ValueError):
        return str(exc)
    return f"运行失败（{type(exc).__name__}），请重试或联系管理员检查服务日志。"


def public_worker_event(event: dict) -> dict:
    """Protect public model-error events even when internal trace captures content."""
    data = event.get("data")
    if event.get("type") != "event" or not isinstance(data, dict) or data.get("event") not in {
        "model.call.failed", "model.route.attempts", "run_failed", "run.failed",
    }:
        return event
    hidden_fields = {"error", "error_message", "exception", "last_error"}

    def project(value, depth=0):
        if depth > 16:
            return "[omitted]"
        if isinstance(value, dict):
            return {key: "模型调用失败，详情请由管理员检查服务日志。"
                    if key in hidden_fields and isinstance(item, str) else project(item, depth + 1)
                    for key, item in value.items()}
        if isinstance(value, list):
            return [project(item, depth + 1) for item in value]
        return value

    return {**event, "data": project(data)}
