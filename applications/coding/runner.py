import subprocess
from functools import partial
from typing import Any

from runtime.context import (
    ArtifactStore,
    ContextBuilder,
    EventCompactor,
    LongContentDetector,
    PromptAssetsService,
)
from runtime.context.budget import ContextBudgeter
from runtime.context.providers import DEFAULT_CONTEXT_PROVIDERS
from runtime.context.events import ContextEventType, thaw
from runtime.execution.loop_policies import standard_execution_policies
from applications.coding.context_state import build_coding_context_view
from applications.coding.task_state import ensure_task_state
from runtime.runtime import get_last_assistant_text
from runtime.agent_spec import AgentSpec
from runtime.runtime import RunContext, Runtime
from runtime.extensions import RuntimeExtensions
from applications.coding.handoff import (
    CODING_CONVERSATION_SUMMARY_METADATA_KEY,
    CODING_HANDOFF_METADATA_KEY,
    PENDING_CODING_TASK_SUMMARY_METADATA_KEY,
    build_coding_session_handoff,
    build_coding_task_summary,
)
from runtime.trace.events import (
    WORKSPACE_DIFF_WRITTEN,
    WORKSPACE_RESOLVED,
    WORKSPACE_SNAPSHOT_CAPTURED,
)
from runtime.trace.trace_store import event_preview
from runtime.trace.workspace import capture_workspace_snapshot, write_workspace_artifacts
from runtime.workspace import WorkspaceResolver
from config import (
    ARTIFACT_OFFLOADING_ENABLED,
    CONTEXT_ARTIFACT_ROOT,
    LONG_CONTENT_MAX_BYTES,
    LONG_CONTENT_MAX_CHARS,
    LONG_CONTENT_MAX_TOKENS,
    WORKDIR,
)
from memory.commands import MemoryContext
from runtime.sessions import SessionManager
from .artifacts import TaskArtifactPaths, TaskArtifactWriter
from .conclusions import TaskConclusionExtractor
from .promotion import TaskMemoryPromoter, PromotionResult
from .session import TaskSessionFactory, TaskSessionRecord
from .context_contributor import CodingRuntimeContributor
from user_scope import explicit_user_id_for_session, user_role_for_session
from skill_runtime import SKILL_LOADER


