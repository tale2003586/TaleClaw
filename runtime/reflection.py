import json
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ReflectionDecision:
    action: str = "continue"
    reason: str = ""
    instruction: str = ""
    message: str = ""


@dataclass(frozen=True)
class ReflectionContext:
    session_id: str
    agent_name: str
    profile_name: str
    tool_mode: str
    reasoning_steps: int
    unavailable_tools: tuple[str, ...]
    loop_guard_denied: bool
    tool_results: tuple[dict[str, Any], ...]
    recent_messages: tuple[dict[str, Any], ...]


class ReflectionAgent:
    """Lightweight evaluator for risky or stuck reasoning states."""

    def __init__(
        self,
        *,
        provider,
        model: str,
        max_tokens: int = 500,
        min_reasoning_steps: int = 10,
        reflection_interval: int = 5,
    ) -> None:
        self.provider = provider
        self.model = model
        self.max_tokens = max(1, int(max_tokens))
        self.min_reasoning_steps = max(1, int(min_reasoning_steps))
        self.reflection_interval = max(1, int(reflection_interval))

    def should_reflect(
        self,
        *,
        session,
        profile,
        response,
        execution,
        reasoning_steps: int,
    ) -> bool:
        if execution.loop_guard_denied:
            return True
        if execution.unavailable_tools:
            return True
        if any(item.get("status") != "success" for item in execution.tool_results):
            return True
        if reasoning_steps < self.min_reasoning_steps:
            return False
        return (reasoning_steps - self.min_reasoning_steps) % self.reflection_interval == 0

    def reflect(
        self,
        *,
        session,
        profile,
        response,
        execution,
        reasoning_steps: int,
    ) -> ReflectionDecision:
        context = ReflectionContext(
            session_id=str(getattr(session, "id", "")),
            agent_name=str((getattr(session, "metadata", {}) or {}).get("kind", "agent")),
            profile_name=str(getattr(profile, "name", "")),
            tool_mode=str(getattr(profile, "tool_mode", "")),
            reasoning_steps=reasoning_steps,
            unavailable_tools=tuple(execution.unavailable_tools),
            loop_guard_denied=execution.loop_guard_denied,
            tool_results=tuple(execution.tool_results[-6:]),
            recent_messages=tuple((getattr(session, "messages", []) or [])[-8:]),
        )
        messages = [
            {
                "role": "system",
                "content": (
                    "You are a reflection agent. Evaluate whether the active "
                    "agent should continue, revise strategy, ask the user, or stop. "
                    "Do not solve the task. Return compact JSON only with keys: "
                    "action, reason, instruction, message. action must be one of "
                    "continue, revise, ask_user, stop."
                ),
            },
            {
                "role": "user",
                "content": json.dumps(context.__dict__, ensure_ascii=False, default=str),
            },
        ]
        try:
            response = self.provider.chat(
                model=self.model,
                messages=messages,
                tools=[],
                tool_choice="none",
                max_tokens=self.max_tokens,
            )
            return _parse_decision(response.content or "")
        except Exception as exc:
            return ReflectionDecision(
                action="continue",
                reason=f"Reflection failed: {type(exc).__name__}: {exc}",
            )


def _parse_decision(text: str) -> ReflectionDecision:
    raw = _extract_json(text)
    if not raw:
        return ReflectionDecision(action="continue", reason="Reflection returned no JSON.")
    try:
        data = json.loads(raw)
    except Exception as exc:
        return ReflectionDecision(
            action="continue",
            reason=f"Reflection JSON parse failed: {exc}",
        )
    if not isinstance(data, dict):
        return ReflectionDecision(action="continue", reason="Reflection JSON was not an object.")
    action = str(data.get("action") or "continue").strip().lower()
    if action not in {"continue", "revise", "ask_user", "stop"}:
        action = "continue"
    return ReflectionDecision(
        action=action,
        reason=str(data.get("reason") or ""),
        instruction=str(data.get("instruction") or ""),
        message=str(data.get("message") or ""),
    )


def _extract_json(text: str) -> str:
    cleaned = (text or "").strip()
    if cleaned.startswith("```"):
        lines = cleaned.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].startswith("```"):
            lines = lines[:-1]
        cleaned = "\n".join(lines).strip()
    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start == -1 or end == -1 or end < start:
        return ""
    return cleaned[start:end + 1]
