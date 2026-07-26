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


def test_execution_can_emit_serialized_governance_trace_without_affecting_result() -> None:
    events = []
    class Trace:
        def append_event(self, run_state, event_name, payload, **kwargs):
            events.append((event_name, payload))

    registry = ToolRegistry()
    registry.register(
        _schema("checkpoint"),
        lambda: "ok",
        governance=ToolGovernanceMetadata(
            tool_scope=ToolScope.KERNEL,
            context_effect=ContextEffect.CHECKPOINT,
        ),
    )

    assert registry.execute(
        "checkpoint", {}, trace_store=Trace(), run_state=object()
    ) == "ok"
    assert events == [("tool.governance.observed", {
        "tool_name": "checkpoint",
        "tool_scope": "kernel",
        "state_effect": "none",
        "risk_level": "low",
        "requires_audit": True,
        "allowed_modes": [],
        "policy_tag": "",
        "memory_effect": "none",
        "context_effect": "checkpoint",
    })]


def test_governance_trace_failure_does_not_block_tool() -> None:
    class BrokenTrace:
        def append_event(self, *args, **kwargs):
            raise OSError("trace unavailable")

    registry = ToolRegistry()
    registry.register(_schema(), lambda: "ok")
    assert registry.execute(
        "example", {}, trace_store=BrokenTrace(), run_state=object()
    ) == "ok"
