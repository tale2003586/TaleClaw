# context.py

import hashlib
from dataclasses import dataclass, field, replace
import json
from pathlib import Path
import threading
from typing import Any

from runtime.context_budget import BudgetedText, ContextBudgeter
from runtime.context_history import (
    BudgetedMessages,
    budget_active_turn,
    budget_conversation_history,
)
from runtime.coding_context_state import (
    CodingContextView,
    build_coding_context_view,
)
from runtime.context_build_state import BuildState
from runtime.context_sections import ContextBuildReport, ContextSection, message_chars
from runtime.trace.events import (
    SECURITY_RAG_COMPLETED,
    SECURITY_RAG_FAILED,
    SECURITY_RAG_SEARCH_COMPLETED,
    SECURITY_RAG_SEARCH_FAILED,
    SECURITY_RAG_STARTED,
)
from runtime.trace.rag import (
    append_security_rag_event,
    rag_completed_payload,
    search_trace_payload,
    security_rag_span_id,
)
from runtime.working_memory import render_working_memory_block
from skill_runtime import SKILL_LOADER
from memory.vector_runtime import history_vector_scope_for_session
from knowledge.tracing import Timer, make_rag_trace, write_rag_trace_if_enabled
from config import (
    CODING_CONTEXT_COMPACTION_TARGET_TOKENS,
    CODING_CONTEXT_COMPACTION_TRIGGER_TOKENS,
    CODING_CONTEXT_RECENT_GROUPS,
    CODING_CONTEXT_STATE_ENABLED,
    WORKING_MEMORY_RESUME_ENABLED,
)


DEFAULT_INSTRUCTION_LIMIT = 12000


@dataclass
class ContextBundle:
    messages: list[dict]
    report: ContextBuildReport | None = None


@dataclass
class ContextPrefix:
    """Stable prompt prefix reused across reasoning steps in one turn."""

    system_prompt: str
    profile_prompt: str
    instruction_sections: list[ContextSection]
    instruction_reductions: list[dict[str, Any]]
    skill_catalog: BudgetedText
    runtime_guidance: str
    fingerprint: str
    base_fingerprint: str = ""
    messages: list[dict] = field(default_factory=list)
    history_messages: list[dict] = field(default_factory=list)
    budgeted_history: BudgetedMessages | None = None
    active_turn_start_index: int | None = None
    cache_hit: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)


