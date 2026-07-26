from runtime.context import ContextBuilder, ContextMemoryService, PromptAssetsService
from runtime.context.budget import ContextBudgeter
from runtime.context.providers import DEFAULT_CONTEXT_PROVIDERS
from runtime.execution.loop_policies import standard_execution_policies
from applications.coding.context_state import build_coding_context_view
from runtime.execution.failure_reasons import REASONING_LOOP_STOP_REASON_KEY
from runtime.runtime import get_last_assistant_text
from runtime.agent_spec import AgentSpec
from runtime.runtime import RunContext, Runtime
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
from config import WORKDIR, WORKING_MEMORY_CHECKPOINT_ENABLED
from memory.store import MemoryStore
from runtime.sessions import SessionManager
from .artifacts import TaskArtifactPaths, TaskArtifactWriter
from .conclusions import TaskConclusionExtractor
from .memory_lifecycle import TaskMemoryLifecycle
from .promotion import TaskMemoryPromoter, PromotionResult
from .session import TaskSessionFactory, TaskSessionRecord
from user_scope import explicit_user_id_for_session, user_role_for_session
from runtime.working_memory import inherit_working_memory, sync_working_memory
from runtime.working_memory import render_working_memory_block
from skill_runtime import SKILL_LOADER


class CodingApplication:
    """Application lifecycle around the reusable coding AgentSpec/Runtime."""
    def __init__(
        self,
        *,
        sessions: SessionManager,
        base_pipeline: Runtime,
        global_memory: MemoryStore,
        workspace_root=None,
        workspace_resolver: WorkspaceResolver | None = None,
    ) -> None:
        self.sessions = sessions
        self.base_pipeline = base_pipeline
        self.global_memory = global_memory
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
        global_memory = self._global_memory_for(parent_session)
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
            user_id=explicit_user_id_for_session(parent_session),
            user_role=user_role_for_session(parent_session),
        )
        record.session.metadata[CODING_HANDOFF_METADATA_KEY] = session_handoff.to_dict()
        if WORKING_MEMORY_CHECKPOINT_ENABLED:
            inherit_working_memory(
                source_session=parent_session,
                target_session=record.session,
                objective=user_text,
                task_id=record.task_id,
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
        task_memory = MemoryStore(record.memory_root)
        self._seed_task_memory(
            task_memory=task_memory,
            parent_session_id=parent_session.id,
            user_text=user_text,
            session_handoff=session_handoff,
        )
        record.session.add_message(
            "user",
            self._build_task_request(
                parent_session.id,
                user_text,
                global_memory,
                workspace=workspace,
                session_handoff=session_handoff,
            ),
        )

        task_pipeline = self._build_task_pipeline(
            task_memory,
            max_reasoning_steps=_task_reasoning_budget(
                user_text,
                default_steps=getattr(self.base_pipeline, "max_reasoning_steps", 24),
            ),
        )
        resolved_agent = agent_spec or AgentSpec.from_profile(profile)
        task_pipeline.run(
            resolved_agent,
            user_text,
            RunContext(
                session=record.session,
                profile=profile,
                cancel_requested=cancel_requested,
                checkpoint_callback=lambda session: self.sessions.save(session),
                run_state=run_state,
                trace_store=trace_store,
            ),
        )

        reply = get_last_assistant_text(record.session.messages)
        stop_reason = record.session.metadata.get(REASONING_LOOP_STOP_REASON_KEY)
        record.session.metadata["status"] = "stopped" if stop_reason else "completed"
        record.session.metadata["task_reply"] = reply
        if WORKING_MEMORY_CHECKPOINT_ENABLED:
            sync_working_memory(
                source_session=record.session,
                target_session=parent_session,
            )

        extraction = self.conclusion_extractor.extract(
            user_request=user_text,
            task_summary=reply,
            messages=record.session.messages,
        )
        promotion = TaskMemoryPromoter(global_memory).promote(
            task_id=record.task_id,
            task_memory=task_memory,
            extracted_conclusions=extraction.candidates,
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
        task_memory: MemoryStore,
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
            memory_service=ContextMemoryService(
                memory_store=task_memory,
                working_memory_renderer=render_working_memory_block,
            ),
            coding_context_view_builder=build_coding_context_view,
            context_providers=DEFAULT_CONTEXT_PROVIDERS,
        )
        memory_lifecycle = TaskMemoryLifecycle(task_memory)
        if hasattr(self.base_pipeline, "fork"):
            return self.base_pipeline.fork(
                context_builder=context_builder,
                memory_lifecycle=memory_lifecycle,
                max_reasoning_steps=max_reasoning_steps,
                execution_policy_factory=standard_execution_policies,
            )
        return Runtime(
            tools=self.base_pipeline.tools,
            provider=self.base_pipeline.provider,
            model=self.base_pipeline.model,
            tool_executor=self.base_pipeline.tool_executor,
            context_builder=context_builder,
            memory_lifecycle=memory_lifecycle,
            max_reasoning_steps=max_reasoning_steps or self.base_pipeline.max_reasoning_steps,
            execution_policy_factory=standard_execution_policies,
        )

    def _seed_task_memory(
        self,
        *,
        task_memory: MemoryStore,
        parent_session_id: str,
        user_text: str,
        session_handoff,
    ) -> None:
        task_memory.append("now", f"Parent session: {parent_session_id}")
        task_memory.append("now", f"Task request: {user_text}")
        task_memory.append(
            "memory",
            "This task session may read global context but should only write durable "
            "findings to task-local memory. Useful findings are promoted after completion.",
        )
        task_memory.write_recent_context(
            f"- parent_session: `{parent_session_id}`\n"
            f"- task_request: {user_text}\n"
            f"- handoff_recent_turns: {len(session_handoff.recent_turns)}\n"
            f"- handoff_has_prior_summary: {bool(session_handoff.prior_summary.strip())}"
        )

    def _build_task_request(
        self,
        parent_session_id: str,
        user_text: str,
        global_memory: MemoryStore,
        *,
        workspace,
        session_handoff,
    ) -> str:
        global_memory_text = global_memory.read_all()
        workspace_root = str(workspace.root)
        workspace_display = str(getattr(workspace, "display_name", "") or workspace_root)
        return (
            f"<task-session parent_session=\"{parent_session_id}\">\n"
            "You are running in an isolated coding task session. "
            "Use the task-local context for intermediate work. "
            "Only durable project conventions or important findings should be memorized. "
            "When you discover a reusable project conclusion, call memorize with "
            "section='pending' so it can be reviewed for global promotion.\n"
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
            "<global-memory-snapshot>\n"
            f"{global_memory_text}\n"
            "</global-memory-snapshot>\n\n"
            f"User coding task:\n{user_text}"
        )

    def _global_memory_for(self, session) -> MemoryStore:
        if hasattr(self.global_memory, "for_session"):
            return self.global_memory.for_session(session)
        return self.global_memory

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
                f"Promoted {len(promotion.promoted)} task memory item(s) to global PENDING.md.",
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


def _pipeline_model_for(pipeline: Runtime, purpose: str):
    if hasattr(pipeline, "provider_and_model_for"):
        return pipeline.provider_and_model_for(purpose)
    return pipeline.provider, pipeline.model


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
