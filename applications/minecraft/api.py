from __future__ import annotations

from dataclasses import dataclass

from .application import MinecraftApplication
from .models import ResourceGoal


@dataclass(frozen=True)
class CreateMinecraftTaskRequest:
    user_id: str
    session_id: str
    bot_id: str
    resource: str
    quantity: int
    idempotency_key: str | None = None


class MinecraftApi:
    def __init__(self, application: MinecraftApplication) -> None:
        self.application = application

    def create(self, request: CreateMinecraftTaskRequest):
        return self.application.start_task(
            goal=ResourceGoal(
                resource=request.resource,
                quantity=request.quantity,
            ),
            user_id=request.user_id,
            session_id=request.session_id,
            bot_id=request.bot_id,
            idempotency_key=request.idempotency_key,
        )

    def status(self, task_id: str, *, user_id: str, session_id: str):
        return self.application.get_status(
            task_id,
            user_id=user_id,
            session_id=session_id,
        )

    def cancel(self, task_id: str, *, user_id: str, session_id: str) -> bool:
        return self.application.cancel_task(
            task_id,
            user_id=user_id,
            session_id=session_id,
        )
