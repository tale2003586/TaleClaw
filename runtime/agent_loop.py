import inspect
import threading
from typing import Callable

from bus.user_bus import OutboundMessage, MessageBus
from config import WORKING_MEMORY_CHECKPOINT_ENABLED
from runtime.pipeline import Pipeline
from runtime.trace.events import RUN_COMPLETED, RUN_FAILED
from runtime.trace.run_state import RunState
from runtime.trace.trace_store import event_preview
from runtime.routing.router import ModeRouter
from runtime.working_memory import (
    is_resume_request,
    prepare_working_memory_for_turn,
)
from sessions import SessionManager


class AgentLoop:
    def __init__(
        self,
        bus: MessageBus,
        sessions: SessionManager,
        pipeline: Pipeline,
        router: ModeRouter,
        plugin_manager=None,
        task_session_runner=None,
        subagent_runner=None,
        trace_store=None,
    ) -> None:
        self.bus = bus
        self.sessions = sessions
        self.pipeline = pipeline
        self.router = router
        self.plugin_manager = plugin_manager
        self.task_session_runner = task_session_runner
        self.subagent_runner = subagent_runner
        self.trace_store = trace_store
        self._cancel_events: dict[str, threading.Event] = {}
        self._cancel_lock = threading.Lock()

    async def run_once(self, on_text: Callable[[str], None] | None = None) -> None:
        inbound = await self.bus.consume_inbound()
        await self.run_inbound(inbound, on_text=on_text)

    async def run_inbound(
        self,
        inbound,
        on_text: Callable[[str], None] | None = None,
    ) -> None:
        session = self.sessions.get_or_create(inbound.session_key)
        self._begin_cancel_scope(session.id)
        run_state = None

        try:
            run_state = self._receive(session, inbound)
            if await self._preprocess(session, inbound, run_state, on_text):
                return
            route = self._route(session, inbound, run_state)
            if route.switched:
                await self._handle_switch(session, inbound, route, run_state, on_text)
                return
            self._record(session, inbound, run_state)
            reply = self._execute(session, inbound, route, run_state, on_text)
            self._postprocess(session, inbound, reply)
            await self._deliver(session, inbound, reply, route, run_state, on_text)
        except Exception as exc:
            if run_state is not None:
                self._fail_run(run_state, session, exc)
            raise
        finally:
            self._end_cancel_scope(session.id)

    def request_cancel(self, session_id: str) -> bool:
        with self._cancel_lock:
            event = self._cancel_events.get(str(session_id))
            if event is None:
                return False
            event.set()
            return True

    def cancel_requested(self, session_id: str) -> bool:
        with self._cancel_lock:
            event = self._cancel_events.get(str(session_id))
        return bool(event is not None and event.is_set())

    def _receive(self, session, inbound) -> RunState:
        self._apply_inbound_identity(session, inbound)
        return self._start_run(inbound, session)

    async def _preprocess(self, session, inbound, run_state, on_text) -> bool:
        if self.plugin_manager is None:
            return False
        plugin_result = self.plugin_manager.before_turn(inbound, session)
        if not plugin_result.abort:
            return False
        run_state.set_route(
            mode=session.current_mode,
            execution_path="plugin_abort",
            intent="plugin_abort",
        )
        self._trace(run_state, "plugin_aborted_turn", {
            "reply_preview": event_preview(plugin_result.reply),
        })
        self._finish_run(
            run_state,
            session,
            plugin_result.reply,
            report={"execution_path": "plugin_abort"},
        )
        self.sessions.save(session)
        self._emit_text(on_text, plugin_result.reply)
        await self.bus.publish_outbound(OutboundMessage(
            channel=inbound.channel,
            chat_id=inbound.chat_id,
            content=plugin_result.reply,
        ))
        return True

    def _route(self, session, inbound, run_state):
        route = self.router.route(session, inbound.content)
        run_state.set_route(
            mode=session.current_mode,
            execution_path=route.execution,
            intent=route.intent,
            profile=route.profile.name,
        )
        self._trace(run_state, "route_selected", {
            "intent": route.intent,
            "execution": route.execution,
            "profile": route.profile.name,
            "tool_mode": route.profile.tool_mode,
            "confidence": route.confidence,
            "reason": route.reason,
            "switched": route.switched,
        })
        return route

    async def _handle_switch(self, session, inbound, route, run_state, on_text) -> None:
        reply = route.switch_message or ""
        session.add_message(
            "user",
            inbound.content,
            media=inbound.media,
            metadata=self._message_metadata(inbound, run_state),
        )
        session.add_message(
            "assistant",
            reply,
            metadata={
                "kind": "mode_switch",
                "mode": session.current_mode,
                "run_id": run_state.run_id,
            },
        )
        self._finish_run(
            run_state,
            session,
            reply,
            report={"execution_path": "direct_reply"},
        )
        self.sessions.save(session)
        self._emit_text(on_text, reply)
        await self.bus.publish_outbound(OutboundMessage(
            channel=inbound.channel,
            chat_id=inbound.chat_id,
            content=reply,
        ))

    def _record(self, session, inbound, run_state) -> None:
        session.add_message(
            "user",
            inbound.content,
            media=inbound.media,
            metadata=self._message_metadata(inbound, run_state),
        )

    def _execute(self, session, inbound, route, run_state, on_text) -> str:
        if self.subagent_runner is not None:
            session.metadata["subagent_runner_available"] = True
        cancel_requested = lambda: self.cancel_requested(session.id)
        if (
            WORKING_MEMORY_CHECKPOINT_ENABLED
            and route.profile.tool_mode == "coding"
        ):
            prepare_working_memory_for_turn(
                session,
                objective=inbound.content,
                resume_requested=is_resume_request(inbound.content),
                task_id=session.id,
            )
        if self.task_session_runner is not None and route.profile.tool_mode == "coding":
            task_kwargs = {
                "parent_session": session,
                "user_text": inbound.content,
                "profile": route.profile,
            }
            if _accepts_keyword(
                self.task_session_runner.run_coding_task,
                "cancel_requested",
            ):
                task_kwargs["cancel_requested"] = cancel_requested
            workspace_root = (inbound.metadata or {}).get("workspace_root")
            if workspace_root:
                task_kwargs["workspace_root"] = workspace_root
            if self.trace_store is not None:
                task_kwargs.update({
                    "run_state": run_state,
                    "trace_store": self.trace_store,
                })
            reply = self.task_session_runner.run_coding_task(**task_kwargs)
            session.add_message(
                "assistant",
                reply,
                metadata={"run_id": run_state.run_id},
            )
            self._emit_text(on_text, reply)
            return reply
        run_kwargs = {
            "on_text": on_text,
            "run_state": run_state,
            "trace_store": self.trace_store,
        }
        if _accepts_keyword(self.pipeline.run, "cancel_requested"):
            run_kwargs["cancel_requested"] = cancel_requested
        return self.pipeline.run(session, route.profile, **run_kwargs)

    def _postprocess(self, session, inbound, reply) -> None:
        if self.plugin_manager is not None:
            self.plugin_manager.after_turn(inbound, session, reply)

    async def _deliver(self, session, inbound, reply, route, run_state, on_text) -> None:
        self._finish_run(
            run_state,
            session,
            reply,
            report={"execution_path": route.execution},
        )
        self.sessions.save(session)
        await self.bus.publish_outbound(OutboundMessage(
            channel=inbound.channel,
            chat_id=inbound.chat_id,
            content=reply,
        ))

    def _emit_text(
        self,
        on_text: Callable[[str], None] | None,
        content: str,
    ) -> None:
        if on_text is not None and content:
            on_text(content)

    def _apply_inbound_identity(self, session, inbound) -> None:
        metadata = inbound.metadata or {}
        inbound_user_id = metadata.get("user_id")
        session_user_id = session.metadata.get("user_id")
        if (
            inbound_user_id is not None
            and session_user_id is not None
            and inbound_user_id != session_user_id
        ):
            raise ValueError("Inbound user identity does not match the existing session.")
        for key in ("user_id", "user_role"):
            if key in metadata:
                session.metadata[key] = metadata[key]

    def _start_run(self, inbound, session) -> RunState:
        metadata = inbound.metadata or {}
        run_state = RunState.create(
            session_id=session.id,
            channel=inbound.channel,
            chat_id=inbound.chat_id,
            user_id=session.metadata.get("user_id") or metadata.get("user_id"),
            user_role=session.metadata.get("user_role") or metadata.get("user_role"),
            mode=session.current_mode,
            execution_path="routing",
            metadata={
                "sender": inbound.sender,
                "media_count": len(inbound.media or []),
            },
        )
        session.metadata["active_run_id"] = run_state.run_id
        session.metadata["last_run_id"] = run_state.run_id
        if self.trace_store is not None:
            self.trace_store.start_run(run_state)
            self.trace_store.append_event(run_state, "inbound_received", {
                "content_preview": event_preview(inbound.content),
                "metadata": metadata,
            })
        return run_state

    def _finish_run(
        self,
        run_state: RunState,
        session,
        reply: str,
        *,
        report: dict | None = None,
    ) -> None:
        session.metadata.pop("active_run_id", None)
        session.metadata["last_run_id"] = run_state.run_id
        if run_state.status == "running":
            run_state.finish_success(reply)
        elif run_state.final_answer is None:
            run_state.final_answer = reply
        self._trace(run_state, "run_finished", {
            "status": run_state.status,
            "reply_preview": event_preview(reply),
        })
        self._trace(run_state, RUN_COMPLETED, {
            "status": run_state.status,
            "reply_preview": event_preview(reply),
        })
        run_report = self._run_report(run_state, session, report=report)
        if self.trace_store is not None:
            self.trace_store.write_run_state(run_state)
            self.trace_store.write_report(
                run_state,
                run_report,
            )
        self._after_run_plugins(run_state, session, run_report)

    def _fail_run(self, run_state: RunState, session, exc: Exception) -> None:
        session.metadata.pop("active_run_id", None)
        session.metadata["last_run_id"] = run_state.run_id
        run_state.fail(exc)
        self._trace(run_state, "run_failed", {
            "error": run_state.error,
        })
        self._trace(run_state, RUN_FAILED, {
            "error": run_state.error,
        })
        run_report = self._run_report(run_state, session)
        if self.trace_store is not None:
            self.trace_store.write_run_state(run_state)
            self.trace_store.write_report(
                run_state,
                run_report,
            )
        self._after_run_plugins(run_state, session, run_report)

    def _run_report(
        self,
        run_state: RunState,
        session,
        *,
        report: dict | None = None,
    ) -> dict:
        return {
            "session_id": session.id,
            "mode": session.current_mode,
            "message_count": len(session.messages),
            "last_route": (session.metadata or {}).get("last_route"),
            "metadata": run_state.metadata,
            **(report or {}),
        }

    def _message_metadata(self, inbound, run_state: RunState) -> dict:
        metadata = dict(inbound.metadata or {})
        metadata["run_id"] = run_state.run_id
        return metadata

    def _trace(self, run_state: RunState, event_name: str, payload: dict) -> None:
        if self.trace_store is not None:
            self.trace_store.append_event(run_state, event_name, payload)

    def _begin_cancel_scope(self, session_id: str) -> None:
        with self._cancel_lock:
            event = self._cancel_events.get(str(session_id))
            if event is None:
                event = threading.Event()
                self._cancel_events[str(session_id)] = event
            else:
                event.clear()

    def _end_cancel_scope(self, session_id: str) -> None:
        with self._cancel_lock:
            self._cancel_events.pop(str(session_id), None)

    def _after_run_plugins(
        self,
        run_state: RunState,
        session,
        report: dict,
    ) -> None:
        if self.plugin_manager is None:
            return
        run_dir = (
            self.trace_store.run_dir(run_state)
            if self.trace_store is not None
            else None
        )
        self.plugin_manager.after_run(
            run_state=run_state,
            session=session,
            run_dir=run_dir,
            report=report,
        )


def _accepts_keyword(callable_obj, keyword: str) -> bool:
    try:
        parameters = inspect.signature(callable_obj).parameters
    except (TypeError, ValueError):
        return True
    if keyword in parameters:
        return True
    return any(
        parameter.kind == inspect.Parameter.VAR_KEYWORD
        for parameter in parameters.values()
    )
