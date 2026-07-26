from runtime.context.budget import ContextBudgeter, SectionBudgetRule

from applications.minecraft.catalog import CATALOG
from applications.minecraft.context import MinecraftPlannerContextBuilder
from applications.minecraft.memory_adapter import MinecraftMemoryAdapter
from applications.minecraft.models import MinecraftTask, ResourceGoal
from applications.minecraft.world_state import BeliefWorldState


class Memory:
    def __init__(self):
        self.queries = []

    def recall(self, query):
        self.queries.append(query)
        return "relevant successful plan " * 100

    def append_pending(self, content, **_kwargs):
        return content


def test_memory_only_reads_for_cognitive_purposes():
    store = Memory()
    adapter = MinecraftMemoryAdapter(store, max_chars=100)
    assert adapter.retrieve("wood", purpose="ordinary_action") == ""
    assert adapter.read_count == 0
    assert len(adapter.retrieve("wood", purpose="initial_plan")) == 100
    assert adapter.read_count == 1


def test_planner_context_is_bounded_and_has_no_chat_history():
    budgeter = ContextBudgeter(
        enabled=True,
        total_budget_chars=2000,
        rules={
            "memory": SectionBudgetRule("memory", 100, 20, "head"),
            "active_turn": SectionBudgetRule("active_turn", 500, 100, "head_tail"),
        },
    )
    context = MinecraftPlannerContextBuilder(
        budgeter=budgeter,
        memory=MinecraftMemoryAdapter(Memory(), max_chars=100),
        max_chars=2000,
    )
    task = MinecraftTask(
        user_id="u",
        session_id="s",
        bot_id="b",
        goal=ResourceGoal(resource="oak_log", quantity=4),
    )
    messages = context.build(
        task=task,
        world=BeliefWorldState(task_id=task.task_id),
        catalog=CATALOG,
    )
    rendered = "\n".join(item["content"] for item in messages)
    assert len(rendered) < 3000
    assert "ordinary chat" not in rendered
    assert "<memory>" in rendered
