from __future__ import annotations

import threading


class CancellationToken:
    def __init__(self, scope_id: str) -> None:
        self._scope_id = str(scope_id)
        self._event = threading.Event()

    @property
    def scope_id(self) -> str:
        return self._scope_id

    def request(self) -> None:
        self._event.set()

    def requested(self) -> bool:
        return self._event.is_set()


class CancellationRegistry:
    def __init__(self) -> None:
        self._tokens: dict[str, CancellationToken] = {}
        self._lock = threading.RLock()

    def register(self, scope_id: str) -> CancellationToken:
        key = self._key(scope_id)
        with self._lock:
            token = self._tokens.get(key)
            if token is None:
                token = CancellationToken(key)
                self._tokens[key] = token
            return token

    def token(self, scope_id: str) -> CancellationToken | None:
        key = self._key(scope_id)
        with self._lock:
            return self._tokens.get(key)

    def request(self, scope_id: str) -> bool:
        token = self.token(scope_id)
        if token is None:
            return False
        token.request()
        return True

    def requested(self, scope_id: str) -> bool:
        token = self.token(scope_id)
        return bool(token and token.requested())

    def release(self, scope_id: str) -> bool:
        key = self._key(scope_id)
        with self._lock:
            return self._tokens.pop(key, None) is not None

    @staticmethod
    def _key(scope_id: str) -> str:
        key = str(scope_id or "").strip()
        if not key:
            raise ValueError("cancellation scope_id is required")
        return key
