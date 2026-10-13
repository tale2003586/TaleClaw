import json
import os
import time
from dataclasses import dataclass, asdict
from models.provider import ModelProtocolError
from runtime.execution.budget import ExecutionCancelled, ExecutionBudgetExceeded
from typing import Any


class HybridModeClassifier:
    def __init__(self, *, provider, model: str) -> None:
        self.provider = provider
        self.model = model

    def should_use_coding(self, user_text: str) -> bool:
        """Compatibility adapter; no shared mutable last_result."""
        result = self.classify(user_text)
        return result.valid and result.label == "coding"

    def classify(self, user_text: str) -> "RouteDecision":
        started = time.perf_counter()
        text = str(user_text).strip()
        visible = text[:4000]
        model = os.environ.get("HYBRID_ROUTE_MODEL") or self.model
        response = None
        label = "bot"
        valid = False
        fallback = "empty_input" if not text else ""
        error_type = ""
        if not text:
            return RouteDecision(label=label, valid=False, fallback_reason=fallback,
                                 model=model, input_chars=0, visible_chars=0)
        try:
            response = self.provider.chat(
                model=model,
                messages=[
                    {
                        "role": "system",
                        "content": (
                            "Classify whether a user request should enter an isolated "
                            "coding task session. Return JSON only: "
                            '{"mode":"coding|bot","reason":"short explanation"}. '
                            "Choose coding only when the user asks to inspect, modify, "
                            "debug, run, test, deploy, or reason concretely about code, "
                            "files, terminals, Git, or a software project. Choose bot "
                            "for general conversation, conceptual explanations, writing, "
                            "brainstorming, or questions that merely mention technical words."
                        ),
                    },
                    {
                        "role": "user",
                        "content": visible,
                    },
                ],
                tools=[],
                tool_choice="none",
                max_tokens=max(
                    60,
                    int(os.environ.get("HYBRID_ROUTE_MAX_TOKENS", "160")),
                ),
            )
            payload = _extract_json_object(response.content or "")
            value = payload.get("mode")
            if not isinstance(value, str) or value not in {"coding", "bot"}:
                raise ValueError("Hybrid route mode must be exactly coding or bot.")
            label, valid = value, True
        except (ExecutionCancelled, ExecutionBudgetExceeded):
            raise
        except Exception as exc:
            fallback = "invalid_output" if isinstance(exc, (ValueError, ModelProtocolError)) else "provider_error"
            error_type = type(exc).__name__
        usage = getattr(response, "usage", None)
        metadata = getattr(response, "provider_metadata", None) or {}
        return RouteDecision(
            label=label, valid=valid, fallback_reason=fallback, error_type=error_type,
            model=str(metadata.get("selected_model") or model),
            request_id=str(metadata.get("request_id") or ""),
            elapsed_ms=round((time.perf_counter()-started)*1000, 3),
            input_chars=len(text), visible_chars=len(visible),
            input_tokens=getattr(usage, "input_tokens", None),
            output_tokens=getattr(usage, "output_tokens", None),
            total_tokens=getattr(usage, "total_tokens", None),
        )


def _extract_json_object(text: str) -> dict[str, Any]:
    text = text.strip()
    if text.startswith("```") and text.endswith("```"):
        lines = text.splitlines()
        text = "\n".join(lines[1:-1]).strip()
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end < start:
        raise ValueError("Hybrid route classifier response did not contain JSON.")
    payload = json.loads(text[start:end + 1])
    if not isinstance(payload, dict):
        raise ValueError("Hybrid route classifier response must be an object.")
    return payload


@dataclass(frozen=True)
class RouteDecision:
    label: str
    valid: bool
    fallback_reason: str = ""
    error_type: str = ""
    model: str = ""
    request_id: str = ""
    elapsed_ms: float = 0.0
    input_chars: int = 0
    visible_chars: int = 0
    input_tokens: int | None = None
    output_tokens: int | None = None
    total_tokens: int | None = None
    schema_version: str = "route_decision.v1"
    prompt_version: str = "hybrid-json-v1-prefix4000"

    def to_dict(self):
        data = asdict(self)
        data["input_truncated"] = self.visible_chars < self.input_chars
        data["usage_status"] = "complete" if all(value is not None for value in
            (self.input_tokens, self.output_tokens, self.total_tokens)) else "unknown"
        return data
