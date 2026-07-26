"""Composable context providers used by ContextBuilder.

Providers own data selection and budgeting. ContextBuilder remains the ordered
composer during the compatibility phase so prompt and token behavior stay stable.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


class PromptContextProvider:
    name = "prompt"

    def provide(
        self,
        builder,
        *,
        profile,
        session,
        active_turn_start_index: int | None,
        prefix=None,
    ):
        return prefix or builder.build_prefix(
            profile,
            session=session,
            active_turn_start_index=active_turn_start_index,
        )


@dataclass(frozen=True)
class HistoryContext:
    session_messages: list[dict]
    history_messages: list[dict]
    active_turn_messages: list[dict]
    current_request: str
    budgeted_history: Any


class HistoryContextProvider:
    name = "history"

    def provide(
        self,
        builder,
        *,
        session,
        profile,
        prefix,
        active_turn_start_index: int | None,
        include_history: bool = True,
    ) -> HistoryContext:
        session_messages = list(session.messages)
        history, active, request = builder._split_active_turn(
            session_messages,
            active_turn_start_index=active_turn_start_index,
        )
        if not include_history:
            history = []
            budgeted = builder._budget_conversation_history_for_profile(
                [],
                profile=profile,
                session=session,
            )
        elif builder._can_reuse_prefix_history(
            prefix,
            profile=profile,
            session=session,
            active_turn_start_index=active_turn_start_index,
        ):
            history = list(prefix.history_messages)
            budgeted = prefix.budgeted_history
        else:
            budgeted = builder._budget_conversation_history_for_profile(
                history,
                profile=profile,
                session=session,
            )
        return HistoryContext(
            session_messages=session_messages,
            history_messages=history,
            active_turn_messages=active,
            current_request=request,
            budgeted_history=budgeted,
        )


@dataclass(frozen=True)
class MemoryContext:
    raw_memory: str
    budgeted_memory: Any
    raw_working_memory: str
    budgeted_working_memory: Any


class MemoryContextProvider:
    name = "memory"

    def provide(
        self,
        builder,
        *,
        session,
        profile,
        current_request: str,
        include_memory: bool = True,
    ):
        raw_memory = (
            builder.memory_service.build_memory_block(
                session,
                current_request=current_request,
            )
            if include_memory
            else ""
        )
        raw_working = builder._build_working_memory_block(
            session,
            profile=profile,
        )
        return MemoryContext(
            raw_memory=raw_memory,
            budgeted_memory=builder.budgeter.apply("memory", raw_memory),
            raw_working_memory=raw_working,
            budgeted_working_memory=builder.budgeter.apply(
                "working_memory",
                raw_working,
            ),
        )


@dataclass(frozen=True)
class RetrievalContext:
    raw_history: str
    history_hits: list
    budgeted_history: Any
    raw_security: str
    security_decision: Any
    security_hits: list
    budgeted_security: Any


class RetrievalContextProvider:
    name = "retrieval"

    def provide(
        self,
        builder,
        *,
        session,
        current_request: str,
        active_turn_messages: list[dict],
        include_security_knowledge: bool,
        trace_store=None,
        run_state=None,
        trace_parent_span_id: str | None = None,
        reasoning_step: int | None = None,
    ) -> RetrievalContext:
        service = builder.retrieval_service
        if service is None:
            raw_history, history_hits = "", []
        else:
            raw_history, history_hits = service.retrieve_history(
                session=session,
                current_request=current_request,
                active_turn_messages=active_turn_messages,
            )
        if include_security_knowledge and service is not None:
            raw_security, decision, security_hits = service.retrieve_security(
                current_request=current_request,
                trace_store=trace_store,
                run_state=run_state,
                trace_parent_span_id=trace_parent_span_id,
                reasoning_step=reasoning_step,
            )
        else:
            raw_security, decision, security_hits = "", None, []
        return RetrievalContext(
            raw_history=raw_history,
            history_hits=history_hits,
            budgeted_history=builder.budgeter.apply(
                "retrieved_history",
                raw_history,
            ),
            raw_security=raw_security,
            security_decision=decision,
            security_hits=security_hits,
            budgeted_security=builder.budgeter.apply(
                "security_knowledge",
                raw_security,
            ),
        )


class CodingContextProvider:
    name = "coding"

    def enabled(self, builder, *, session, profile) -> bool:
        return builder._coding_context_state_enabled(profile, session=session)


class EmptyMemoryContextProvider:
    name = "memory"

    def provide(self, builder, **kwargs) -> MemoryContext:
        empty = builder.budgeter.apply("memory", "")
        working = builder.budgeter.apply("working_memory", "")
        return MemoryContext("", empty, "", working)


class EmptyRetrievalContextProvider:
    name = "retrieval"

    def provide(self, builder, **kwargs) -> RetrievalContext:
        return RetrievalContext(
            raw_history="",
            history_hits=[],
            budgeted_history=builder.budgeter.apply("retrieved_history", ""),
            raw_security="",
            security_decision=None,
            security_hits=[],
            budgeted_security=builder.budgeter.apply("security_knowledge", ""),
        )


class NoCodingContextProvider:
    name = "coding"

    def enabled(self, builder, **kwargs) -> bool:
        return False


MINIMAL_CONTEXT_PROVIDERS = (
    PromptContextProvider(),
    HistoryContextProvider(),
    EmptyMemoryContextProvider(),
    EmptyRetrievalContextProvider(),
    NoCodingContextProvider(),
)


DEFAULT_CONTEXT_PROVIDERS = (
    PromptContextProvider(),
    HistoryContextProvider(),
    MemoryContextProvider(),
    RetrievalContextProvider(),
    CodingContextProvider(),
)
