import time

from applications.minecraft.api import CreateMinecraftTaskRequest, MinecraftApi
from applications.minecraft.application import MinecraftApplication
from applications.minecraft.service import MinecraftTaskService
from applications.minecraft.stores.memory import InMemoryMinecraftTaskStore
from runtime.cancellation import CancellationRegistry
from tests.minecraft.fakes import FakeBridge


def test_programmatic_api_runs_without_agent_loop():
    bridge = FakeBridge(available={"oak_log": 8})
    service = MinecraftTaskService(
        store=InMemoryMinecraftTaskStore(),
        bridge=bridge,
        cancellations=CancellationRegistry(),
    )
    application = MinecraftApplication(service=service)
    api = MinecraftApi(application)
    try:
        task = api.create(
            CreateMinecraftTaskRequest(
                user_id="api",
                session_id="api:test",
                bot_id="test-bot",
                resource="oak_log",
                quantity=4,
            )
        )
        deadline = time.monotonic() + 2
        status = api.status(task.task_id, user_id="api", session_id="api:test")
        while not status.status.terminal and time.monotonic() < deadline:
            time.sleep(0.01)
            status = api.status(task.task_id, user_id="api", session_id="api:test")
        assert status.status.value == "succeeded"
        assert status.net_acquired == 4
    finally:
        application.close()
