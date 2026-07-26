from __future__ import annotations

import asyncio

from applications.minecraft.models import ResourceGoal
from applications.minecraft.service import MinecraftTaskService


async def run_resource_benchmark(
    service: MinecraftTaskService,
    *,
    resource: str,
    quantity: int,
    case_id: str,
) -> dict:
    task = await service.create_task(
        goal=ResourceGoal(resource=resource, quantity=quantity),
        user_id="benchmark",
        session_id=f"benchmark:{case_id}",
        bot_id="benchmark-bot",
        idempotency_key=case_id,
    )
    result = await service.run_to_completion(task.task_id)
    return result.task.model_dump(mode="json")


def run_resource_benchmark_sync(service: MinecraftTaskService, **kwargs) -> dict:
    return asyncio.run(run_resource_benchmark(service, **kwargs))
