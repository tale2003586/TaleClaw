import ast
from pathlib import Path
from types import SimpleNamespace

from runtime.execution.loop_policies import (
    ToolBatchPolicy,
    WebSearchBudgetPolicy,
    standard_execution_policies,
)
from runtime.execution.policy_set import ExecutionPolicies


ROOT = Path(__file__).resolve().parents[1]


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    imports = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imports.add(node.module)
    return imports


def test_kernel_construction_modules_do_not_import_concrete_optional_policies():
    for relative in (
        "runtime/execution/agent_runner.py",
        "runtime/execution/reasoning_loop.py",
    ):
        imports = _imports(ROOT / relative)
        assert "runtime.execution.loop_policies" not in imports
        assert "runtime.working_memory" not in imports


def test_minimal_policies_have_no_metadata_or_batch_side_effects():
    session = SimpleNamespace(metadata={})
    policies = ExecutionPolicies.minimal(24)

    assert policies.web_search.denial(session, "web_search") == ""
    assert policies.web_search.add_notice(session, "web_search", "result") == "result"
    assert policies.tool_batch.should_parallelize_tasks([], available=True) is False
    assert policies.tool_batch.should_batch_reads([], available=True) is False
    assert session.metadata == {}


def test_standard_policy_factory_explicitly_enables_product_policies():
    policies = standard_execution_policies(24)

    assert isinstance(policies.web_search, WebSearchBudgetPolicy)
    assert isinstance(policies.tool_batch, ToolBatchPolicy)
