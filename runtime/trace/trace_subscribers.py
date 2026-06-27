from __future__ import annotations

import json
import threading
from typing import Any, Callable


class TraceSubscribers:
    def __init__(self, lock: threading.RLock) -> None:
        self._lock = lock
        self._subscribers: dict[str, list[Callable[[dict[str, Any]], None]]] = {}
        self._run_to_session: dict[str, str] = {}

    def register_run(self, run_id: str, session_id: str) -> None:
        with self._lock:
            self._run_to_session[run_id] = session_id

    def clear_run(self, run_id: str) -> None:
        with self._lock:
            self._run_to_session.pop(run_id, None)

    def subscribe(
        self,
        session_id: str,
        cb: Callable[[dict[str, Any]], None],
    ) -> Callable[[], None]:
        with self._lock:
            self._subscribers.setdefault(session_id, []).append(cb)

        def _unsub() -> None:
            with self._lock:
                subscribers = self._subscribers.get(session_id, [])
                if cb in subscribers:
                    subscribers.remove(cb)
                if not subscribers:
                    self._subscribers.pop(session_id, None)

        return _unsub

    def snapshot(
        self,
        run_id: str,
        fallback_session_id: str,
    ) -> list[Callable[[dict[str, Any]], None]]:
        with self._lock:
            owner = self._run_to_session.get(run_id, fallback_session_id)
            return list(self._subscribers.get(owner, []))

    def dispatch(
        self,
        event: dict[str, Any],
        subscribers: list[Callable[[dict[str, Any]], None]],
    ) -> None:
        if not subscribers:
            return
        subscriber_event = _subscriber_event(event)
        for cb in subscribers:
            try:
                cb(subscriber_event)
            except Exception:
                continue


def _subscriber_event(event: dict[str, Any]) -> dict[str, Any]:
    projected = dict(event)
    payload = event.get("payload")
    if isinstance(payload, dict):
        projected["payload"] = {
            str(key): _subscriber_value(value)
            for key, value in payload.items()
            if not _looks_secret(str(key))
        }
    else:
        projected["payload"] = _subscriber_value(payload)
    return projected


def _subscriber_value(value: Any) -> Any:
    if isinstance(value, str):
        return _preview(value)
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    if isinstance(value, dict):
        text = json.dumps(value, ensure_ascii=False, default=str)
        if len(text) > 500:
            return _preview(value)
        return {
            str(key): _subscriber_value(item)
            for key, item in value.items()
            if not _looks_secret(str(key))
        }
    if isinstance(value, list):
        text = json.dumps(value, ensure_ascii=False, default=str)
        if len(text) > 500:
            return _preview(value)
        return [_subscriber_value(item) for item in value]
    return _preview(value)


def _preview(value: Any, *, limit: int = 500) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        text = value
    else:
        text = json.dumps(value, ensure_ascii=False, default=str)
    if len(text) <= limit:
        return text
    return text[:limit] + "...[truncated]"


def _looks_secret(key: str) -> bool:
    lowered = key.lower()
    return (
        lowered in {"token", "password", "secret", "api_key", "apikey"}
        or lowered.endswith("_token")
        or lowered.endswith("-token")
        or any(
            marker in lowered
            for marker in (
                "api_key",
                "apikey",
                "authorization",
                "access_token",
                "refresh_token",
            )
        )
    )