class CodingApplication:
    """Application lifecycle around the reusable coding AgentSpec/Runtime."""
    def __init__(
        self,
        *,
        sessions: SessionManager,
        base_pipeline: Runtime,
        workspace_root=None,
        workspace_resolver: WorkspaceResolver | None = None,
        semantic_memory_command_service=None,
        artifact_store: ArtifactStore | None = None,
        long_content_detector: LongContentDetector | None = None,
    ) -> None:
        self.sessions = sessions
        self.base_pipeline = base_pipeline
        self.semantic_memory_command_service = semantic_memory_command_service
        self.artifact_store = artifact_store or ArtifactStore(CONTEXT_ARTIFACT_ROOT)
        self.long_content_detector = long_content_detector or LongContentDetector(
            self.artifact_store,
            max_tokens=LONG_CONTENT_MAX_TOKENS,
            max_chars=LONG_CONTENT_MAX_CHARS,
            max_bytes=LONG_CONTENT_MAX_BYTES,
        )
        if workspace_resolver is not None:
            self.workspace_resolver = workspace_resolver
        elif workspace_root is not None:
            self.workspace_resolver = WorkspaceResolver(
                allowed_roots=[workspace_root],
                default_workspace=workspace_root,
            )
        else:
            self.workspace_resolver = WorkspaceResolver()
        self.factory = TaskSessionFactory(sessions)
        conclusion_provider, conclusion_model = _pipeline_model_for(
            base_pipeline,
            "task_conclusion",
        )
        self.conclusion_extractor = TaskConclusionExtractor(
            provider=conclusion_provider,
            model=conclusion_model,
        )
        compaction_provider, compaction_model = _pipeline_model_for(
            base_pipeline,
            "summary",
        )
        self.event_compactor = EventCompactor(
            provider=compaction_provider,
            model=compaction_model,
        )
        self.artifact_writer = TaskArtifactWriter()

    def run_coding_task(
        self,
        *,
        parent_session,
        user_text: str,
        profile,
        agent_spec=None,
        workspace_root=None,
        cancel_requested=None,
        run_state=None,
        trace_store=None,
    ) -> str:
        externalized = self._externalize_current_request(
            parent_session,
            user_text,
        )
        user_text = externalized["content"]
        request_artifact_refs = externalized["artifact_refs"]
        original_request_ref = externalized["event_ref"]
        workspace = self.workspace_resolver.resolve(
            workspace_root,
            session=parent_session,
        )
        self.workspace_resolver.bind_session(parent_session, workspace)
        session_handoff = build_coding_session_handoff(
            parent_session,
            current_user_request=user_text,
        )
        parent_session.metadata[CODING_CONVERSATION_SUMMARY_METADATA_KEY] = (
            session_handoff.prior_summary
        )
        record = self.factory.create(
            parent_session_id=parent_session.id,
            task_type="coding",
            user_request=user_text,
            original_request_ref=original_request_ref,
            artifact_refs=request_artifact_refs,
            user_id=explicit_user_id_for_session(parent_session),
            user_role=user_role_for_session(parent_session),
        )
        self.workspace_resolver.bind_session(record.session, workspace)
        repository_id, repository_revision = _repository_identity(workspace.root)
        if repository_id:
            record.session.metadata["repository"] = repository_id
        if repository_revision:
            record.session.metadata["code_revision"] = repository_revision
        record.session.metadata[CODING_HANDOFF_METADATA_KEY] = session_handoff.to_dict()
        ensure_task_state(
            record.session,
            objective_summary=_request_summary(user_text),
            original_request_ref=original_request_ref,
            artifact_refs=[
                str(ref.get("storage_uri") or ref.get("artifact_id") or "")
                for ref in request_artifact_refs
            ],
        )
        if run_state is not None:
            record.session.metadata["parent_run_id"] = run_state.run_id
            self.workspace_resolver.bind_session(record.session, workspace)
            run_state.metadata["coding_application"] = {
                "task_id": record.task_id,
                "coding_application_id": record.session.id,
                "status": "running",
            }
        if trace_store is not None and run_state is not None:
            trace_store.append_event(run_state, WORKSPACE_RESOLVED, {
                **workspace.to_metadata(),
            })
            trace_store.append_event(run_state, "coding_application_started", {
                "task_id": record.task_id,
                "coding_application_id": record.session.id,
                "parent_session_id": parent_session.id,
                "task_type": record.task_type,
                "user_request_preview": event_preview(user_text),
                "handoff_recent_turn_count": len(session_handoff.recent_turns),
                "handoff_has_prior_summary": bool(session_handoff.prior_summary.strip()),
            })
            trace_store.write_run_state(run_state)
        workspace_before = None
        if trace_store is not None and run_state is not None:
            workspace_before = capture_workspace_snapshot(workspace.root)
            trace_store.append_event(run_state, WORKSPACE_SNAPSHOT_CAPTURED, {
                "phase": "before",
                "root": workspace_before.root,
                "file_count": len(workspace_before.files),
                "skipped_count": len(workspace_before.skipped),
            })
        record.session.add_message(
            "user",
            self._build_task_request(
                parent_session.id,
                user_text,
                workspace=workspace,
                session_handoff=session_handoff,
            ),
            metadata={
                "kind": "coding_task_request",
                "source": "runtime-generated-wrapper",
                "original_request_ref": original_request_ref,
                "artifact_refs": request_artifact_refs,
            },
        )

        task_pipeline = self._build_task_pipeline(
            max_reasoning_steps=_task_reasoning_budget(
                user_text,
                default_steps=getattr(self.base_pipeline, "max_reasoning_steps", 24),
            ),
        )
        resolved_agent = agent_spec or AgentSpec.from_profile(profile)
        run_result = task_pipeline.run(
            resolved_agent,
            user_text,
            RunContext(
                session=record.session,
                profile=profile,
                cancel_requested=cancel_requested,
                checkpoint_callback=lambda session: self.sessions.save(session),
                run_state=run_state,
                trace_store=trace_store,
                extensions=RuntimeExtensions(
                    context_contributors=(CodingRuntimeContributor(),),
                ),
            ),
        )

        reply = get_last_assistant_text(record.session.messages)
        stop_reason = run_result.execution.stop_reason
        record.session.metadata["status"] = (
            "stopped" if stop_reason and stop_reason != "completed" else "completed"
        )
        record.session.metadata["task_reply"] = reply

        extraction = self.conclusion_extractor.extract(
            user_request=user_text,
            task_summary=reply,
            messages=record.session.messages,
        )
        promotion = TaskMemoryPromoter(
            command_service=self.semantic_memory_command_service,
        ).promote(
            task_id=record.task_id,
            extracted_conclusions=extraction.candidates,
            memory_context=(
                MemoryContext.from_session(record.session)
                if self.semantic_memory_command_service is not None
                else None
            ),
            repository_revision=repository_revision,
        )
        artifacts = None
        try:
            artifacts = self.artifact_writer.write(
                record=record,
                user_request=user_text,
                task_reply=reply,
                extraction=extraction,
                promotion=promotion,
            )
            record.session.metadata["task_log_path"] = _portable_path(artifacts.task_log_path)
            record.session.metadata["conclusions_path"] = _portable_path(artifacts.conclusions_path)
        except Exception as exc:
            record.session.metadata["artifact_error"] = f"{type(exc).__name__}: {exc}"

        if extraction.error:
            record.session.metadata["conclusion_extraction_error"] = extraction.error
        parent_task_summary = build_coding_task_summary(
            task_id=record.task_id,
            parent_session_id=record.parent_session_id,
            task_type=record.task_type,
            status=record.session.metadata.get("status", "completed"),
            user_request=user_text,
            task_reply=reply,
            extraction_summary=extraction.summary,
            task_log_path=record.session.metadata.get("task_log_path", ""),
            conclusions_path=record.session.metadata.get("conclusions_path", ""),
            promoted_count=len(promotion.promoted),
            skipped_count=len(promotion.skipped),
            rejected_count=len(promotion.rejected),
            original_request_ref=original_request_ref,
        )
        parent_session.metadata[PENDING_CODING_TASK_SUMMARY_METADATA_KEY] = (
            parent_task_summary
        )
        self.sessions.save(record.session)
        workspace_diff = None
        if (
            workspace_before is not None
            and trace_store is not None
            and run_state is not None
        ):
            workspace_after = capture_workspace_snapshot(workspace.root)
            trace_store.append_event(run_state, WORKSPACE_SNAPSHOT_CAPTURED, {
                "phase": "after",
                "root": workspace_after.root,
                "file_count": len(workspace_after.files),
                "skipped_count": len(workspace_after.skipped),
            })
            workspace_diff = write_workspace_artifacts(
                trace_store.run_dir(run_state),
                before=workspace_before,
                after=workspace_after,
            )
            trace_store.append_event(run_state, WORKSPACE_DIFF_WRITTEN, {
                "summary": workspace_diff.get("summary", {}),
                "created_preview": workspace_diff.get("created", [])[:20],
                "modified_preview": [
                    item.get("path") for item in workspace_diff.get("modified", [])[:20]
                ],
                "deleted_preview": workspace_diff.get("deleted", [])[:20],
            })
        if run_state is not None:
            task_report = {
                "task_id": record.task_id,
                "coding_application_id": record.session.id,
                "status": record.session.metadata.get("status", "completed"),
                "task_log_path": record.session.metadata.get("task_log_path", ""),
                "conclusions_path": record.session.metadata.get("conclusions_path", ""),
                "promoted_count": len(promotion.promoted),
                "skipped_count": len(promotion.skipped),
                "rejected_count": len(promotion.rejected),
            }
            if workspace_diff is not None:
                task_report["workspace_diff"] = workspace_diff.get("summary", {})
            run_state.metadata["coding_application"] = task_report
            if trace_store is not None:
                trace_store.append_event(run_state, "coding_application_completed", task_report)
                trace_store.write_run_state(run_state)
        return self._format_parent_reply(record, reply, promotion, artifacts)
    def _build_task_pipeline(
        self,
        *,
        max_reasoning_steps: int | None = None,
    ) -> Runtime:
        context_budgeter = ContextBudgeter.from_env()
        context_builder = ContextBuilder(
            budgeter=context_budgeter,
            prompt_assets_service=PromptAssetsService(
                budgeter=context_budgeter,
                skill_loader=SKILL_LOADER,
            ),
            coding_context_view_builder=partial(
                build_coding_context_view,
                event_compactor=self.event_compactor,
            ),
            context_providers=DEFAULT_CONTEXT_PROVIDERS,
        )
        if hasattr(self.base_pipeline, "fork"):
            return self.base_pipeline.fork(
                context_builder=context_builder,
                max_reasoning_steps=max_reasoning_steps,
                execution_policy_factory=standard_execution_policies,
            )
        return Runtime(
            tools=self.base_pipeline.tools,
            provider=self.base_pipeline.provider,
            model=self.base_pipeline.model,
            tool_executor=self.base_pipeline.tool_executor,
            context_builder=context_builder,
            max_reasoning_steps=max_reasoning_steps or self.base_pipeline.max_reasoning_steps,
            execution_policy_factory=standard_execution_policies,
        )

    def _build_task_request(
        self,
        parent_session_id: str,
        user_text: str,
        *,
        workspace,
        session_handoff,
    ) -> str:
        workspace_root = str(workspace.root)
        workspace_display = str(getattr(workspace, "display_name", "") or workspace_root)
        return (
            f"<task-session parent_session=\"{parent_session_id}\">\n"
            "You are running in an isolated coding task session. "
            "Task progress is owned by TaskState and transient evidence stays in session context.\n"
            "</task-session>\n\n"
            f"<coding-workspace root=\"{workspace_root}\" display=\"{workspace_display}\">\n"
            "All file tools use paths relative to this workspace. "
            "bash already runs at this workspace root; do not cd to the host Codex workdir "
            "or to absolute paths outside this workspace. "
            "When read_file/list_files returns a truncated result, continue with the "
            "provided offset instead of rereading the same page.\n"
            "</coding-workspace>\n\n"
            f"{session_handoff.render_prompt_block()}\n\n"
            "<execution-guidance>\n"
            "For broad read-only architecture reviews with independent lines of inquiry, "
            "use one early parallel_tasks fan-out, then synthesize the returned findings. "
            "If a subagent reports success=false or truncated=true, state that limitation "
            "and answer from the evidence already available unless one targeted follow-up "
            "read is necessary.\n"
            "</execution-guidance>\n\n"
            f"User coding task:\n{user_text}"
        )

    def _externalize_current_request(self, parent_session, user_text: str) -> dict[str, Any]:
        content = str(user_text or "")
        artifact_refs = _latest_user_artifact_refs(
            parent_session,
            expected_content=content,
        )
        replaces_event_id = ""
        if ARTIFACT_OFFLOADING_ENABLED:
            result = self.long_content_detector.externalize(
                content,
                artifact_type="user_input",
                name="coding-user-request",
                metadata={"session_id": str(getattr(parent_session, "id", "") or "")},
            )
            content = result.content
            if result.artifact_ref is not None:
                if not artifact_refs:
                    artifact_refs.append(result.artifact_ref.to_dict())
                replaces_event_id = _replace_latest_matching_user_message(
                    parent_session,
                    original=user_text,
                    replacement=content,
                    artifact_ref=result.artifact_ref.to_dict(),
                )
                append_event = getattr(parent_session, "append_event", None)
                if callable(append_event):
                    append_event(
                        ContextEventType.ARTIFACT_CREATED,
                        {
                            "artifact_ref": result.artifact_ref.to_dict(),
                            "source": "coding_user_request",
                        },
                    )
        event_ref = _ensure_current_user_request_event(
            parent_session,
            content=content,
            artifact_refs=artifact_refs,
        )
        if replaces_event_id:
            append_event = getattr(parent_session, "append_event", None)
            if callable(append_event):
                append_event(
                    ContextEventType.LEGACY_MESSAGE_REPLACED,
                    {
                        "replaces_event_id": replaces_event_id,
                        "replacement_event_id": event_ref.removeprefix("event://"),
                        "artifact_ref": artifact_refs[0] if artifact_refs else {},
                        "source": "coding_user_request",
                    },
                )
        return {
            "content": content,
            "artifact_refs": artifact_refs,
            "event_ref": event_ref,
        }

    def _format_parent_reply(
        self,
        record: TaskSessionRecord,
        reply: str,
        promotion: PromotionResult,
        artifacts: TaskArtifactPaths | None,
    ) -> str:
        status = str(record.session.metadata.get("status") or "completed")
        lines = [
            f"[TaskSession `{record.task_id}` {status}]",
            "",
            reply.strip() or "(no assistant reply)",
        ]
        if promotion.promoted:
            lines.extend([
                "",
                f"Submitted {len(promotion.promoted)} durable project conclusion(s).",
            ])
        if promotion.skipped:
            lines.extend([
                "",
                f"Skipped {len(promotion.skipped)} duplicate task memory item(s).",
            ])
        if promotion.rejected:
            lines.extend([
                "",
                f"Rejected {len(promotion.rejected)} noisy task memory candidate(s).",
            ])
        if artifacts is not None:
            lines.extend([
                "",
                f"Task log: `{_portable_path(artifacts.task_log_path)}`",
                f"Conclusions: `{_portable_path(artifacts.conclusions_path)}`",
            ])
        return "\n".join(lines)


