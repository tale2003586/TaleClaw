from __future__ import annotations

import json
import os

from applications.minecraft.models import ResourceGoal
from applications.minecraft.parser import parse_resource_goal
from plugins.base import Plugin, ToolRegistration, TurnContext, TurnResult


class MinecraftPlugin(Plugin):
    name = "minecraft"

    def __init__(self, application, *, session_adapter=None, bot_id: str | None = None) -> None:
        self.application = application
        self.session_adapter = session_adapter
        self.bot_id = bot_id or os.getenv("MINECRAFT_BOT_USERNAME", "TaleClawBot")

    def tools(self) -> list[ToolRegistration]:
        return [
            ToolRegistration(
                schema=_schema(
                    "minecraft_start_task",
                    "Create one Minecraft resource task.",
                    {
                        "resource": {"type": "string"},
                        "quantity": {"type": "integer", "minimum": 1},
                    },
                    ["resource", "quantity"],
                ),
                handler=self._start,
                allowed_agents={"minecraft"},
                session_scoped=True,
            ),
            ToolRegistration(
                schema=_schema(
                    "minecraft_get_status",
                    "Get the current Minecraft task status.",
                    {"task_id": {"type": "string"}},
                    [],
                ),
                handler=self._status,
                allowed_agents={"minecraft"},
                session_scoped=True,
                risk="low",
            ),
            ToolRegistration(
                schema=_schema(
                    "minecraft_cancel_task",
                    "Cancel the current Minecraft task and active game action.",
                    {"task_id": {"type": "string"}},
                    [],
                ),
                handler=self._cancel,
                allowed_agents={"minecraft"},
                session_scoped=True,
            ),
            ToolRegistration(
                schema=_schema(
                    "minecraft_get_bot_status",
                    "Get the configured Minecraft bot identity and active task.",
                    {},
                    [],
                ),
                handler=self._bot_status,
                allowed_agents={"minecraft"},
                session_scoped=True,
                risk="low",
            ),
        ]

    def before_turn(self, context: TurnContext) -> TurnResult | None:
        text = str(context.inbound.content or "").strip()
        if not text.startswith("/minecraft "):
            return None
        try:
            goal = parse_resource_goal(text)
            task = self._create(goal, context.session)
            return TurnResult(
                abort=True,
                reply=(
                    f"Minecraft 任务已创建：{task.task_id}；"
                    f"目标为净新增 {task.goal.quantity} 个 {task.goal.resource}。"
                ),
            )
        except Exception as exc:
            return TurnResult(abort=True, reply=f"Minecraft 任务创建失败：{exc}")

    def _start(self, resource: str, quantity: int, _session=None) -> str:
        goal = ResourceGoal(resource=resource, quantity=quantity)
        return _json(self._create(goal, _session))

    def _status(self, task_id: str = "", _session=None) -> str:
        resolved = task_id or self._session_task_id(_session)
        task = self.application.get_status(
            resolved,
            user_id=self._user_id(_session),
            session_id=self._session_id(_session),
        )
        return _json(task)

    def _cancel(self, task_id: str = "", _session=None) -> str:
        resolved = task_id or self._session_task_id(_session)
        cancelled = self.application.cancel_task(
            resolved,
            user_id=self._user_id(_session),
            session_id=self._session_id(_session),
        )
        return json.dumps(
            {"task_id": resolved, "cancel_requested": cancelled},
            ensure_ascii=False,
        )

    def _bot_status(self, _session=None) -> str:
        task_id = self._session_task_id(_session, required=False)
        payload = {"bot_id": self.bot_id, "task_id": task_id}
        if task_id:
            payload["task"] = self.application.get_status(
                task_id,
                user_id=self._user_id(_session),
                session_id=self._session_id(_session),
            ).model_dump(mode="json")
        return json.dumps(payload, ensure_ascii=False)

    def _create(self, goal: ResourceGoal, session):
        task = self.application.start_task(
            goal=goal,
            user_id=self._user_id(session),
            session_id=self._session_id(session),
            bot_id=self.bot_id,
            idempotency_key=(
                f"{self._session_id(session)}:{goal.resource}:{goal.quantity}"
            ),
        )
        if self.session_adapter is not None:
            self.session_adapter.bind_task(self._session_id(session), task.task_id)
        elif session is not None:
            session.metadata["minecraft_task_id"] = task.task_id
        return task

    def _session_task_id(self, session, *, required: bool = True) -> str:
        if self.session_adapter is not None:
            value = self.session_adapter.task_id(self._session_id(session))
        else:
            value = (getattr(session, "metadata", {}) or {}).get("minecraft_task_id")
        if not value and required:
            raise ValueError("当前会话没有 Minecraft 任务")
        return str(value or "")

    @staticmethod
    def _session_id(session) -> str:
        value = str(getattr(session, "id", "") or "")
        if not value:
            raise ValueError("Minecraft tool requires a session")
        return value

    @staticmethod
    def _user_id(session) -> str:
        metadata = getattr(session, "metadata", {}) or {}
        return str(metadata.get("user_id") or "anonymous")


def _schema(name: str, description: str, properties: dict, required: list[str]) -> dict:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {
                "type": "object",
                "additionalProperties": False,
                "properties": properties,
                "required": required,
            },
        },
    }


def _json(value) -> str:
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json")
    return json.dumps(value, ensure_ascii=False)
