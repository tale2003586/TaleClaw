import asyncio
import threading
import unittest
from types import SimpleNamespace

from applications.app_runtime import AppRuntime
from gateway.feishu.adapter import FeishuGateway
from gateway.telegram.client import TelegramBotApiClient
from models.provider import OpenAICompatibleProvider
from web.server import AgentService


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
    def test_stop_cancels_request_and_waits_for_finally(self) -> None:
        request_started = threading.Event()
        request_finalized = threading.Event()

        class Runtime:
            def __init__(self):
                self.coordinator = SimpleNamespace(
                    sessions=SimpleNamespace(close=lambda: None)
                )

            def start(self):
                pass

            async def stop(self):
                pass

        class Service(AgentService):
            async def _start_async(self):
                self._runtime = Runtime()
                self._session_locks = {}
                self._runtime.start()

        async def request():
            request_started.set()
            try:
                await asyncio.Event().wait()
            finally:
                request_finalized.set()

        service = Service()
        service.ensure_started()
        future = service._submit(request())
        self.assertTrue(request_started.wait(timeout=2))

        service.stop()
        service.stop()

        self.assertTrue(request_finalized.is_set())
        self.assertTrue(future.done())
        self.assertFalse(service._thread.is_alive())
        self.assertIsNone(service._loop)

    def test_startup_error_is_not_replaced_by_cleanup_error(self) -> None:
        class Service(AgentService):
            async def _start_async(self):
                raise ModelError("first model failure")

            async def _stop_async(self):
                raise RuntimeError("secondary cleanup failure")

        service = Service()

        with self.assertRaises(RuntimeError) as caught:
            service.ensure_started()

        self.assertIsInstance(caught.exception.__cause__, ModelError)
        self.assertEqual("first model failure", str(caught.exception.__cause__))


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
