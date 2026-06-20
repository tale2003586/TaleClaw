from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from typing import Any

from user_scope import normalize_user_id, normalize_user_role


_SAFE_USER_ID_CHARS = re.compile(r"[^a-zA-Z0-9_.-]+")


@dataclass(frozen=True)
class FeishuIdentity:
    user_id: str
    role: str


class FeishuIdentityResolver:
    def __init__(
        self,
        *,
        allowed_open_ids: set[str] | None = None,
        allow_any: bool = False,
        user_map: dict[str, FeishuIdentity] | None = None,
    ) -> None:
        self.allowed_open_ids = allowed_open_ids or set()
        self.allow_any = bool(allow_any)
        self.user_map = user_map or {}

    @classmethod
    def from_env(cls) -> "FeishuIdentityResolver":
        allowed_open_ids, allow_any = _parse_allowed_open_ids(
            os.environ.get("FEISHU_ALLOWED_OPEN_IDS", "")
        )
        return cls(
            allowed_open_ids=allowed_open_ids,
            allow_any=allow_any,
            user_map=_parse_user_map(os.environ.get("FEISHU_USER_MAP", "")),
        )

    def resolve(self, feishu_open_id: str | None) -> FeishuIdentity | None:
        open_id = str(feishu_open_id or "").strip()
        if not open_id:
            return None
        if open_id in self.user_map:
            return self.user_map[open_id]
        if open_id in self.allowed_open_ids or self.allow_any:
            return FeishuIdentity(
                user_id=normalize_user_id(_default_user_id(open_id)),
                role="user",
            )
        return None


def _parse_allowed_open_ids(value: str) -> tuple[set[str], bool]:
    allowed = set()
    allow_any = False
    for item in str(value or "").split(","):
        cleaned = item.strip()
        if not cleaned:
            continue
        if cleaned == "*":
            allow_any = True
            continue
        allowed.add(cleaned)
    return allowed, allow_any


def _parse_user_map(value: str) -> dict[str, FeishuIdentity]:
    raw = str(value or "").strip()
    if not raw:
        return {}
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    if not isinstance(payload, dict):
        return {}
    result = {}
    for open_id, raw_identity in payload.items():
        if not isinstance(raw_identity, dict):
            continue
        try:
            result[str(open_id)] = FeishuIdentity(
                user_id=normalize_user_id(str(raw_identity.get("user_id", ""))),
                role=normalize_user_role(str(raw_identity.get("role", "user"))),
            )
        except ValueError:
            continue
    return result


def _default_user_id(open_id: str) -> str:
    safe = _SAFE_USER_ID_CHARS.sub("_", open_id).strip("._-")
    candidate = f"feishu_{safe}"[:64]
    if not candidate or not candidate[0].isalnum():
        candidate = "feishu_user"
    return candidate