class ContextBuilder:
    _instruction_cache_lock = threading.Lock()
    _instruction_cache: dict[Path, tuple[int, int, str]] = {}
    _guidance_registry: list[str] = [
        "Use recall_memory when the user asks about prior preferences or project conventions.",
        "Use memorize when the user states a durable preference or important fact.",
        "Some tools are deferred. Use tool_search to find or unlock tools that are not currently visible.",
        "For code-security questions involving vulnerabilities, CVE/CWE/GHSA, dependencies, auth, authorization, injection, XSS, SSRF, tokens, secrets, file upload, path traversal, or secure coding guidance, call security_rag_search for local evidence before giving a final answer.",
        "Search-like tools are limited opportunities. Batch queries and gather enough evidence before deciding whether another search is necessary.",
    ]

    def __init__(
        self,
        memory_store=None,
        *,
        instruction_root: str | Path | None = None,
        instruction_limit: int = DEFAULT_INSTRUCTION_LIMIT,
        budgeter: ContextBudgeter | None = None,
        history_vector_index=None,
        history_scope_resolver=None,
        memory_vector_index=None,
        memory_scope_resolver=None,
        retrieval_top_k: int = 6,
        retrieval_min_score: float = 0.35,
        security_retrieval_router=None,
        security_route_classifier=None,
        security_knowledge_index=None,
        security_auto_context_enabled: bool = True,
        skill_loader=None,
    ) -> None:
        self.memory_store = memory_store
        self.instruction_root = Path(instruction_root or Path.cwd()).resolve()
        self.instruction_limit = max(1000, int(instruction_limit))
        self.budgeter = budgeter or ContextBudgeter.from_env()
        self.history_vector_index = history_vector_index or memory_vector_index
        self.history_scope_resolver = (
            history_scope_resolver
            or memory_scope_resolver
            or history_vector_scope_for_session
        )
        self.retrieval_top_k = max(1, int(retrieval_top_k))
        self.retrieval_min_score = float(retrieval_min_score)
        self.security_retrieval_router = security_retrieval_router
        self.security_route_classifier = security_route_classifier
        self.security_knowledge_index = security_knowledge_index
        self.security_auto_context_enabled = bool(security_auto_context_enabled)
        self.skill_loader = skill_loader if skill_loader is not None else SKILL_LOADER
        self._prefix_cache_lock = threading.Lock()
        self._prefix_cache: dict[str, ContextPrefix] = {}

    def build_prefix(
        self,
        profile,
        *,
        session=None,
        active_turn_start_index: int | None = None,
    ) -> ContextPrefix:
        base = self._build_base_prefix(profile)
        if session is None:
            return base

        session_messages = list(getattr(session, "messages", []) or [])
        history_messages, _, _ = self._split_active_turn(
            session_messages,
            active_turn_start_index=active_turn_start_index,
        )
        budgeted_history = self._budget_conversation_history_for_profile(
            history_messages,
            profile=profile,
            session=session,
        )
        messages = [
            {"role": "system", "content": base.system_prompt},
            *budgeted_history.rendered_messages,
        ]
        fingerprint = _prefix_messages_fingerprint(
            base.fingerprint,
            messages,
            active_turn_start_index=active_turn_start_index,
        )
        metadata = {
            **base.metadata,
            "base_fingerprint": base.fingerprint,
            "fingerprint": fingerprint,
            "includes_history": True,
            "history_message_count": len(history_messages),
            "rendered_history_message_count": len(budgeted_history.rendered_messages),
            "coding_active_turn_only": self._coding_uses_active_turn_only_history(
                profile,
                session=session,
            ),
        }
        return replace(
            base,
            fingerprint=fingerprint,
            base_fingerprint=base.fingerprint,
            messages=messages,
            history_messages=history_messages,
            budgeted_history=budgeted_history,
            active_turn_start_index=active_turn_start_index,
            metadata=metadata,
        )

    def _build_base_prefix(self, profile) -> ContextPrefix:
        fingerprint = self._prefix_fingerprint(profile)
        with self._prefix_cache_lock:
            cached = self._prefix_cache.get(fingerprint)
        if cached is not None:
            return replace(
                cached,
                cache_hit=True,
                metadata={
                    **cached.metadata,
                    "cache_hit": True,
                },
            )

        profile_prompt = str(getattr(profile, "system_prompt", "") or "")
        instruction_block, instruction_sections, instruction_reductions = (
            self._build_instruction_block(profile)
        )
        raw_skill_catalog = self._build_skill_catalog_block()
        budgeted_skill_catalog = self.budgeter.apply(
            "skill_catalog",
            raw_skill_catalog,
        )
        runtime_guidance = self._runtime_guidance()
        system_prompt = self._build_system_prompt(
            profile_prompt=profile_prompt,
            instruction_block=instruction_block,
            skill_catalog_block=budgeted_skill_catalog.rendered_text,
            runtime_guidance=runtime_guidance,
        )
        prefix = ContextPrefix(
            system_prompt=system_prompt,
            profile_prompt=profile_prompt,
            instruction_sections=instruction_sections,
            instruction_reductions=instruction_reductions,
            skill_catalog=budgeted_skill_catalog,
            runtime_guidance=runtime_guidance,
            fingerprint=fingerprint,
            base_fingerprint=fingerprint,
            messages=[{"role": "system", "content": system_prompt}],
            metadata={
                "fingerprint": fingerprint,
                "base_fingerprint": fingerprint,
                "cache_hit": False,
                "mode": str(getattr(profile, "tool_mode", "bot") or "bot"),
                "includes_history": False,
                "skill_catalog_chars": len(budgeted_skill_catalog.rendered_text),
            },
        )
        with self._prefix_cache_lock:
            self._prefix_cache[fingerprint] = prefix
        return prefix

    def build(
        self,
        *,
        session,
        profile,
        prefix: ContextPrefix | None = None,
        inbox: list | None = None,
        background_results: list | None = None,
        active_turn_start_index: int | None = None,
        include_security_knowledge: bool = True,
        trace_store=None,
        run_state=None,
        trace_parent_span_id: str | None = None,
        reasoning_step: int | None = None,

    ) -> ContextBundle:
        prefix = prefix or self.build_prefix(
            profile,
            session=session,
            active_turn_start_index=active_turn_start_index,
        )
        profile_prompt = prefix.profile_prompt
        instruction_sections = prefix.instruction_sections
        instruction_reductions = prefix.instruction_reductions
        runtime_guidance = prefix.runtime_guidance
        system_prompt = prefix.system_prompt
        session_messages = list(session.messages)
        history_messages, active_turn_messages, current_request = (
            self._split_active_turn(
                session_messages,
                active_turn_start_index=active_turn_start_index,
            )
        )
        if self._can_reuse_prefix_history(
            prefix,
            profile=profile,
            session=session,
            active_turn_start_index=active_turn_start_index,
        ):
            history_messages = list(prefix.history_messages)
            budgeted_history = prefix.budgeted_history
        else:
            budgeted_history = self._budget_conversation_history_for_profile(
                history_messages,
                profile=profile,
                session=session,
            )

        raw_memory_block = self._build_memory_block(
            session,
            current_request=current_request,
        )
        budgeted_memory = self.budgeter.apply("memory", raw_memory_block)
        raw_working_memory_block = self._build_working_memory_block(session, profile=profile)
        budgeted_working_memory = self.budgeter.apply(
            "working_memory",
            raw_working_memory_block,
        )
        raw_retrieved_history, retrieved_hits = self._build_retrieved_history_block(
            session=session,
            current_request=current_request,
            active_turn_messages=active_turn_messages,
        )
        budgeted_retrieved_history = self.budgeter.apply(
            "retrieved_history",
            raw_retrieved_history,
        )
        if include_security_knowledge:
            raw_security_knowledge, security_decision, security_hits = (
                self._build_security_knowledge_block(
                    current_request=current_request,
                    trace_store=trace_store,
                    run_state=run_state,
                    trace_parent_span_id=trace_parent_span_id,
                    reasoning_step=reasoning_step,
                )
            )
        else:
            raw_security_knowledge, security_decision, security_hits = "", None, []
        budgeted_security_knowledge = self.budgeter.apply(
            "security_knowledge",
            raw_security_knowledge,
        )
        raw_task_runtime_events = self._build_task_runtime_events_block(
            inbox=inbox or [],
            background_results=background_results or [],
        )
        budgeted_task_runtime_events = self.budgeter.apply(
            "task_runtime_events",
            raw_task_runtime_events,
        )

        if self._coding_context_state_enabled(profile, session=session):
            return self._build_coding_context_state_bundle(
                session=session,
                profile=profile,
                prefix=prefix,
                profile_prompt=profile_prompt,
                instruction_sections=instruction_sections,
                instruction_reductions=instruction_reductions,
                skill_catalog=prefix.skill_catalog,
                runtime_guidance=runtime_guidance,
                system_prompt=system_prompt,
                session_messages=session_messages,
                history_messages=history_messages,
                budgeted_history=budgeted_history,
                active_turn_messages=active_turn_messages,
                active_turn_start_index=active_turn_start_index,
                current_request=current_request,
                raw_memory_block=raw_memory_block,
                budgeted_memory=budgeted_memory,
                raw_working_memory_block=raw_working_memory_block,
                raw_retrieved_history=raw_retrieved_history,
                retrieved_hits=retrieved_hits,
                budgeted_retrieved_history=budgeted_retrieved_history,
                raw_security_knowledge=raw_security_knowledge,
                security_decision=security_decision,
                security_hits=security_hits,
                budgeted_security_knowledge=budgeted_security_knowledge,
                raw_task_runtime_events=raw_task_runtime_events,
                budgeted_task_runtime_events=budgeted_task_runtime_events,
                inbox=inbox or [],
                background_results=background_results or [],
            )

        context_frame = self._build_context_frame(
            memory_block=budgeted_memory.rendered_text,
            working_memory_block=budgeted_working_memory.rendered_text,
            retrieved_history_block=budgeted_retrieved_history.rendered_text,
            security_knowledge_block=budgeted_security_knowledge.rendered_text,
            task_runtime_events_block=budgeted_task_runtime_events.rendered_text,
        )

        budgeted_active_turn = budget_active_turn(
            active_turn_messages,
            enabled=self.budgeter.enabled,
            rule=self._active_turn_rule_for_profile(profile, session=session),
        )
        budgeted_active_turn = self._annotate_active_turn_budget_for_profile(
            budgeted_active_turn,
            profile=profile,
            session=session,
        )
        if self._can_reuse_prefix_messages(
            prefix,
            profile=profile,
            session=session,
            active_turn_start_index=active_turn_start_index,
        ):
            messages = list(prefix.messages)
        else:
            messages = [
                {"role": "system", "content": system_prompt},
                *budgeted_history.rendered_messages,
            ]

        if context_frame:
            messages.append({
                "role": "user",
                "content": context_frame,
            })

        messages.extend(budgeted_active_turn.rendered_messages)

        build_state = BuildState(
            messages=messages,
            profile_prompt=profile_prompt,
            instruction_sections=instruction_sections,
            skill_catalog=prefix.skill_catalog,
            runtime_guidance=runtime_guidance,
            system_prompt=system_prompt,
            session_messages=session_messages,
            history_messages=history_messages,
            budgeted_history=budgeted_history,
            active_turn_messages=active_turn_messages,
            budgeted_active_turn=budgeted_active_turn,
            active_turn_start_index=active_turn_start_index,
            current_request=current_request,
            memory_block=budgeted_memory.rendered_text,
            raw_memory_block=raw_memory_block,
            budgeted_memory=budgeted_memory,
            working_memory_block=budgeted_working_memory.rendered_text,
            raw_working_memory_block=raw_working_memory_block,
            budgeted_working_memory=budgeted_working_memory,
            retrieved_history_block=budgeted_retrieved_history.rendered_text,
            raw_retrieved_history_block=raw_retrieved_history,
            budgeted_retrieved_history=budgeted_retrieved_history,
            retrieved_hits=retrieved_hits,
            security_knowledge_block=budgeted_security_knowledge.rendered_text,
            raw_security_knowledge_block=raw_security_knowledge,
            budgeted_security_knowledge=budgeted_security_knowledge,
            security_decision=security_decision,
            security_hits=security_hits,
            inbox=inbox or [],
            background_results=background_results or [],
            task_runtime_events=budgeted_task_runtime_events.rendered_text,
            raw_task_runtime_events=raw_task_runtime_events,
            budgeted_task_runtime_events=budgeted_task_runtime_events,
            context_frame=context_frame,
            reductions=[
                *instruction_reductions,
                *self._reduction_list(
                    budgeted_history,
                    budgeted_active_turn,
                    prefix.skill_catalog,
                    budgeted_memory,
                    budgeted_retrieved_history,
                    budgeted_security_knowledge,
                    budgeted_task_runtime_events,
                ),
            ],
            prefix_fingerprint=prefix.fingerprint,
            prefix_cache_hit=prefix.cache_hit,
            prefix_metadata=dict(prefix.metadata),
        )
        report = self._build_report(build_state)
        return ContextBundle(messages=messages, report=report)

    def _build_coding_context_state_bundle(
        self,
        *,
        session,
        profile,
        prefix: ContextPrefix,
        profile_prompt: str,
        instruction_sections: list[ContextSection],
        instruction_reductions: list[dict[str, Any]],
        skill_catalog: BudgetedText,
        runtime_guidance: str,
        system_prompt: str,
        session_messages: list[dict],
        history_messages: list[dict],
        budgeted_history: BudgetedMessages,
        active_turn_messages: list[dict],
        active_turn_start_index: int | None,
        current_request: str,
        raw_memory_block: str,
        budgeted_memory: BudgetedText,
        raw_working_memory_block: str,
        raw_retrieved_history: str,
        retrieved_hits: list,
        budgeted_retrieved_history: BudgetedText,
        raw_security_knowledge: str,
        security_decision,
        security_hits: list,
        budgeted_security_knowledge: BudgetedText,
        raw_task_runtime_events: str,
        budgeted_task_runtime_events: BudgetedText,
        inbox: list,
        background_results: list,
    ) -> ContextBundle:
        effective_active_turn_start_index = (
            active_turn_start_index
            if active_turn_start_index is not None
            else len(history_messages)
        )
        budgeted_working_memory = BudgetedText(
            name="working_memory",
            raw_text=raw_working_memory_block,
            rendered_text="",
            budget_chars=None,
            strategy="coding_context_state",
            truncated=bool(raw_working_memory_block),
            metadata={
                "budget_enabled": bool(self.budgeter.enabled),
                "strategy": "coding_context_state",
                "transport": "coding_context_state",
                "consumed_by": "coding_context_state",
            },
        )
        context_frame = self._build_context_frame(
            memory_block=budgeted_memory.rendered_text,
            working_memory_block="",
            retrieved_history_block=budgeted_retrieved_history.rendered_text,
            security_knowledge_block=budgeted_security_knowledge.rendered_text,
            task_runtime_events_block=budgeted_task_runtime_events.rendered_text,
        )

        if self._can_reuse_prefix_messages(
            prefix,
            profile=profile,
            session=session,
            active_turn_start_index=active_turn_start_index,
        ):
            messages = list(prefix.messages)
        else:
            messages = [
                {"role": "system", "content": system_prompt},
                *budgeted_history.rendered_messages,
            ]

        if context_frame:
            messages.append({
                "role": "user",
                "content": context_frame,
            })

        view = build_coding_context_view(
            session,
            objective=current_request,
            active_turn_start_index=effective_active_turn_start_index,
            static_messages=messages,
            threshold_tokens=CODING_CONTEXT_COMPACTION_TRIGGER_TOKENS,
            target_tokens=CODING_CONTEXT_COMPACTION_TARGET_TOKENS,
            keep_recent_groups=CODING_CONTEXT_RECENT_GROUPS,
        )
        messages.append(view.state_message)
        messages.extend(view.recent_messages)

        active_turn_raw = _copy_context_messages(active_turn_messages)
        active_turn_rendered = _copy_context_messages(view.recent_messages)
        budgeted_active_turn = BudgetedMessages(
            name="active_turn",
            raw_messages=active_turn_raw,
            rendered_messages=active_turn_rendered,
            budget_chars=None,
            floor_chars=0,
            strategy="coding_context_state",
            truncated=view.compacted or len(active_turn_rendered) < len(active_turn_raw),
            reduction=view.reduction,
            metadata={
                "budget_enabled": bool(self.budgeter.enabled),
                "strategy": "coding_context_state",
                "transport": "chat_messages",
                "preserve": True,
                "message_count": len(active_turn_raw),
                "rendered_message_count": len(active_turn_rendered),
                "start_index": effective_active_turn_start_index,
                "prompt_tail_start_index": view.state.prompt_tail_start_index,
                "compacted_until_index": view.state.compacted_until_index,
                "generation": view.state.generation,
                "before_tokens": view.before_tokens,
                "after_tokens": view.after_tokens,
                "compacted": view.compacted,
            },
        )

        build_state = BuildState(
            messages=messages,
            profile_prompt=profile_prompt,
            instruction_sections=instruction_sections,
            skill_catalog=skill_catalog,
            runtime_guidance=runtime_guidance,
            system_prompt=system_prompt,
            session_messages=session_messages,
            history_messages=history_messages,
            budgeted_history=budgeted_history,
            active_turn_messages=active_turn_messages,
            budgeted_active_turn=budgeted_active_turn,
            active_turn_start_index=active_turn_start_index,
            current_request=current_request,
            memory_block=budgeted_memory.rendered_text,
            raw_memory_block=raw_memory_block,
            budgeted_memory=budgeted_memory,
            working_memory_block="",
            raw_working_memory_block=raw_working_memory_block,
            budgeted_working_memory=budgeted_working_memory,
            retrieved_history_block=budgeted_retrieved_history.rendered_text,
            raw_retrieved_history_block=raw_retrieved_history,
            budgeted_retrieved_history=budgeted_retrieved_history,
            retrieved_hits=retrieved_hits,
            security_knowledge_block=budgeted_security_knowledge.rendered_text,
            raw_security_knowledge_block=raw_security_knowledge,
            budgeted_security_knowledge=budgeted_security_knowledge,
            security_decision=security_decision,
            security_hits=security_hits,
            inbox=inbox,
            background_results=background_results,
            task_runtime_events=budgeted_task_runtime_events.rendered_text,
            raw_task_runtime_events=raw_task_runtime_events,
            budgeted_task_runtime_events=budgeted_task_runtime_events,
            context_frame=context_frame,
            reductions=[
                *instruction_reductions,
                *self._reduction_list(
                    budgeted_history,
                    budgeted_active_turn,
                    skill_catalog,
                    budgeted_memory,
                    budgeted_retrieved_history,
                    budgeted_security_knowledge,
                    budgeted_task_runtime_events,
                ),
            ],
            prefix_fingerprint=prefix.fingerprint,
            prefix_cache_hit=prefix.cache_hit,
            prefix_metadata=dict(prefix.metadata),
        )
        report = self._build_report(build_state)
        self._append_coding_context_state_report(report, view)
        return ContextBundle(messages=messages, report=report)

    def _append_coding_context_state_report(
        self,
        report: ContextBuildReport,
        view: CodingContextView,
    ) -> None:
        report.sections.append(
            ContextSection.from_text(
                "coding_context_state",
                str(view.state_message.get("content") or ""),
                raw_text=json.dumps(
                    view.state.to_dict(),
                    ensure_ascii=False,
                    sort_keys=True,
                    default=str,
                ),
                budget_chars=CODING_CONTEXT_COMPACTION_TARGET_TOKENS,
                truncated=view.compacted,
                metadata={
                    "transport": "chat_message",
                    "preserve": True,
                    "generation": view.state.generation,
                    "threshold_tokens": CODING_CONTEXT_COMPACTION_TRIGGER_TOKENS,
                    "target_tokens": CODING_CONTEXT_COMPACTION_TARGET_TOKENS,
                    "keep_recent_groups": CODING_CONTEXT_RECENT_GROUPS,
                    "before_tokens": view.before_tokens,
                    "after_tokens": view.after_tokens,
                    "prompt_tail_start_index": view.state.prompt_tail_start_index,
                    "compacted_until_index": view.state.compacted_until_index,
                    "recent_message_count": len(view.recent_messages),
                    "active_message_count": len(view.active_messages),
                    "compacted": view.compacted,
                },
            )
        )
        report.metadata = {
            **dict(report.metadata or {}),
            "coding_context_state_enabled": True,
            "coding_context_generation": view.state.generation,
            "coding_context_prompt_tail_start_index": view.state.prompt_tail_start_index,
            "coding_context_compacted_until_index": view.state.compacted_until_index,
            "coding_context_before_tokens": view.before_tokens,
            "coding_context_after_tokens": view.after_tokens,
        }

    def _build_context_frame(
        self,
        *,
        memory_block: str,
        working_memory_block: str,
        retrieved_history_block: str,
        security_knowledge_block: str,
        task_runtime_events_block: str,
    ) -> str:
        sections = []

        if security_knowledge_block:
            sections.append(
                "<!-- context-priority: security_knowledge; highest priority local security evidence -->\n"
                + security_knowledge_block
            )

        if working_memory_block:
            sections.append(
                "<!-- context-priority: working_memory; resumable task checkpoint -->\n"
                + working_memory_block
            )

        if task_runtime_events_block:
            sections.append(
                "<!-- context-priority: task_runtime_events; immediate inbox/background updates -->\n"
                + task_runtime_events_block
            )

        if retrieved_history_block:
            sections.append(
                "<!-- context-priority: retrieved_history; relevant prior session turns -->\n"
                + retrieved_history_block
            )

        if memory_block:
            sections.append(
                "<!-- context-priority: memory; durable user/project memory -->\n"
                + memory_block
            )

        return "\n\n".join(sections)

    def _build_working_memory_block(self, session, *, profile) -> str:
        if not WORKING_MEMORY_RESUME_ENABLED:
            return ""
        if not self._coding_uses_active_turn_only_history(profile, session=session):
            return ""
        return render_working_memory_block(session)
    
    def _build_system_prompt(
        self,
        *,
        profile_prompt: str,
        instruction_block: str,
        skill_catalog_block: str,
        runtime_guidance: str,
    ) -> str:
        sections = [
            profile_prompt,
            instruction_block,
            skill_catalog_block,
            runtime_guidance,
        ]
        return "\n\n".join(section for section in sections if section.strip())

    def _build_skill_catalog_block(self) -> str:
        if self.skill_loader is None:
            return ""
        try:
            descriptions = str(self.skill_loader.get_descriptions() or "").strip()
        except Exception:
            return ""
        if not descriptions or descriptions == "(no skills available)":
            return ""
        return (
            "<skill-catalog>\n"
            "Use load_skill(name=\"...\") when the user request matches a skill's "
            "description, triggers, tags, or required workflow. Load only the relevant "
            "skill body before applying it.\n\n"
            "Available skills:\n"
            f"{descriptions}\n"
            "</skill-catalog>"
        )

    def _runtime_guidance(self) -> str:
        return "\n".join(self._guidance_registry)

    @classmethod
    def register_guidance(cls, text: str) -> None:
        item = str(text or "").strip()
        if not item or item in cls._guidance_registry:
            return
        cls._guidance_registry.append(item)

    def _build_instruction_block(
        self,
        profile,
    ) -> tuple[str, list[ContextSection], list[dict[str, Any]]]:
        mode = str(getattr(profile, "tool_mode", "bot") or "bot")
        files = self._instruction_files(mode)

        grouped: dict[str, list[dict[str, Any]]] = {
            "mode_instructions": [],
            "project_instructions": [],
        }
        for section_name, path in files:
            text, raw_text, truncated = self._read_instruction_file(path)
            if text:
                source = _relative_or_name(path, self.instruction_root)
                grouped[section_name].append({
                    "source": source,
                    "text": text,
                    "raw_text": raw_text,
                    "truncated": truncated,
                })

        blocks = []
        report_sections = []
        reductions = []
        for section_name, items in grouped.items():
            if not items:
                continue
            rendered_text = "\n\n".join(item["text"] for item in items)
            raw_text = "\n\n".join(item["raw_text"] for item in items)
            budgeted = self.budgeter.apply(
                section_name,
                rendered_text,
                raw_text=raw_text,
            )
            if budgeted.reduction is not None:
                reductions.append(budgeted.reduction)
            sources = [item["source"] for item in items]
            blocks.append(
                f"<instructions section=\"{section_name}\" sources=\"{','.join(sources)}\">\n"
                f"{budgeted.rendered_text}\n"
                "</instructions>"
            )
            report_sections.append(
                ContextSection.from_text(
                    section_name,
                    budgeted.rendered_text,
                    raw_text=raw_text,
                    budget_chars=budgeted.budget_chars
                    if self.budgeter.enabled
                    else self.instruction_limit * len(items),
                    truncated=any(item["truncated"] for item in items)
                    or budgeted.truncated,
                    metadata={
                        "mode": mode,
                        "sources": sources,
                        **budgeted.metadata,
                        "files": [
                            {
                                "source": item["source"],
                                "raw_chars": len(item["raw_text"]),
                                "rendered_chars": len(item["text"]),
                                "truncated": bool(item["truncated"]),
                            }
                            for item in items
                        ],
                    },
                )
            )
        return "\n\n".join(blocks), report_sections, reductions

    def _instruction_files(self, mode: str) -> list[tuple[str, Path]]:
        if mode == "coding":
            return [
                ("mode_instructions", self.instruction_root / ".agent" / "coding.md"),
                ("project_instructions", self.instruction_root / "AGENTS.md"),
            ]
        return [
            ("mode_instructions", self.instruction_root / ".agent" / "assistant.md"),
        ]

    def _read_instruction_file(self, path: Path) -> tuple[str, str, bool]:
        if not path.is_file():
            return "", "", False
        path = path.resolve()
        try:
            stat = path.stat()
        except OSError:
            return "", "", False
        mtime_ns = stat.st_mtime_ns
        size = stat.st_size
        with self._instruction_cache_lock:
            cached = self._instruction_cache.get(path)
        if cached is not None and cached[0] == mtime_ns and cached[1] == size:
            text = cached[2]
        else:
            try:
                text = path.read_text(encoding="utf-8", errors="replace").strip()
            except OSError:
                return "", "", False
            with self._instruction_cache_lock:
                self._instruction_cache[path] = (mtime_ns, size, text)
        if len(text) <= self.instruction_limit:
            return text, text, False
        rendered = text[: self.instruction_limit].rstrip() + "\n\n...[truncated]"
        return rendered, text, True

    def _prefix_fingerprint(self, profile) -> str:
        mode = str(getattr(profile, "tool_mode", "bot") or "bot")
        payload = {
            "profile_name": str(getattr(profile, "name", "") or ""),
            "tool_mode": mode,
            "profile_prompt": str(getattr(profile, "system_prompt", "") or ""),
            "runtime_guidance": self._runtime_guidance(),
            "instruction_root": str(self.instruction_root),
            "instruction_limit": self.instruction_limit,
            "budgeter_enabled": self.budgeter.enabled,
            "budget_rules": {
                name: _budget_rule_signature(self.budgeter.rules.get(name))
                for name in ("mode_instructions", "project_instructions", "skill_catalog")
            },
            "instruction_files": [
                _instruction_file_signature(section, path)
                for section, path in self._instruction_files(mode)
            ],
            "skill_catalog": self._skill_catalog_signature(),
        }
        encoded = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            default=str,
        )
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()

    def _skill_catalog_signature(self) -> str:
        if self.skill_loader is None:
            return ""
        try:
            return str(self.skill_loader.get_descriptions() or "")
        except Exception as exc:
            return f"error:{type(exc).__name__}:{exc}"
    
    def _build_memory_block(self, session, *, current_request: str = "") -> str:
        if self.memory_store is None:
            return ""

        store = self.memory_store
        if hasattr(store, "for_session"):
            store = store.for_session(session)
        if current_request.strip() and hasattr(store, "recall"):
            text = store.recall(current_request).strip()
        else:
            text = store.read_all().strip()
        if text == "No relevant memory found.":
            return ""
        if not text:
            return ""

        return "<memory>\n" + text + "\n</memory>"

    def _build_retrieved_history_block(
        self,
        *,
        session,
        current_request: str,
        active_turn_messages: list[dict],
    ) -> tuple[str, list]:
        if self.history_vector_index is None:
            return "", []
        query = self._retrieval_query(current_request, active_turn_messages)
        if not query.strip():
            return "", []
        try:
            hits = self.history_vector_index.search(
                query=query,
                scope=self.history_scope_resolver(session),
                top_k=self.retrieval_top_k,
                min_score=self.retrieval_min_score,
            )
        except Exception:
            return "", []
        if not hits:
            return "", []
        lines = ["<retrieved_history>"]
        for index, hit in enumerate(hits, start=1):
            source = f" source_ref={hit.source_ref}" if hit.source_ref else ""
            full_count = ""
            if isinstance(getattr(hit, "metadata", None), dict):
                count = hit.metadata.get("message_count")
                if count is not None:
                    full_count = f" messages={count}"
            lines.append(
                f"[{index}] score={hit.score:.4f} source_type={hit.source_type}{source}{full_count}\n"
                f"{hit.text.strip()}"
            )
        lines.append("</retrieved_history>")
        return "\n\n".join(lines), hits

    def _retrieval_query(self, current_request: str, active_turn_messages: list[dict]) -> str:
        if current_request.strip():
            return current_request.strip()
        parts = []
        for message in active_turn_messages[-4:]:
            if not isinstance(message, dict):
                continue
            role = message.get("role", "")
            if role in {"user", "assistant"}:
                parts.append(_message_text(message))
        return "\n".join(part for part in parts if part.strip())

    def _build_security_knowledge_block(
        self,
        *,
        current_request: str,
        trace_store=None,
        run_state=None,
        trace_parent_span_id: str | None = None,
        reasoning_step: int | None = None,
    ) -> tuple[str, Any | None, list]:
        if not self.security_auto_context_enabled:
            return "", None, []
        if not current_request.strip():
            return "", None, []
        if self.security_retrieval_router is None or self.security_knowledge_index is None:
            return "", None, []
        total_timer = Timer()
        source = "context_auto"
        rag_span_id = security_rag_span_id(run_state, trace_parent_span_id, source)

        def append_runtime_event(event_name: str, payload: dict[str, Any]) -> None:
            append_security_rag_event(
                trace_store,
                run_state,
                event_name,
                {"source": source, **payload},
                span_id=rag_span_id if event_name in {SECURITY_RAG_STARTED, SECURITY_RAG_COMPLETED, SECURITY_RAG_FAILED} else None,
                parent_span_id=(
                    trace_parent_span_id
                    if event_name in {SECURITY_RAG_STARTED, SECURITY_RAG_COMPLETED, SECURITY_RAG_FAILED}
                    else rag_span_id
                ),
                step=reasoning_step,
            )

        append_runtime_event(SECURITY_RAG_STARTED, {
            "query": current_request,
            "entrypoint": "context_builder",
        })
        plan = None
        try:
            route_timer = Timer()
            router_trace_events: list[dict[str, Any]] = []

            def capture_router_trace(item: dict[str, Any]) -> None:
                if not isinstance(item, dict):
                    return
                event_name = str(item.get("event") or SECURITY_RAG_SEARCH_COMPLETED)
                payload = item.get("payload") if isinstance(item.get("payload"), dict) else item
                router_trace_events.append(dict(payload))
                append_runtime_event(event_name, dict(payload))

            if hasattr(self.security_retrieval_router, "route_with_retrieval"):
                plan = self.security_retrieval_router.route_with_retrieval(
                    current_request,
                    index=self.security_knowledge_index,
                    llm_classifier=self.security_route_classifier,
                    trace_callback=capture_router_trace,
                )
                decision = plan.decision
                hits = plan.hits
                route_action = getattr(plan, "action", "")
                search_trace = {
                    "router_searches": [
                        record.__dict__ for record in getattr(plan, "searches", [])
                    ],
                    "runtime_events": router_trace_events,
                }
            else:
                decision = self.security_retrieval_router.route(
                    current_request,
                    llm_classifier=self.security_route_classifier,
                )
                hits = []
                route_action = ""
                search_trace = {}
            route_ms = route_timer.ms()
        except Exception as exc:
            append_runtime_event(SECURITY_RAG_FAILED, {
                "query": current_request,
                "stage": "route",
                "error_type": type(exc).__name__,
                "error_message": str(exc),
                "latency_ms": {"total": total_timer.ms()},
            })
            write_rag_trace_if_enabled(make_rag_trace(
                source="context_auto",
                query=current_request,
                latency_ms={"total": total_timer.ms()},
                error=f"router_error:{type(exc).__name__}: {exc}",
            ))
            return (
                "<security_knowledge status=\"router_error\">\n"
                f"{type(exc).__name__}: {exc}\n"
                "</security_knowledge>",
                None,
                [],
            )
        if not getattr(decision, "use_rag", False):
            append_runtime_event(SECURITY_RAG_COMPLETED, rag_completed_payload(
                source=source,
                query=current_request,
                rewritten_query=getattr(decision, "query", current_request),
                decision=decision,
                action=route_action or "no_rag",
                reason=getattr(decision, "reason", "") or "Router skipped security RAG.",
                hits=[],
                latency_ms={"route_with_retrieval": route_ms, "total": total_timer.ms()},
                searches=getattr(plan, "searches", []) if plan is not None else [],
            ))
            write_rag_trace_if_enabled(make_rag_trace(
                source="context_auto",
                query=current_request,
                rewritten_query=getattr(decision, "query", current_request),
                router_decision=decision,
                latency_ms={"route_with_retrieval": route_ms, "total": total_timer.ms()},
            ))
            return "", decision, []
        if not hits:
            try:
                search_timer = Timer()
                legacy_search_trace: dict[str, Any] = {}
                hits = self.security_knowledge_index.search(
                    query=decision.query,
                    top_k=getattr(decision, "top_k", 5) or 5,
                    min_score=getattr(decision, "min_score", 0.0) or 0.0,
                    trace_callback=legacy_search_trace.update,
                )
                search_trace["legacy_search_ms"] = search_timer.ms()
                search_trace.update(legacy_search_trace)
                append_runtime_event(SECURITY_RAG_SEARCH_COMPLETED, search_trace_payload(
                    source=source,
                    query=decision.query,
                    stage="legacy_search",
                    retrieval_mode=str(legacy_search_trace.get("retrieval_mode") or ""),
                    top_k=getattr(decision, "top_k", 5) or 5,
                    min_score=getattr(decision, "min_score", 0.0) or 0.0,
                    hit_count=len(hits),
                    trace=legacy_search_trace,
                ))
            except Exception as exc:
                append_runtime_event(SECURITY_RAG_FAILED, {
                    "query": current_request,
                    "rewritten_query": getattr(decision, "query", current_request),
                    "stage": "search",
                    "route": getattr(decision, "route", ""),
                    "error_type": type(exc).__name__,
                    "error_message": str(exc),
                    "latency_ms": {"route": route_ms, "total": total_timer.ms()},
                })
                write_rag_trace_if_enabled(make_rag_trace(
                    source="context_auto",
                    query=current_request,
                    rewritten_query=getattr(decision, "query", current_request),
                    router_decision=decision,
                    latency_ms={"route": route_ms, "total": total_timer.ms()},
                    error=f"search_error:{type(exc).__name__}: {exc}",
                ))
                return (
                    "<security_knowledge status=\"search_error\">\n"
                    f"route={getattr(decision, 'route', '')} query={getattr(decision, 'query', '')}\n"
                    f"{type(exc).__name__}: {exc}\n"
                    "</security_knowledge>",
                    decision,
                    [],
                )
        append_runtime_event(SECURITY_RAG_COMPLETED, rag_completed_payload(
            source=source,
            query=current_request,
            rewritten_query=decision.query,
            decision=decision,
            action=route_action,
            reason=getattr(plan, "reason", "") if plan is not None else "",
            hits=hits,
            latency_ms={
                "route_with_retrieval": route_ms,
                **(search_trace.get("latency_ms") or {}),
                "total": total_timer.ms(),
            },
            searches=getattr(plan, "searches", []) if plan is not None else [],
        ))
        write_rag_trace_if_enabled(make_rag_trace(
            source="context_auto",
            query=current_request,
            rewritten_query=decision.query,
            router_decision=decision,
            hits=hits,
            latency_ms={
                "route_with_retrieval": route_ms,
                **(search_trace.get("latency_ms") or {}),
                "total": total_timer.ms(),
            },
        ))
        if not hits:
            return "", decision, []

        lines = [
            "<security_knowledge>",
            "Use these local code-security knowledge snippets as evidence. Prefer cited source paths when answering.",
            f"route={decision.route} action={route_action} confidence={decision.confidence:.4f} query={decision.query}",
        ]
        for index, hit in enumerate(hits, start=1):
            lines.append(
                f"[{index}] [{_score_tier(hit.score)}] score={hit.score:.4f} "
                f"source={hit.source_relpath} title={hit.title}\n"
                f"{hit.text.strip()}"
            )
        lines.append("</security_knowledge>")
        return "\n\n".join(lines), decision, hits

    def _build_report(
        self,
        state: BuildState,
    ) -> ContextBuildReport:
        messages = state.messages
        profile_prompt = state.profile_prompt
        instruction_sections = state.instruction_sections
        skill_catalog = state.skill_catalog
        runtime_guidance = state.runtime_guidance
        system_prompt = state.system_prompt
        budgeted_history = state.budgeted_history
        active_turn_messages = state.active_turn_messages
        budgeted_active_turn = state.budgeted_active_turn
        active_turn_start_index = state.active_turn_start_index
        current_request = state.current_request
        memory_block = state.memory_block
        raw_memory_block = state.raw_memory_block
        budgeted_memory = state.budgeted_memory
        working_memory_block = state.working_memory_block
        raw_working_memory_block = state.raw_working_memory_block
        budgeted_working_memory = state.budgeted_working_memory
        retrieved_history_block = state.retrieved_history_block
        raw_retrieved_history_block = state.raw_retrieved_history_block
        budgeted_retrieved_history = state.budgeted_retrieved_history
        retrieved_hits = state.retrieved_hits
        security_knowledge_block = state.security_knowledge_block
        raw_security_knowledge_block = state.raw_security_knowledge_block
        budgeted_security_knowledge = state.budgeted_security_knowledge
        security_decision = state.security_decision
        security_hits = state.security_hits
        inbox = state.inbox
        background_results = state.background_results
        task_runtime_events = state.task_runtime_events
        raw_task_runtime_events = state.raw_task_runtime_events
        budgeted_task_runtime_events = state.budgeted_task_runtime_events
        context_frame = state.context_frame
        reductions = state.reductions
        sections = [
            ContextSection.from_text("system_profile", profile_prompt),
            *instruction_sections,
            ContextSection.from_text(
                "skill_catalog",
                skill_catalog.rendered_text,
                raw_text=skill_catalog.raw_text,
                budget_chars=skill_catalog.budget_chars,
                truncated=skill_catalog.truncated,
                metadata={
                    "transport": "system_prompt",
                    **skill_catalog.metadata,
                },
            ),
            ContextSection.from_text("runtime_guidance", runtime_guidance),
            ContextSection.from_text(
                "system_prompt",
                system_prompt,
                metadata={
                    "composed": True,
                },
            ),
            ContextSection(
                name="conversation_history",
                raw_chars=budgeted_history.raw_chars,
                rendered_chars=budgeted_history.rendered_chars,
                budget_chars=budgeted_history.budget_chars,
                truncated=budgeted_history.truncated,
                metadata=budgeted_history.metadata,
            ),
            ContextSection.from_text(
                "current_request",
                current_request,
                metadata={
                    "transport": "chat_message",
                    "preserve": True,
                },
            ),
            ContextSection(
                name="active_turn",
                raw_chars=budgeted_active_turn.raw_chars,
                rendered_chars=budgeted_active_turn.rendered_chars,
                budget_chars=budgeted_active_turn.budget_chars,
                truncated=budgeted_active_turn.truncated,
                metadata={
                    "transport": "chat_messages",
                    "preserve": True,
                    "start_index": active_turn_start_index,
                    "message_count": len(active_turn_messages),
                    **budgeted_active_turn.metadata,
                },
            ),
            ContextSection.from_text(
                "memory",
                memory_block,
                raw_text=raw_memory_block,
                budget_chars=budgeted_memory.budget_chars,
                truncated=budgeted_memory.truncated,
                metadata={
                    "transport": "context_frame",
                    **budgeted_memory.metadata,
                },
            ),
            ContextSection.from_text(
                "working_memory",
                working_memory_block,
                raw_text=raw_working_memory_block,
                budget_chars=budgeted_working_memory.budget_chars,
                truncated=budgeted_working_memory.truncated,
                metadata={
                    "transport": "context_frame",
                    **budgeted_working_memory.metadata,
                },
            ),
            ContextSection.from_text(
                "retrieved_history",
                retrieved_history_block,
                raw_text=raw_retrieved_history_block,
                budget_chars=budgeted_retrieved_history.budget_chars,
                truncated=budgeted_retrieved_history.truncated,
                metadata={
                    "transport": "context_frame",
                    "hit_count": len(retrieved_hits),
                    "hits": [
                        {
                            "id": getattr(hit, "id", ""),
                            "score": getattr(hit, "score", 0.0),
                            "source_type": getattr(hit, "source_type", ""),
                            "source_ref": getattr(hit, "source_ref", ""),
                            "message_count": (
                                getattr(hit, "metadata", {}).get("message_count")
                                if isinstance(getattr(hit, "metadata", None), dict)
                                else None
                            ),
                        }
                        for hit in retrieved_hits
                    ],
                    **budgeted_retrieved_history.metadata,
                },
            ),
            ContextSection.from_text(
                "security_knowledge",
                security_knowledge_block,
                raw_text=raw_security_knowledge_block,
                budget_chars=budgeted_security_knowledge.budget_chars,
                truncated=budgeted_security_knowledge.truncated,
                metadata={
                    "transport": "context_frame",
                    "decision": (
                        security_decision.to_dict()
                        if hasattr(security_decision, "to_dict")
                        else None
                    ),
                    "hit_count": len(security_hits),
                    "hits": [
                        {
                            "id": getattr(hit, "id", ""),
                            "score": getattr(hit, "score", 0.0),
                            "source": getattr(hit, "source_relpath", ""),
                            "title": getattr(hit, "title", ""),
                            "chunk_index": getattr(hit, "chunk_index", 0),
                        }
                        for hit in security_hits
                    ],
                    **budgeted_security_knowledge.metadata,
                },
            ),
            ContextSection.from_text(
                "task_runtime_events",
                task_runtime_events,
                raw_text=raw_task_runtime_events,
                budget_chars=budgeted_task_runtime_events.budget_chars,
                truncated=budgeted_task_runtime_events.truncated,
                metadata={
                    "inbox_count": len(inbox),
                    "background_result_count": len(background_results),
                    "transport": "context_frame",
                    **budgeted_task_runtime_events.metadata,
                },
            ),
            ContextSection.from_text(
                "inbox",
                json.dumps(inbox, ensure_ascii=False, default=str) if inbox else "",
                metadata={"item_count": len(inbox)},
            ),
            ContextSection.from_text(
                "background_results",
                json.dumps(background_results, ensure_ascii=False, default=str)
                if background_results
                else "",
                metadata={"item_count": len(background_results)},
            ),
            ContextSection.from_text("context_frame", context_frame),
        ]
        return ContextBuildReport(
            total_chars=message_chars(messages),
            budget_chars=self.budgeter.total_budget_chars if self.budgeter.enabled else None,
            over_budget=(
                self.budgeter.enabled
                and self.budgeter.total_budget_chars is not None
                and message_chars(messages) > self.budgeter.total_budget_chars
            ),
            sections=sections,
            reductions=reductions,
            metadata={
                "message_count": len(messages),
                "section_budget_enabled": self.budgeter.enabled,
                "prefix_fingerprint": state.prefix_fingerprint,
                "prefix_cache_hit": state.prefix_cache_hit,
                "prefix": state.prefix_metadata,
            },
        )

    def _split_active_turn(
        self,
        session_messages: list[dict],
        *,
        active_turn_start_index: int | None = None,
    ) -> tuple[list[dict], list[dict], str]:
        if active_turn_start_index is not None:
            index = max(0, min(int(active_turn_start_index), len(session_messages)))
            history_messages = list(session_messages[:index])
            active_turn_messages = list(session_messages[index:])
            return (
                history_messages,
                active_turn_messages,
                self._current_request_from_active_turn(active_turn_messages),
            )

        history_messages = list(session_messages)
        for index in range(len(history_messages) - 1, -1, -1):
            message = history_messages[index]
            if not isinstance(message, dict):
                continue
            if str(message.get("role") or "") != "user":
                continue
            current = _message_text(message)
            current_message = history_messages.pop(index)
            return history_messages, [current_message], current
        return history_messages, [], ""

    def _current_request_from_active_turn(self, messages: list[dict]) -> str:
        for message in messages or []:
            if not isinstance(message, dict):
                continue
            if str(message.get("role") or "") == "user":
                return _message_text(message)
        return ""

    def _budget_conversation_history_for_profile(
        self,
        history_messages: list[dict],
        *,
        profile,
        session=None,
    ) -> BudgetedMessages:
        rule = self.budgeter.rules.get("conversation_history")
        if not self._coding_uses_active_turn_only_history(profile, session=session):
            return budget_conversation_history(
                history_messages,
                enabled=self.budgeter.enabled,
                rule=rule,
            )

        raw_messages = _copy_context_messages(history_messages)
        raw_chars = message_chars(raw_messages)
        transferred_budget = max(0, int(getattr(rule, "budget_chars", 0) or 0))
        transferred_floor = max(0, int(getattr(rule, "floor_chars", 0) or 0))
        reduction = None
        if raw_messages:
            reduction = {
                "section": "conversation_history",
                "reason": "coding_history_budget_transferred_to_active_turn",
                "before_chars": raw_chars,
                "after_chars": 0,
                "budget_chars": 0,
                "floor_chars": 0,
                "strategy": "drop_for_coding",
                "before_messages": len(raw_messages),
                "after_messages": 0,
                "transferred_budget_chars": transferred_budget,
                "transferred_floor_chars": transferred_floor,
            }
        return BudgetedMessages(
            name="conversation_history",
            raw_messages=raw_messages,
            rendered_messages=[],
            budget_chars=0,
            floor_chars=0,
            strategy="drop_for_coding",
            truncated=bool(raw_messages),
            reduction=reduction,
            metadata={
                "budget_enabled": bool(self.budgeter.enabled),
                "strategy": "drop_for_coding",
                "transport": "chat_messages",
                "message_count": len(raw_messages),
                "rendered_message_count": 0,
                "coding_active_turn_only": True,
                "transferred_to": "active_turn",
                "transferred_budget_chars": transferred_budget,
                "transferred_floor_chars": transferred_floor,
            },
        )

    def _active_turn_rule_for_profile(self, profile, *, session=None) -> Any:
        rule = self.budgeter.rules.get("active_turn")
        if rule is None or not self._coding_uses_active_turn_only_history(
            profile,
            session=session,
        ):
            return rule
        history_rule = self.budgeter.rules.get("conversation_history")
        if history_rule is None:
            return rule
        return replace(
            rule,
            budget_chars=max(1, int(rule.budget_chars) + int(history_rule.budget_chars)),
            floor_chars=max(0, int(rule.floor_chars) + int(history_rule.floor_chars)),
        )

    def _annotate_active_turn_budget_for_profile(
        self,
        budgeted: BudgetedMessages,
        *,
        profile,
        session=None,
    ) -> BudgetedMessages:
        if not self._coding_uses_active_turn_only_history(profile, session=session):
            return budgeted
        history_rule = self.budgeter.rules.get("conversation_history")
        transferred_budget = max(0, int(getattr(history_rule, "budget_chars", 0) or 0))
        transferred_floor = max(0, int(getattr(history_rule, "floor_chars", 0) or 0))
        metadata = {
            **budgeted.metadata,
            "coding_conversation_history_budget_transferred": True,
            "transferred_from": "conversation_history",
            "transferred_budget_chars": transferred_budget,
            "transferred_floor_chars": transferred_floor,
        }
        reduction = budgeted.reduction
        if reduction is not None:
            reduction = {
                **reduction,
                "transferred_from": "conversation_history",
                "transferred_budget_chars": transferred_budget,
                "transferred_floor_chars": transferred_floor,
            }
        return replace(
            budgeted,
            metadata=metadata,
            reduction=reduction,
        )

    def _coding_context_state_enabled(self, profile, *, session=None) -> bool:
        return bool(CODING_CONTEXT_STATE_ENABLED) and self._coding_uses_active_turn_only_history(
            profile,
            session=session,
        )

    def _can_reuse_prefix_history(
        self,
        prefix: ContextPrefix,
        *,
        profile,
        session=None,
        active_turn_start_index: int | None,
    ) -> bool:
        if prefix.budgeted_history is None:
            return False
        if prefix.active_turn_start_index != active_turn_start_index:
            return False
        coding_context = self._coding_uses_active_turn_only_history(
            profile,
            session=session,
        )
        history_is_coding = bool(
            (prefix.budgeted_history.metadata or {}).get("coding_active_turn_only")
        )
        return history_is_coding if coding_context else not history_is_coding

    def _can_reuse_prefix_messages(
        self,
        prefix: ContextPrefix,
        *,
        profile,
        session=None,
        active_turn_start_index: int | None,
    ) -> bool:
        if not prefix.messages:
            return False
        return self._can_reuse_prefix_history(
            prefix,
            profile=profile,
            session=session,
            active_turn_start_index=active_turn_start_index,
        )

    def _coding_uses_active_turn_only_history(self, profile, *, session=None) -> bool:
        if _normalized_mode(getattr(profile, "tool_mode", "")) == "coding":
            return True
        if str(getattr(profile, "name", "") or "").startswith("subagent:"):
            return True
        if session is None:
            return False
        if _normalized_mode(getattr(session, "current_mode", "")) == "coding":
            return True
        metadata = getattr(session, "metadata", {}) or {}
        if str(metadata.get("kind") or "") == "subagent":
            return True
        for key in ("mode", "tool_mode", "task_type"):
            if _normalized_mode(metadata.get(key)) == "coding":
                return True
        return False

    def _build_task_runtime_events_block(
        self,
        *,
        inbox: list,
        background_results: list,
    ) -> str:
        parts = []
        if inbox:
            lines = ["<inbox>", "Recent inbox messages:"]
            for index, item in enumerate(inbox, start=1):
                if isinstance(item, dict):
                    sender = item.get("from") or item.get("sender") or item.get("source") or "unknown"
                    msg_type = item.get("type") or item.get("kind") or "message"
                    body = (
                        item.get("body")
                        or item.get("content")
                        or item.get("message")
                        or item.get("payload")
                        or ""
                    )
                    lines.append(f"[inbox] {sender} ({msg_type}): {_squash(str(body), 500)}")
                else:
                    lines.append(f"[inbox] {_squash(str(item), 500)}")
            lines.append("</inbox>")
            parts.append("\n".join(lines))
        if background_results:
            lines = ["<background-results>", "Background task updates:"]
            for index, item in enumerate(background_results, start=1):
                if isinstance(item, dict):
                    task_id = item.get("task_id") or item.get("id") or f"item-{index}"
                    status = item.get("status") or "unknown"
                    result = item.get("result") or item.get("content") or item.get("message") or ""
                    lines.append(f"[bg:{task_id}] {status}: {_squash(str(result), 700)}")
                else:
                    lines.append(f"[bg:item-{index}] {_squash(str(item), 700)}")
            lines.append("</background-results>")
            parts.append("\n".join(lines))
        return "\n\n".join(parts)

    def _reduction_list(
        self,
        *items: BudgetedText | BudgetedMessages,
    ) -> list[dict[str, Any]]:
        return [item.reduction for item in items if item.reduction is not None]