def _repository_identity(root) -> tuple[str, str]:
    try:
        top = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            timeout=3,
            check=True,
        ).stdout.strip()
        revision = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=3,
            check=True,
        ).stdout.strip()
        return top, revision
    except (OSError, subprocess.SubprocessError):
        return "", ""


def _pipeline_model_for(pipeline: Runtime, purpose: str):
    if hasattr(pipeline, "provider_and_model_for"):
        return pipeline.provider_and_model_for(purpose)
    return pipeline.provider, pipeline.model


def _request_summary(value: str) -> str:
    text = str(value or "").strip().split("\n\n", 1)[0]
    return " ".join(text.split())[:300] or "Complete the current coding task"


def _latest_user_artifact_refs(
    session,
    *,
    expected_content: str | None = None,
) -> list[dict[str, Any]]:
    for message in reversed(list(getattr(session, "messages", []) or [])):
        if not isinstance(message, dict) or str(message.get("role") or "") != "user":
            continue
        if (
            expected_content is not None
            and str(message.get("content") or "") != str(expected_content or "")
        ):
            return []
        metadata = message.get("metadata")
        ref = metadata.get("artifact_ref") if isinstance(metadata, dict) else None
        if ref is None:
            ref = message.get("artifact_ref")
        return [dict(ref)] if isinstance(ref, dict) else []
    return []


