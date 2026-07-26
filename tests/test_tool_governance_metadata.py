from tools.governance import (
    ContextEffect,
    MemoryEffect,
    ToolGovernanceMetadata,
    ToolRiskLevel,
    ToolScope,
    ToolStateEffect,
    governance_for_tool,
)
from tools.tool_registry import ToolRegistry


def _schema(name: str = "example") -> dict:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": "Example tool.",
            "parameters": {"type": "object", "properties": {}},
        },
    }


def test_registration_defaults_are_inert_and_do_not_change_schema_or_execution() -> None:
    registry = ToolRegistry()
    schema = _schema()
    registry.register(schema, lambda: "ok")

    assert registry.schemas_for_mode() == [schema]
    assert registry.execute("example", {}) == "ok"
    assert registry.governance_catalog() == [{
        "name": "example",
        "tool_scope": "user",
        "state_effect": "none",
        "risk_level": "low",
        "requires_audit": False,
        "allowed_modes": [],
        "policy_tag": "",
        "memory_effect": "none",
        "context_effect": "none",
    }]


def test_kernel_tools_default_to_audited_metadata() -> None:
    metadata = ToolGovernanceMetadata(tool_scope=ToolScope.KERNEL)

    assert metadata.requires_audit is True


def test_governance_catalog_can_filter_without_affecting_original_catalog() -> None:
    registry = ToolRegistry()
    metadata = ToolGovernanceMetadata(
        tool_scope=ToolScope.KERNEL,
        state_effect=ToolStateEffect.RUNTIME_STATE,
        risk_level=ToolRiskLevel.HIGH,
        allowed_modes=("coding",),
        policy_tag="runtime.control",
        context_effect=ContextEffect.CHECKPOINT,
    )
    registry.register(_schema("checkpoint"), lambda: "ok", governance=metadata)

    assert registry.catalog() == [{
        "name": "checkpoint",
        "description": "Example tool.",
        "risk": "normal",
        "source": "local",
        "allowed_agents": None,
        "always_on": False,
    }]
    assert registry.governance_catalog()[0]["context_effect"] == "checkpoint"
    assert registry.governance_catalog()[0]["requires_audit"] is True


def test_memory_tools_have_explicit_runtime_classifications() -> None:
    write = governance_for_tool("memorize")
    read = governance_for_tool("recall_memory")

    assert (write.memory_effect, write.state_effect) == (
        MemoryEffect.WRITE,
        ToolStateEffect.AGENT_STATE,
    )
    assert write.requires_audit is True
    assert read.memory_effect is MemoryEffect.READ
    assert read.context_effect is ContextEffect.INJECT
    assert read.requires_audit is True