def _relative_or_name(path: Path, root: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return path.name


def _instruction_file_signature(section: str, path: Path) -> dict[str, Any]:
    try:
        resolved = path.resolve()
        stat = resolved.stat()
    except OSError:
        return {
            "section": section,
            "path": str(path),
            "exists": False,
        }
    return {
        "section": section,
        "path": str(resolved),
        "exists": True,
        "mtime_ns": stat.st_mtime_ns,
        "size": stat.st_size,
    }


def _budget_rule_signature(rule) -> dict[str, Any] | None:
    if rule is None:
        return None
    return {
        "budget_chars": getattr(rule, "budget_chars", None),
        "floor_chars": getattr(rule, "floor_chars", None),
        "strategy": getattr(rule, "strategy", None),
        "keep_head_turns": getattr(rule, "keep_head_turns", None),
        "keep_tail_turns": getattr(rule, "keep_tail_turns", None),
        "summary_chars": getattr(rule, "summary_chars", None),
        "keep_recent_results": getattr(rule, "keep_recent_results", None),
        "preserve_tools": list(getattr(rule, "preserve_tools", ()) or ()),
    }


def _prefix_messages_fingerprint(
    base_fingerprint: str,
    messages: list[dict],
    *,
    active_turn_start_index: int | None,
) -> str:
    payload = {
        "base_fingerprint": base_fingerprint,
        "active_turn_start_index": active_turn_start_index,
        "messages": messages,
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        default=str,
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _message_text(message: dict) -> str:
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict):
                parts.append(str(item.get("text") or item.get("content") or ""))
            else:
                parts.append(str(item))
        return "".join(parts)
    return str(content or "")


def _normalized_mode(value: Any) -> str:
    return str(value or "").strip().lower()


def _copy_context_messages(messages: list[dict]) -> list[dict]:
    copied = []
    for message in messages or []:
        if isinstance(message, dict):
            copied.append(dict(message))
        else:
            copied.append(message)
    return copied


def _score_tier(score: float) -> str:
    try:
        value = float(score)
    except (TypeError, ValueError):
        return "UNKNOWN"
    if value >= 0.80:
        return "HIGH"
    if value >= 0.60:
        return "MEDIUM"
    return "LOW"


def _squash(text: str, limit: int) -> str:
    rendered = " ".join(str(text or "").split())
    if len(rendered) <= limit:
        return rendered
    return rendered[: max(0, limit - 3)].rstrip() + "..."
