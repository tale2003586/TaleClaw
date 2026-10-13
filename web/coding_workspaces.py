"""Owned worktree and fixed-candidate delivery HTTP operations."""
from __future__ import annotations

from http import HTTPStatus
import logging
from urllib.parse import parse_qs, unquote

from runtime.worktrees import WorktreeError, WorktreeService, WorktreeNotFoundError
from runtime.workspace_lease import WorkspaceBusyError
from runtime.worktree_delivery import DeliveryNotFoundError, WorktreeDeliveryService
from web.chat_runs import _single_parameter

logger = logging.getLogger(__name__)


class CodingWorkspacesHttpMixin:
    def _handle_coding_workspaces(self, parsed, *, mutation=False) -> None:
        # RequestHandler authenticates before dispatch. Every record lookup also
        # checks ownership, including administrative users.
        if not self._require_admin():
            return
        owner = self._current_user().user_id
        if mutation and not self._require_json_same_origin():
            return
        try:
            worktrees = getattr(self, "worktree_service", None) or WorktreeService()
            deliveries = WorktreeDeliveryService(worktrees)
            suffix = parsed.path.removeprefix("/api/coding/")
            parts = [unquote(part) for part in suffix.split("/")]
            if not parts or len(parts) > 3 or any(not part or "/" in part for part in parts):
                raise DeliveryNotFoundError("Coding operation not found")
            collection, *rest = parts
            payload = self._read_json_body() if mutation else {}
            if not isinstance(payload, dict):
                raise ValueError("A JSON object is required")
            if collection == "worktrees":
                if not rest and not mutation:
                    result = {"worktrees": worktrees.list(owner_id=owner)}
                elif rest:
                    # Hide existence and owner before invoking a mutation.
                    if rest[0] not in {item["worktree_id"] for item in worktrees.list(owner_id=owner)}:
                        raise DeliveryNotFoundError("Worktree not found")
                    if len(rest) == 1 and not mutation:
                        result = {"worktree": worktrees.inspect(rest[0], owner_id=owner)}
                    elif len(rest) == 2 and mutation and rest[1] in {"checkpoint", "remove"}:
                        _fields(payload, set())
                        if rest[1] == "checkpoint":
                            result = {"checkpoint": deliveries.checkpoint(rest[0], owner_id=owner)}
                        else:
                            result = {"worktree": worktrees.remove(rest[0], owner_id=owner)}
                    else:
                        raise DeliveryNotFoundError("Worktree operation not found")
                else:
                    raise DeliveryNotFoundError("Worktree operation not found")
            elif collection == "deliveries":
                if not rest:
                    if mutation:
                        _fields(payload, {"worktree_id", "target_ref"})
                        identifier = payload.get("worktree_id")
                        if not isinstance(identifier, str) or not identifier:
                            raise ValueError("worktree_id is required")
                        if identifier not in {item["worktree_id"] for item in worktrees.list(owner_id=owner)}:
                            raise DeliveryNotFoundError("Worktree not found")
                        target_ref = payload.get("target_ref")
                        if target_ref is not None and not isinstance(target_ref, str):
                            raise ValueError("target_ref must be a string")
                        result = {"delivery": deliveries.prepare(identifier, owner_id=owner,
                                                                  target_ref=target_ref or None)}
                    else:
                        identifier = _single_parameter(parse_qs(parsed.query), "worktree_id")
                        result = {"deliveries": deliveries.list(owner_id=owner, worktree_id=identifier or None)}
                elif len(rest) == 1 and not mutation:
                    result = {"delivery": deliveries.get(rest[0], owner_id=owner)}
                elif len(rest) == 2 and mutation:
                    # Authorize before parsing commands or doing work.
                    deliveries.get(rest[0], owner_id=owner)
                    if rest[1] == "verify":
                        _fields(payload, {"command", "timeout_seconds"})
                        result = {"delivery": deliveries.verify(rest[0], owner_id=owner,
                            command=payload.get("command"), timeout_seconds=payload.get("timeout_seconds", 120))}
                    elif rest[1] == "publish":
                        _fields(payload, set())
                        result = {"delivery": deliveries.publish(rest[0], owner_id=owner)}
                    else:
                        raise DeliveryNotFoundError("Delivery operation not found")
                else:
                    raise DeliveryNotFoundError("Delivery operation not found")
            else:
                raise DeliveryNotFoundError("Coding operation not found")
            self._send_json(result, headers={"Cache-Control": "no-store"})
        except (DeliveryNotFoundError, WorktreeNotFoundError) as exc:
            self._send_json({"error": str(exc)}, status=HTTPStatus.NOT_FOUND)
        except (WorktreeError, WorkspaceBusyError) as exc:
            self._send_json({"error": str(exc)}, status=HTTPStatus.CONFLICT)
        except (ValueError, TypeError) as exc:
            self._send_json({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
        except OSError:
            logger.exception("Managed workspace storage unavailable")
            self._send_json({"error": "Managed workspace storage is unavailable"},
                            status=HTTPStatus.SERVICE_UNAVAILABLE)


def _fields(payload: dict, allowed: set[str]) -> None:
    if set(payload) - allowed:
        raise ValueError("Unknown request fields")