def _replace_latest_matching_user_message(
    session,
    *,
    original: str,
    replacement: str,
    artifact_ref: dict[str, Any],
) -> str:
    messages = getattr(session, "messages", []) or []
    for message in reversed(messages):
        if not isinstance(message, dict) or str(message.get("role") or "") != "user":
            continue
        if str(message.get("content") or "") != str(original or ""):
            return ""
        backfill = getattr(session, "_backfill_legacy_messages", None)
        if callable(backfill):
            backfill()
        replaces_event_id = _message_event_id(session, message)
        message["content"] = replacement
        metadata = message.get("metadata")
        if not isinstance(metadata, dict):
            metadata = {}
            message["metadata"] = metadata
        metadata["artifact_ref"] = artifact_ref
        metadata["content_externalized"] = True
        return replaces_event_id
    return ""


def _ensure_current_user_request_event(
    session,
    *,
    content: str,
    artifact_refs: list[dict[str, Any]],
) -> str:
    messages = getattr(session, "messages", None)
    if not isinstance(messages, list):
        raise TypeError("coding parent session must expose a message list")
    current = messages[-1] if messages else None
    if not (
        isinstance(current, dict)
        and str(current.get("role") or "") == "user"
        and str(current.get("content") or "") == str(content or "")
    ):
        metadata: dict[str, Any] = {"kind": "coding_user_request_source"}
        if artifact_refs:
            metadata.update({
                "artifact_ref": dict(artifact_refs[0]),
                "content_externalized": True,
            })
        session.add_message("user", content, metadata=metadata)
        current = session.messages[-1]
    backfill = getattr(session, "_backfill_legacy_messages", None)
    if callable(backfill):
        backfill()
    event_id = _message_event_id(session, current)
    if not event_id:
        raise RuntimeError("coding request source event could not be recorded")
    return f"event://{event_id}"


def _message_event_id(session, message: dict[str, Any]) -> str:
    for event in reversed(list(getattr(session, "event_log", []) or [])):
        if str(getattr(event, "type", "") or "") not in {
            ContextEventType.USER_MESSAGE.value,
            ContextEventType.USER_CORRECTION.value,
        }:
            continue
        payload = thaw(event.payload)
        event_message = payload.get("message") if isinstance(payload, dict) else None
        if isinstance(event_message, dict) and event_message == message:
            return str(event.event_id)
    return ""


def _task_reasoning_budget(user_text: str, *, default_steps: int) -> int:
    text = str(user_text or "")
    independent_markers = [
        "独立",
        "多条",
        "分别检查",
        "架构梳理",
        "repository-wide",
        "multi-file",
    ]
    numbered_lines = sum(1 for line in text.splitlines() if line.strip().startswith(tuple("123456789")))
    if numbered_lines >= 3 or any(marker in text for marker in independent_markers):
        return max(int(default_steps or 24), 36)
    return int(default_steps or 24)


def _portable_path(path) -> str:
    resolved = path.resolve()
    try:
        return resolved.relative_to(WORKDIR.resolve()).as_posix()
    except ValueError:
        return str(resolved)
