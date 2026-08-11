"""Policies applied during the reasoning loop."""

from __future__ import annotations

import math

from config import (
    REASONING_FINISHING_REMINDER_RATIO,
)
from runtime.trace.trace_store import event_preview
from runtime.execution.policy_set import ExecutionPolicies


class WebSearchBudgetPolicy:
    def denial(self, session, tool_name: str, *, state=None) -> str:
        if tool_name != "web_search":
            return ""
        limit = state.web_search_limit if state is not None else 0
        if limit <= 0:
            return ""
        used = state.web_search_used if state is not None else 0
        remaining = max(0, limit - used)
        if remaining <= 0:
            if state is not None:
                state.web_search_remaining = 0
            return (
                "Error: web_search budget exhausted for this turn. "
                "Do not call web_search again; answer using the search results "
                "and context already available."
            )
        used += 1
        if state is not None:
            state.web_search_used = used
            state.web_search_remaining = max(0, limit - used)
        return ""

    def add_notice(self, session, tool_name: str, output: str, *, state=None) -> str:
        if tool_name != "web_search":
            return output
        limit = state.web_search_limit if state is not None else 0
        if limit <= 0:
            return output
        used = state.web_search_used if state is not None else 0
        remaining = state.web_search_remaining if state is not None else 0
        if remaining > 0:
            instruction = (
                f"You have {remaining} web_search calls remaining in this turn "
                f"out of {limit}. Before searching again, consolidate queries and "
                "only call web_search if the existing results are insufficient."
            )
        else:
            instruction = (
                f"You have used all {limit} web_search calls for this turn. "
                "Do not call web_search again; complete the answer using the "
                "results and context already available."
            )
        return (
            '<web-search-budget remaining="'
            f'{remaining}" used="{used}" limit="{limit}">\n'
            f"{instruction}\n"
            "</web-search-budget>\n\n"
            f"{output}"
        )


class FinishingReminderPolicy:
    def __init__(self, max_reasoning_steps: int) -> None:
        self.max_reasoning_steps = max_reasoning_steps

    def inject(
        self,
        session,
        reasoning_steps: int,
        *,
        trace=None,
        state=None,
    ) -> None:
        if state is None or state.finishing_reminder_sent:
            return
        reminder_step = self._reminder_step()
        if reasoning_steps < reminder_step:
            return
        remaining_steps = max(0, self.max_reasoning_steps - reasoning_steps)
        message = finishing_reminder_message(
            reasoning_steps=reasoning_steps,
            max_reasoning_steps=self.max_reasoning_steps,
            remaining_steps=remaining_steps,
        )
        session.add_message(
            "system",
            message,
            metadata={
                "kind": "runtime_finishing_reminder",
                "reason": "reasoning_budget",
                "step": reasoning_steps,
                "max_reasoning_steps": self.max_reasoning_steps,
                "remaining_steps": remaining_steps,
            },
        )
        state.finishing_reminder_sent = True
        if trace is not None:
            trace(
                "reasoning.finishing_reminder.injected",
                {
                    "step": reasoning_steps,
                    "max_reasoning_steps": self.max_reasoning_steps,
                    "remaining_steps": remaining_steps,
                    "ratio": REASONING_FINISHING_REMINDER_RATIO,
                    "message_preview": event_preview(message),
                },
            )

    def _reminder_step(self) -> int:
        ratio = REASONING_FINISHING_REMINDER_RATIO
        if ratio <= 0:
            return self.max_reasoning_steps + 1
        return max(
            1,
            min(
                self.max_reasoning_steps,
                math.ceil(self.max_reasoning_steps * ratio),
            ),
        )


def standard_execution_policies(max_reasoning_steps: int) -> ExecutionPolicies:
    return ExecutionPolicies(
        web_search=WebSearchBudgetPolicy(),
        finishing=FinishingReminderPolicy(max_reasoning_steps),
    )

def finishing_reminder_message(
    *,
    reasoning_steps: int,
    max_reasoning_steps: int,
    remaining_steps: int,
) -> str:
    return (
        '<runtime-finishing-reminder reason="reasoning_budget" '
        f'step="{reasoning_steps}" max_steps="{max_reasoning_steps}" '
        f'remaining_steps="{remaining_steps}">\n'
        f"You have used {reasoning_steps}/{max_reasoning_steps} reasoning steps. "
        "If the core task can already be answered, stop supplemental evidence "
        "gathering and provide the final answer now. Treat exact line numbers, "
        "citations, and formatting polish as nice-to-have rather than blockers. "
        "Do not call nl, rg, grep, read_file, or bash only to perfect references; "
        "use remaining tools only for blockers required to answer correctly.\n"
        "</runtime-finishing-reminder>"
    )
