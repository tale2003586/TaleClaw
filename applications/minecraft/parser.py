from __future__ import annotations

import re

from .catalog import CATALOG, DomainCatalog
from .models import ResourceGoal


class GoalParseError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


_QUANTITY_RESOURCE = re.compile(
    r"(?P<quantity>-?\d+)\s*(?:个|块|份|颗|组)?\s*(?P<resource>[\w\u4e00-\u9fff ]+)"
)


def parse_resource_goal(
    text: str,
    *,
    catalog: DomainCatalog = CATALOG,
) -> ResourceGoal:
    cleaned = str(text or "").strip()
    if cleaned.startswith("/minecraft"):
        cleaned = cleaned[len("/minecraft") :].strip()
    if not cleaned:
        raise GoalParseError("empty_goal", "需要提供资源名称和正整数数量。")

    numeric_tokens = re.findall(r"-?\d+", cleaned)
    mentioned_resources = {
        resource.resource_id
        for resource in catalog.resources()
        if any(
            alias.lower() in cleaned.lower()
            for alias in (resource.resource_id, *resource.aliases)
        )
    }
    if len(numeric_tokens) > 1 and len(mentioned_resources) > 1:
        raise GoalParseError("multiple_goals", "第一版一次只接受一个资源目标。")

    matches: list[tuple[int, str]] = []
    for match in _QUANTITY_RESOURCE.finditer(cleaned):
        quantity = int(match.group("quantity"))
        resource_text = _strip_action_words(match.group("resource"))
        definition = _best_resource_match(resource_text, catalog)
        if definition is not None:
            matches.append((quantity, definition.resource_id))

    unique = {(quantity, resource) for quantity, resource in matches}
    if len(unique) > 1:
        raise GoalParseError("multiple_goals", "第一版一次只接受一个资源目标。")
    if not unique:
        if re.search(r"-?\d+", cleaned):
            raise GoalParseError("unknown_resource", "无法识别要收集的资源。")
        raise GoalParseError("missing_quantity", "资源任务需要包含正整数数量。")

    quantity, resource = next(iter(unique))
    if quantity <= 0:
        raise GoalParseError("invalid_quantity", "资源数量必须是正整数。")
    return ResourceGoal(
        resource=resource,
        quantity=quantity,
        original_text=text,
    )


def _strip_action_words(value: str) -> str:
    result = value.strip()
    for word in (
        "然后",
        "并且",
        "请",
        "帮我",
        "去",
        "挖",
        "采集",
        "收集",
        "获取",
        "拿",
        "个",
        "块",
        "颗",
    ):
        result = result.replace(word, " ")
    return " ".join(result.split()).strip()


def _best_resource_match(value: str, catalog: DomainCatalog):
    exact = catalog.resolve(value)
    if exact is not None:
        return exact
    lowered = value.lower()
    candidates = []
    for resource in catalog.resources():
        for alias in (resource.resource_id, *resource.aliases):
            if alias.lower() in lowered:
                candidates.append((len(alias), resource))
    if not candidates:
        return None
    return max(candidates, key=lambda item: item[0])[1]
