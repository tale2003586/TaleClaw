from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class ConclusionCandidate:
    category: str
    content: str
    evidence: str = ""
    confidence: float = 0.0
    source: str = "llm"


@dataclass
class ConclusionExtraction:
    summary: str = ""
    candidates: list[ConclusionCandidate] = field(default_factory=list)
    raw_response: str = ""
    error: str = ""


class TaskConclusionExtractor:
    """Ask the LLM for structured, reusable conclusions after a coding task."""

    def __init__(self, *, provider, model: str, max_tokens: int = 1200) -> None:
        self.provider = provider
        self.model = model
        self.max_tokens = max_tokens

    def extract(
        self,
        *,
        user_request: str,
        task_summary: str,
        messages: list[dict[str, Any]],
    ) -> ConclusionExtraction:
        prompt = _build_prompt(
            user_request=user_request,
            task_summary=task_summary,
            messages=messages,
        )
        try:
            response = self.provider.chat(
                model=self.model,
                messages=[
                    {
                        "role": "system",
                        "content": (
                            "Extract durable project memory candidates from a completed coding task. "
                            "Return JSON only. Do not include transient execution details, greetings, "
                            "system prompts, or generic task reports."
                        ),
                    },
                    {"role": "user", "content": prompt},
                ],
                tools=[],
                tool_choice="none",
                max_tokens=self.max_tokens,
            )
            raw = str(response.content or "").strip()
            payload = _parse_json_object(raw)
            return ConclusionExtraction(
                summary=str(payload.get("summary", "")).strip(),
                candidates=_parse_candidates(payload.get("conclusions", [])),
                raw_response=raw,
            )
        except Exception as exc:
            return ConclusionExtraction(error=f"{type(exc).__name__}: {exc}")


def _build_prompt(
    *,
    user_request: str,
    task_summary: str,
    messages: list[dict[str, Any]],
) -> str:
    tool_trace = _format_tool_trace(messages)
    return (
        "Analyze the completed coding task and return this exact JSON shape:\n"
        "{\n"
        '  "summary": "one concise sentence describing the completed task",\n'
        '  "conclusions": [\n'
        "    {\n"
        '      "category": "project|decision|preference|fact",\n'
        '      "content": "a reusable conclusion, maximum 240 characters",\n'
        '      "evidence": "short file, command, or result reference",\n'
        '      "confidence": 0.0\n'
        "    }\n"
        "  ]\n"
        "}\n\n"
        "Rules:\n"
        "- Return at most 6 conclusions.\n"
        "- Keep only durable facts useful in future tasks.\n"
        "- Do not include full replies, system prompts, temporary status, or conversational style.\n"
        "- Use an empty conclusions array if no reusable fact exists.\n\n"
        f"User request:\n{_trim(user_request, 1600)}\n\n"
        f"Final task reply:\n{_trim(task_summary, 2400)}\n\n"
        f"Tool trace summary:\n{tool_trace or '(no tool calls)'}"
    )


def _format_tool_trace(messages: list[dict[str, Any]]) -> str:
    lines = []
    for message in messages:
        if message.get("role") != "tool":
            continue
        arguments = json.dumps(
            message.get("final_arguments", {}),
            ensure_ascii=False,
            default=str,
        )
        content = str(message.get("content", "")).strip()
        lines.append(
            f"- status={message.get('status', 'unknown')} "
            f"args={_trim(arguments, 320)} output={_trim(content, 480)}"
        )
    return _trim("\n".join(lines), 5000)


def _parse_json_object(raw: str) -> dict[str, Any]:
    text = raw.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end < start:
        raise ValueError("LLM conclusion response does not contain a JSON object.")
    payload = json.loads(text[start:end + 1])
    if not isinstance(payload, dict):
        raise ValueError("LLM conclusion response must be a JSON object.")
    return payload


def _parse_candidates(value: Any) -> list[ConclusionCandidate]:
    if not isinstance(value, list):
        return []
    candidates = []
    for item in value[:12]:
        if not isinstance(item, dict):
            continue
        try:
            confidence = float(item.get("confidence", 0.0))
        except (TypeError, ValueError):
            confidence = 0.0
        candidates.append(ConclusionCandidate(
            category=str(item.get("category", "fact")).strip().lower(),
            content=str(item.get("content", "")).strip(),
            evidence=str(item.get("evidence", "")).strip(),
            confidence=max(0.0, min(1.0, confidence)),
            source="llm",
        ))
    return candidates


def _trim(text: str, limit: int) -> str:
    value = str(text or "")
    if len(value) <= limit:
        return value
    return value[:limit].rstrip() + f"... ({len(value) - limit} chars omitted)"
