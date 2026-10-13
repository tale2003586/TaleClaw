import asyncio
import threading
import unittest
from types import SimpleNamespace

from applications.app_runtime import AppRuntime
from gateway.feishu.adapter import FeishuGateway
from gateway.telegram.client import TelegramBotApiClient
from models.provider import OpenAICompatibleProvider
from web.agent_service import AgentService


class ModelError(RuntimeError):
    pass


class AsyncRuntimeLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_app_runtime_stop_is_idempotent_and_awaits_dispatcher(self) -> None:
        entered = asyncio.Event()
        finalized = asyncio.Event()

        class Bus:
            async def dispatch_outbound(self):
                entered.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    finalized.set()

            def stop(self):
                pass

        runtime = AppRuntime(bus=Bus(), coordinator=SimpleNamespace())
        runtime.start()
        await entered.wait()

        await runtime.stop()
        await runtime.stop()

        self.assertTrue(finalized.is_set())
        self.assertIsNone(runtime._dispatch_task)

    async def test_app_runtime_closes_its_session_manager_once(self) -> None:
        class Bus:
            def stop(self):
                pass

        class Sessions:
            def __init__(self):
                self.close_count = 0

            def close(self):
                self.close_count += 1

        sessions = Sessions()
        runtime = AppRuntime(
            bus=Bus(),
            coordinator=SimpleNamespace(),
            services=SimpleNamespace(session_manager=sessions),
        )

        await runtime.stop()
        await runtime.stop()

        self.assertEqual(1, sessions.close_count)

    async def test_session_manager_close_failure_does_not_block_shutdown(self) -> None:
        class Bus:
            def __init__(self):
                self.entered = asyncio.Event()
                self.finalized = asyncio.Event()

            async def dispatch_outbound(self):
                self.entered.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    self.finalized.set()

            def stop(self):
                pass

        bus = Bus()
        runtime = AppRuntime(
            bus=bus,
            coordinator=SimpleNamespace(),
            services=SimpleNamespace(
                session_manager=SimpleNamespace(
                    close=lambda: (_ for _ in ()).throw(RuntimeError("close failed"))
                )
            ),
        )

        runtime.start()
        await bus.entered.wait()
        await runtime.stop()

        self.assertTrue(runtime._closed)
        self.assertTrue(bus.finalized.is_set())

    async def test_feishu_close_cancels_and_awaits_event_tasks_once(self) -> None:
        started = asyncio.Event()
        finalized = asyncio.Event()

        class Runtime:
            def __init__(self):
                self.stop_count = 0

            async def stop(self):
                self.stop_count += 1

        class Resource:
            def __init__(self):
                self.close_count = 0

            def close(self):
                self.close_count += 1

            def mark_event_seen(self, _event_id):
                return True

        class Client:
            def __init__(self):
                self.close_count = 0

            async def close(self):
                self.close_count += 1

        runtime = Runtime()
        store = Resource()
        client = Client()
        gateway = FeishuGateway(
            runtime=runtime,
            client=client,
            identities=SimpleNamespace(),
            store=store,
        )

        async def event_worker(_payload):
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                finalized.set()

        gateway._handle_message_event = event_worker
        await gateway.handle_callback({
            "header": {
                "event_id": "event-1",
                "event_type": "im.message.receive_v1",
            }
        })
        await started.wait()

        await gateway.close()
        await gateway.close()

        self.assertTrue(finalized.is_set())
        self.assertFalse(gateway._background_tasks)
        self.assertEqual(1, runtime.stop_count)
        self.assertEqual(1, store.close_count)
        self.assertEqual(1, client.close_count)

    async def test_owned_http_client_close_is_idempotent(self) -> None:
        client = TelegramBotApiClient("test-token")

        await client.close()
        await client.close()

        self.assertTrue(client._client.is_closed)


