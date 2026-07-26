import json
import os
from typing import Any


class HybridModeClassifier:
    def __init__(self, *, provider, model: str) -> None:
        self.provider = provider
        self.model = model

    def should_use_coding(self, user_text: str) -> bool:
        text = str(user_text).strip()
        if not text:
            return False
        try:
            response = self.provider.chat(
                model=os.environ.get("HYBRID_ROUTE_MODEL") or self.model,
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
                        "content": text[:4000],
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
            return str(payload.get("mode", "")).strip().lower() == "coding"
        except Exception:
            return False


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
