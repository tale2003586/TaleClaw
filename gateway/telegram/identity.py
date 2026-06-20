from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Any, Mapping

from user_scope import normalize_user_id, normalize_user_role


@dataclass(frozen=True)
class TelegramIdentity:
    telegram_user_id: int
    user_id: str
    role: str


class TelegramIdentityResolver:
    """Maps Telegram accounts to isolated taleclaw users."""

    def __init__(
        self,
        *,
        allowed_user_ids: set[int] | None = None,
        user_map: Mapping[int, TelegramIdentity] | None = None,
        allow_all: bool = False,
    ) -> None:
        self.allowed_user_ids = set(allowed_user_ids or set())
        self.user_map = dict(user_map or {})
        self.allow_all = bool(allow_all)

    @classmethod
    def from_env(cls) -> "TelegramIdentityResolver":
        allowed_user_ids, allow_all = _parse_allowed_user_ids(
            os.environ.get("TELEGRAM_ALLOWED_USER_IDS", "")
        )
        return cls(
            allowed_user_ids=allowed_user_ids,
            user_map=_parse_user_map(os.environ.get("TELEGRAM_USER_MAP", "")),
            allow_all=allow_all,
        )

    def resolve(self, telegram_user_id: int | str) -> TelegramIdentity | None:
        external_id = _positive_int(telegram_user_id, "Telegram user ID")
        mapped = self.user_map.get(external_id)
        if mapped is not None:
            return mapped
        if not self.allow_all and external_id not in self.allowed_user_ids:
            return None
        return TelegramIdentity(
            telegram_user_id=external_id,
            user_id=f"telegram_{external_id}",
            role="user",
        )


def _parse_allowed_user_ids(value: str) -> tuple[set[int], bool]:
    allowed: set[int] = set()
    allow_all = False
    for item in str(value or "").split(","):
        cleaned = item.strip()
        if not cleaned:
            continue
        if cleaned == "*":
            allow_all = True
            continue
        allowed.add(_positive_int(cleaned, "TELEGRAM_ALLOWED_USER_IDS entry"))
    return allowed, allow_all


def _parse_user_map(value: str) -> dict[int, TelegramIdentity]:
    cleaned = str(value or "").strip()
    if not cleaned:
        return {}
    try:
        raw = json.loads(cleaned)
    except json.JSONDecodeError as exc:
        raise ValueError("TELEGRAM_USER_MAP must be valid JSON.") from exc
    if not isinstance(raw, dict):
        raise ValueError("TELEGRAM_USER_MAP must be a JSON object.")

    identities: dict[int, TelegramIdentity] = {}
    for external_id_text, entry in raw.items():
        external_id = _positive_int(external_id_text, "TELEGRAM_USER_MAP key")
        if isinstance(entry, str):
            user_id = normalize_user_id(entry)
            role = "user"
        elif isinstance(entry, dict):
            user_id = normalize_user_id(str(entry.get("user_id", "")))
            role = normalize_user_role(str(entry.get("role", "user")))
        else:
            raise ValueError(
                "TELEGRAM_USER_MAP values must be user ID strings or JSON objects."
            )
        identities[external_id] = TelegramIdentity(
            telegram_user_id=external_id,
            user_id=user_id,
            role=role,
        )
    return identities


def _positive_int(value: Any, label: str) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} must be an integer.") from exc
    if parsed <= 0:
        raise ValueError(f"{label} must be positive.")
    return parsed
