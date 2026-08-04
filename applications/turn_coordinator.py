from typing import Callable

from runtime.messaging.user_bus import OutboundMessage, MessageBus
from config import ARTIFACT_OFFLOADING_ENABLED
from runtime.context.long_content import LongContentDetector
from runtime.context.events import ContextEventType
from runtime.runtime import RunContext, Runtime
from runtime.execution.state import RunExecutionState
from runtime.trace.events import RUN_COMPLETED, RUN_FAILED
from runtime.trace.run_state import RunState
from runtime.trace.trace_store import event_preview
from runtime.routing.agent_router import AgentRouter
from applications.coding.handoff import (
    CODING_TASK_SUMMARY_METADATA_KEY,
    PENDING_CODING_TASK_SUMMARY_METADATA_KEY,
)
from runtime.sessions import SessionManager
from runtime.cancellation import CancellationRegistry


class TurnCoordinator:
    def __init__(
        self,
        bus: MessageBus,
        sessions: SessionManager,
        runtime: Runtime,
        router: AgentRouter,
        plugin_manager=None,
        subagent_runner=None,
        trace_store=None,
        coding_application=None,
        cancellation_registry: CancellationRegistry | None = None,
        long_content_detector: LongContentDetector | None = None,
    ) -> None:
        self.bus = bus
        self.sessions = sessions
        self.runtime = runtime
        self.router = router
        self.plugin_manager = plugin_manager
        self.coding_application = coding_application
        self.subagent_runner = subagent_runner
        self.trace_store = trace_store
        self.cancellation_registry = cancellation_registry or CancellationRegistry()
        self.long_content_detector = long_content_detector

    async def run_once(self, on_text: Callable[[str], None] | None = None) -> None:
        inbound = await self.bus.consume_inbound()
        await self.run_inbound(inbound, on_text=on_text)

    async def run_inbound(
        self,
        inbound,
        on_text: Callable[[str], None] | None = None,
    ) -> RunState | None:
        created_artifacts = self._externalize_inbound_artifacts(inbound)
        session = self.sessions.get_or_create(inbound.session_key)
        if created_artifacts:
            from runtime.context.artifact_access import register_artifact_access_state

            _record_artifact_offload_metrics(session, inbound)
            for created_artifact in created_artifacts:
                register_artifact_access_state(session.metadata, created_artifact)
                session.append_event(
                    ContextEventType.ARTIFACT_CREATED,
                    {"artifact_ref": created_artifact, "source": "user_message"},
                )
        self._begin_cancel_scope(session.id)
        run_state = None

        try:
            run_state = self._receive(session, inbound)
            if await self._preprocess(session, inbound, run_state, on_text):
                return run_state
            route = self._route(session, inbound, run_state)
            if route.switched:
                await self._handle_switch(session, inbound, route, run_state, on_text)
                return run_state
            self._record(session, inbound, run_state)
            reply = self._execute(session, inbound, route, run_state, on_text)
            self._postprocess(session, inbound, reply)
            await self._deliver(session, inbound, reply, route, run_state, on_text)
            return run_state
        except Exception as exc:
            if run_state is not None:
                self._fail_run(run_state, session, exc)
            raise
        finally:
            self._end_cancel_scope(session.id)

    def _externalize_inbound_artifacts(self, inbound) -> list[dict]:
        created = self._externalize_inbound_attachments(inbound)
        content_ref = self._externalize_inbound_content(inbound)
        if content_ref is not None:
            created.append(content_ref)
        return created

    def _externalize_inbound_attachments(self, inbound) -> list[dict]:
        metadata = getattr(inbound, "metadata", None)
        attachments = metadata.get("attachments") if isinstance(metadata, dict) else None
        if not isinstance(attachments, list) or not attachments:
            return []
        if not ARTIFACT_OFFLOADING_ENABLED or self.long_content_detector is None:
            return []
        normalized = []
        created = []
        for index, raw in enumerate(attachments):
            if not isinstance(raw, dict):
                continue
            content = raw.get("content")
            if content is None:
                normalized.append(dict(raw))
                continue
            name = str(raw.get("name") or f"attachment-{index + 1}")
            ref = self.long_content_detector.artifact_store.put_artifact(
                str(content),
                artifact_type="user_input",
                name=name,
                mime_type="text/markdown",
                metadata={
                    "source": "user_attachment",
                    "source_media_type": str(raw.get("media_type") or ""),
                    "source_size_bytes": max(0, int(raw.get("size_bytes") or 0)),
                },
            )
            descriptor = {
                "name": name,
                "media_type": str(raw.get("media_type") or "application/octet-stream"),
                "size_bytes": max(0, int(raw.get("size_bytes") or 0)),
                "content_state": "externalized",
                "artifact_ref": ref.to_dict(),
            }
            normalized.append(descriptor)
            created.append(ref.to_dict())
        metadata["attachments"] = normalized
        return created

    def _externalize_inbound_content(self, inbound) -> dict | None:
        if not ARTIFACT_OFFLOADING_ENABLED or self.long_content_detector is None:
            return None
        content = str(getattr(inbound, "content", "") or "")
        result = self.long_content_detector.externalize(
            content,
            artifact_type="user_input",
            name="user-message",
            metadata={
                "channel": str(getattr(inbound, "channel", "") or ""),
                "chat_id": str(getattr(inbound, "chat_id", "") or ""),
            },
        )
        if result.artifact_ref is None:
            return None
        inbound.content = result.content
        metadata = getattr(inbound, "metadata", None)
        if not isinstance(metadata, dict):
            metadata = {}
            inbound.metadata = metadata
        metadata["artifact_ref"] = result.artifact_ref.to_dict()
        metadata["content_externalized"] = True
        metadata["original_content_tokens"] = result.assessment.token_count
        metadata["original_content_chars"] = result.assessment.char_count
        metadata["original_content_bytes"] = result.assessment.byte_count
        return result.artifact_ref.to_dict()

    def request_cancel(self, session_id: str) -> bool:
        return self.cancellation_registry.request(self._turn_scope(session_id))

    def cancel_requested(self, session_id: str) -> bool:
        return self.cancellation_registry.requested(self._turn_scope(session_id))

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
            mode=session.active_agent,
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
            mode=session.active_agent,
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
                "mode": session.active_agent,
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
        if self.coding_application is not None and route.profile.tool_mode == "coding":
            task_kwargs = {
                "parent_session": session,
                "user_text": inbound.content,
                "profile": route.profile,
                "cancel_requested": cancel_requested,
            }
            if getattr(route, "agent_spec", None) is not None:
                task_kwargs["agent_spec"] = route.agent_spec
            workspace_root = (inbound.metadata or {}).get("workspace_root")
            if workspace_root:
                task_kwargs["workspace_root"] = workspace_root
            if self.trace_store is not None:
                task_kwargs.update({
                    "run_state": run_state,
                    "trace_store": self.trace_store,
                })
            reply = self.coding_application.run_coding_task(**task_kwargs)
            assistant_metadata = {"run_id": run_state.run_id}
            task_summary = session.metadata.pop(
                PENDING_CODING_TASK_SUMMARY_METADATA_KEY,
                None,
            )
            if task_summary:
                assistant_metadata["kind"] = "coding_task_result"
                assistant_metadata[CODING_TASK_SUMMARY_METADATA_KEY] = task_summary
            session.add_message(
                "assistant",
                reply,
                metadata=assistant_metadata,
            )
            self._emit_text(on_text, reply)
            return reply
        agent_spec = getattr(route, "agent_spec", None)
        if agent_spec is None:
            from runtime.agent_spec import AgentSpec

            agent_spec = AgentSpec.from_profile(route.profile)
        if self.runtime is None:
            raise RuntimeError("TurnCoordinator has no Runtime for execution.")
        return self.runtime.run(
            agent_spec,
            inbound.content,
            RunContext(
                session=session,
                profile=route.profile,
                on_text=on_text,
                cancel_requested=cancel_requested,
                run_state=run_state,
                trace_store=self.trace_store,
                state=RunExecutionState(
                    thinking_enabled=bool(
                        (inbound.metadata or {}).get("thinking_enabled", False)
                    )
                ),
            ),
        ).output

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
            mode=session.active_agent,
            execution_path="routing",
            metadata={
                "sender": inbound.sender,
                "media_count": len(inbound.media or []),
            },
        )
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
            "mode": session.active_agent,
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
        scope = self._turn_scope(session_id)
        self.cancellation_registry.release(scope)
        self.cancellation_registry.register(scope)

    def _end_cancel_scope(self, session_id: str) -> None:
        self.cancellation_registry.release(self._turn_scope(session_id))

    @staticmethod
    def _turn_scope(session_id: str) -> str:
        return f"turn:{session_id}"

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


def _record_artifact_offload_metrics(session, inbound) -> None:
    metadata = getattr(session, "metadata", None)
    if not isinstance(metadata, dict):
        metadata = {}
        session.metadata = metadata
    metrics = metadata.get("context_metrics")
    if not isinstance(metrics, dict):
        metrics = {}
    inbound_metadata = (
        inbound.metadata if isinstance(getattr(inbound, "metadata", None), dict) else {}
    )
    original_chars = max(0, int(inbound_metadata.get("original_content_chars") or 0))
    original_tokens = max(0, int(inbound_metadata.get("original_content_tokens") or 0))
    replacement_chars = len(str(getattr(inbound, "content", "") or ""))
    metrics["artifact_offloaded_chars"] = (
        int(metrics.get("artifact_offloaded_chars", 0) or 0) + original_chars
    )
    metrics["artifact_offloaded_tokens"] = (
        int(metrics.get("artifact_offloaded_tokens", 0) or 0) + original_tokens
    )
    metrics["duplicate_content_saved_chars"] = (
        int(metrics.get("duplicate_content_saved_chars", 0) or 0)
        + max(0, original_chars - replacement_chars)
    )
    metadata["context_metrics"] = metrics