class AgentServiceLifecycleTests(unittest.TestCase):
    def test_stop_confirms_real_worker_exit_and_is_idempotent(self) -> None:
        import json
        from pathlib import Path
        import tempfile
        import time
        with tempfile.TemporaryDirectory() as root:
            service = AgentService(root=root, worker_target="tests.run_worker_fakes:run_fake")
            run = service.submit(session_id="a", content="wait:" + str(Path(root) / "release"),
                                 user_id="local", request_id="first")
            try:
                deadline = time.monotonic() + 5
                while not any(event["type"] == "delta" for event in service.events_after(run["run_id"], user_id="local")):
                    self.assertLess(time.monotonic(), deadline)
                    time.sleep(0.01)
            finally:
                service.stop()
                service.stop()
            self.assertFalse(service._thread.is_alive())
            self.assertFalse(service._workers)
            persisted = json.loads((Path(root) / run["run_id"] / "run_state.json").read_text())
            self.assertEqual("stopped", persisted["status"])
            self.assertFalse(persisted["metadata"]["background_execution"]["quarantined"])
            replacement = AgentService(root=root, worker_target="tests.run_worker_fakes:run_fake")
            try:
                replacement.ensure_started()  # OS ownership was released only after cleanup.
            finally:
                replacement.stop()

    def test_worker_failure_keeps_primary_error_and_supervisor_available(self) -> None:
        import tempfile
        import time
        with tempfile.TemporaryDirectory() as root:
            service = AgentService(root=root, worker_target="tests.run_worker_fakes:run_fake")
            try:
                run = service.submit(session_id="a", content="error", user_id="local", request_id="bad")
                deadline = time.monotonic() + 5
                while True:
                    state = service.get_run(run["run_id"], user_id="local")
                    if state["status"] == "failed":
                        break
                    self.assertLess(time.monotonic(), deadline)
                    time.sleep(0.01)
                self.assertIn("fake primary model failure", state["error"])
                self.assertEqual("reply:hello", service.ask(session_id="a", content="hello", user_id="local"))
            finally:
                service.stop()


class _Stream:
    def __init__(self, chunks, *, close_error=None):
        self._chunks = iter(chunks)
        self.close_error = close_error
        self.closed = False

    def __iter__(self):
        return self

    def __next__(self):
        return next(self._chunks)

    def close(self):
        self.closed = True
        if self.close_error is not None:
            raise self.close_error


class ProviderStreamingLifecycleTests(unittest.TestCase):
    @staticmethod
    def _provider(stream):
        completions = SimpleNamespace(create=lambda **_kwargs: stream)
        client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
        return OpenAICompatibleProvider(client)

    def test_stream_callback_failure_closes_upstream(self) -> None:
        chunk = SimpleNamespace(
            choices=[SimpleNamespace(
                delta=SimpleNamespace(content="partial", tool_calls=[])
            )]
        )
        stream = _Stream([chunk])
        provider = self._provider(stream)

        with self.assertRaisesRegex(ModelError, "model failed"):
            provider.stream_chat(
                messages=[],
                tools=[],
                model="test",
                max_tokens=10,
                on_text=lambda _text: (_ for _ in ()).throw(ModelError("model failed")),
            )

        self.assertTrue(stream.closed)

    def test_stream_cleanup_error_does_not_replace_primary_error(self) -> None:
        chunk = SimpleNamespace(
            choices=[SimpleNamespace(
                delta=SimpleNamespace(content="partial", tool_calls=[])
            )]
        )
        stream = _Stream(
            [chunk],
            close_error=RuntimeError("secondary close failure"),
        )
        provider = self._provider(stream)

        with self.assertRaisesRegex(ModelError, "first model failure"):
            provider.stream_chat(
                messages=[],
                tools=[],
                model="test",
                max_tokens=10,
                on_text=lambda _text: (_ for _ in ()).throw(
                    ModelError("first model failure")
                ),
            )

        self.assertTrue(stream.closed)


if __name__ == "__main__":
    unittest.main()
