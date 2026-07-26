from __future__ import annotations

import inspect

from runtime.context import ContextBuilder, ContextMemoryService, PromptAssetsService
from runtime.context.budget import ContextBudgeter


LEGACY_CONTEXT_PARAMETERS = {
    "memory_store",
    "instruction_root",
    "instruction_limit",
    "skill_loader",
    "working_memory_renderer",
}


def test_context_builder_accepts_only_explicit_asset_and_memory_services():
    parameters = set(inspect.signature(ContextBuilder).parameters)

    assert {"prompt_assets_service", "memory_service"} <= parameters
    assert parameters.isdisjoint(LEGACY_CONTEXT_PARAMETERS)


def test_prompt_asset_service_budgeter_becomes_builder_budgeter():
    budgeter = ContextBudgeter.from_env()
    assets = PromptAssetsService(budgeter=budgeter)
    memory = ContextMemoryService()

    builder = ContextBuilder(
        prompt_assets_service=assets,
        memory_service=memory,
    )

    assert builder.budgeter is budgeter
    assert builder.prompt_assets_service is assets
    assert builder.memory_service is memory
