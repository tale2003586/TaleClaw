from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from runtime.context_budget import BudgetedText
from runtime.context_history import BudgetedMessages
from runtime.context_sections import ContextSection


@dataclass
class BuildState:
    messages: list[dict]
    profile_prompt: str
    instruction_sections: list[ContextSection]
    runtime_guidance: str
    system_prompt: str
    session_messages: list[dict]
    history_messages: list[dict]
    budgeted_history: BudgetedMessages
    active_turn_messages: list[dict]
    budgeted_active_turn: BudgetedMessages
    active_turn_start_index: int | None
    current_request: str
    memory_block: str
    raw_memory_block: str
    budgeted_memory: BudgetedText
    working_memory_block: str
    raw_working_memory_block: str
    budgeted_working_memory: BudgetedText
    retrieved_history_block: str
    raw_retrieved_history_block: str
    budgeted_retrieved_history: BudgetedText
    retrieved_hits: list
    security_knowledge_block: str
    raw_security_knowledge_block: str
    budgeted_security_knowledge: BudgetedText
    security_decision: Any | None
    security_hits: list
    inbox: list
    background_results: list
    task_runtime_events: str
    raw_task_runtime_events: str
    budgeted_task_runtime_events: BudgetedText
    context_frame: str
    reductions: list[dict[str, Any]]
