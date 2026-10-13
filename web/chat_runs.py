"""Authenticated background chat transport; connections never own execution."""
from __future__ import annotations

from http import HTTPStatus
import logging
from urllib.parse import parse_qs, unquote

from user_scope import parse_web_session_id


logger = logging.getLogger(__name__)
TERMINAL_RUN_STATUSES = frozenset({"completed", "stopped", "failed", "interrupted"})


class ChatRunsHttpMixin:
    def _handle_chat_runs(self, parsed, *, mutation: bool = False) -> None:
        """Only called after RequestHandler authentication."""
        from web.agent_service import (
            RunCapacityError, RunConflictError, RunNotFoundError,
            RunServiceUnavailableError,
        )

        try:
            user = self._current_user()
            prefix = "/api/chat/runs"
            suffix = parsed.path[len(prefix):]
            if mutation and not self._require_json_same_origin():
                return
            if not suffix:
                if mutation:
                    args = self._chat_run_arguments(self._read_json_body())
                    run = self.agent_service.submit(**args)
                    self._send_json({"run": run}, status=HTTPStatus.ACCEPTED,
                                    headers={"Cache-Control": "no-store"})
                else:
                    params = parse_qs(parsed.query)
                    session_id = _single_parameter(params, "session_id")
                    self._send_json({"runs": self.agent_service.list_runs(
                        user_id=user.user_id, session_id=session_id or None,
                    )}, headers={"Cache-Control": "no-store"})
                return
            parts = suffix.strip("/").split("/")
            if not parts or not parts[0] or len(parts) > 2:
                raise RunNotFoundError("Run not found")
            run_id = unquote(parts[0])
            # Service lookup validates identity and authorization before any
            # response headers or content are sent.
            run = self.agent_service.get_run(run_id, user_id=user.user_id)
            action = parts[1] if len(parts) == 2 else ""
            if mutation:
                if action != "cancel":
                    raise RunNotFoundError("Run operation not found")
                payload = self._read_json_body()
                if payload:
                    raise ValueError("Cancel does not accept request fields")
                self._send_json({"run": self.agent_service.cancel_run(
                    run_id, user_id=user.user_id,
                )}, headers={"Cache-Control": "no-store"})
            elif action == "events":
                params = parse_qs(parsed.query)
                after = int(_single_parameter(params, "after") or "0")
                if after < 0:
                    raise ValueError("after must be a nonnegative event cursor")
                if after > int(run.get("last_seq", 0)):
                    raise ValueError("after is beyond the latest event cursor")
                self._stream_background_run(run, after=after, user_id=user.user_id)
            elif not action:
                self._send_json({"run": run}, headers={"Cache-Control": "no-store"})
            else:
                raise RunNotFoundError("Run operation not found")
        except RunNotFoundError as exc:
            self._send_json({"error": str(exc)}, status=HTTPStatus.NOT_FOUND)
        except RunConflictError as exc:
            self._send_json({"error": str(exc)}, status=HTTPStatus.CONFLICT)
        except RunCapacityError as exc:
            self._send_json({"error": str(exc)}, status=HTTPStatus.TOO_MANY_REQUESTS)
        except (RunServiceUnavailableError, OSError) as exc:
            logger.exception("Background run storage unavailable")
            self._send_json({"error": "Background run service is unavailable", "error_type": type(exc).__name__},
                            status=HTTPStatus.SERVICE_UNAVAILABLE)
        except (ValueError, TypeError) as exc:
            self._send_json({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)

    def _chat_run_arguments(self, payload: dict) -> dict:
        from applications.coding.task_lifecycle import coding_task_options
        from web.server import (
            _safe_storage_path, _thinking_supported, _validated_model_profile,
            _web_storage_id,
        )

        allowed = {"session_id", "message", "request_id", "workspace_root", "attachments",
                   "thinking_enabled", "model_profile", "coding_task"}
        if not isinstance(payload, dict) or set(payload) - allowed:
            raise ValueError("Unknown or invalid chat request fields")
        message = payload.get("message", "")
        request_id = payload.get("request_id", "")
        session_id = payload.get("session_id", "default")
        if not isinstance(message, str) or not message.strip():
            raise ValueError("message is required")
        if len(message) > 1_000_000:
            raise ValueError("message exceeds the supported size")
        if not isinstance(request_id, str) or not request_id.strip() or len(request_id) > 128:
            raise ValueError("A request_id of at most 128 characters is required")
        if not isinstance(session_id, str) or not session_id.strip() or len(session_id) > 256:
            raise ValueError("A valid session_id is required")
        user = self._current_user()
        storage_id = _web_storage_id(session_id.strip(), user.user_id)
        session_id = parse_web_session_id(storage_id)[1]
        workspace_root = payload.get("workspace_root")
        if workspace_root is not None and not isinstance(workspace_root, str):
            raise ValueError("workspace_root must be a string")
        thinking = payload.get("thinking_enabled", False)
        if not isinstance(thinking, bool):
            raise ValueError("thinking_enabled must be a boolean")
        model = _validated_model_profile(payload.get("model_profile", ""))
        if thinking and not _thinking_supported(model):
            raise ValueError("thinking_enabled is unavailable for configured model profiles")
        task = payload.get("coding_task")
        # Hybrid routes which select Coding also get isolation; this metadata
        # selects its workspace contract, not the conversation's agent mode.
        task = dict(task) if isinstance(task, dict) else task
        if task is None:
            task = {}
        coding_task_options(task)
        if task.get("mode") in {"continue", "branch"} and workspace_root:
            raise ValueError("A continued or branched task uses its existing workspace binding")
        attachments = payload.get("attachments", [])
        if (not isinstance(attachments, list) or len(attachments) > 5
                or not all(isinstance(item, str) and item for item in attachments)):
            raise ValueError("attachments must contain at most 5 storage paths")
        for item in attachments:
            path = _safe_storage_path(item, user_id=user.user_id)
            if not path.is_file() or path.is_symlink():
                raise ValueError("An attachment is missing or unreadable")
        args = dict(session_id=session_id, content=message.strip(), request_id=request_id.strip(),
                    user_id=user.user_id, user_role=user.role,
                    thinking_enabled=thinking, model_profile=model, coding_task=task)
        if workspace_root and workspace_root.strip():
            from runtime.workspace import WorkspaceResolver
            # Check user-supplied repository paths before accepting work.
            args["workspace_root"] = str(WorkspaceResolver().resolve(workspace_root).root)
        if attachments:
            args["attachment_paths"] = attachments
        return args

    def _stream_background_run(self, run: dict, *, after: int, user_id: str) -> None:
        from web.server import read_session, _stream_event_projection

        run_id = run["run_id"]
        self._send_stream_headers()
        cursor = after
        try:
            while True:
                events = self.agent_service.events_after(
                    run_id, user_id=user_id, after=cursor, wait_seconds=10,
                )
                for event in events:
                    event = dict(event)
                    data = event.pop("data", {})
                    if event.get("type") == "event":
                        projected = _stream_event_projection(data) or {}
                        if projected.get("run_id") != run_id:
                            projected["source_run_id"] = projected.pop("run_id", None)
                        event = {**projected, **event}
                    elif event.get("type") in {"assistant_segment", "assistant_completed"} and isinstance(data, dict):
                        event = {**data, **event}
                    if event.get("type") == "complete" and "session" not in event:
                        try:
                            event["session"] = read_session(
                                run["session_id"], user_id=user_id, message_limit=40,
                            )
                        except Exception:
                            # The durable reply is still delivered if the
                            # session projection is temporarily unavailable.
                            logger.exception("Could not read completed chat projection")
                    self._send_stream_event(event)
                    cursor = int(event["seq"])
                latest = self.agent_service.get_run(run_id, user_id=user_id)
                if latest["status"] in TERMINAL_RUN_STATUSES and cursor >= int(latest["last_seq"]):
                    return
                if not events:
                    self._send_stream_event({"type": "heartbeat", "run_id": run_id,
                                             "session_id": run["session_id"]})
        except (BrokenPipeError, ConnectionResetError):
            # A subscription is not an execution lease. Explicit cancel is the
            # only browser operation that stops a background run.
            return
        except Exception:
            logger.exception("Background run subscription failed")
            # Headers are already sent. Close; clients resume from their last
            # acknowledged cursor rather than receiving a second HTTP response.
            return

    def _require_json_same_origin(self) -> bool:
        from urllib.parse import urlparse

        media_type = self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
        if media_type != "application/json":
            self._send_json({"error": "Content-Type must be application/json"},
                            status=HTTPStatus.UNSUPPORTED_MEDIA_TYPE)
            return False
        origin = self.headers.get("Origin", "")
        try:
            value = urlparse(origin)
            same_origin = not origin or (
                value.scheme in {"http", "https"}
                and value.netloc.lower() == self.headers.get("Host", "").lower()
            )
        except ValueError:
            same_origin = False
        if self.headers.get("Sec-Fetch-Site") == "cross-site" or not same_origin:
            self._send_json({"error": "A same-origin request is required"}, status=HTTPStatus.FORBIDDEN)
            return False
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length < 0 or length > 8 * 1024 * 1024:
                raise ValueError
        except ValueError:
            self._send_json({"error": "Invalid request body size"}, status=HTTPStatus.BAD_REQUEST)
            return False
        return True


def _single_parameter(params: dict, name: str) -> str:
    values = params.get(name, [])
    if len(values) > 1:
        raise ValueError(f"Only one {name} parameter is allowed")
    return values[0] if values else ""
