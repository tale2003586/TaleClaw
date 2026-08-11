import ast
from pathlib import Path
import json
import os
import subprocess
import sys
from types import SimpleNamespace

from runtime.execution.loop_policies import (
    WebSearchBudgetPolicy,
    standard_execution_policies,
)
from runtime.execution.policy_set import ExecutionPolicies


ROOT = Path(__file__).resolve().parents[1]


def test_bootstrap_does_not_import_memory_implementations_when_disabled() -> None:
    env = {
        **os.environ,
        "SEMANTIC_MEMORY_ENABLED": "0",
        "SEMANTIC_MEMORY_WRITE_ENABLED": "0",
        "EPISODIC_MEMORY_ENABLED": "0",
    }
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import json,sys; import applications.bootstrap; "
                "print(json.dumps(sorted(n for n in sys.modules if n.startswith('memory.'))))"
            ),
        ],
        cwd=ROOT,
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )

    assert json.loads(completed.stdout) == []


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


def test_minimal_policies_have_no_metadata_side_effects():
    session = SimpleNamespace(metadata={})
    policies = ExecutionPolicies.minimal(24)

    assert policies.web_search.denial(session, "web_search") == ""
    assert policies.web_search.add_notice(session, "web_search", "result") == "result"
    assert session.metadata == {}


def test_standard_policy_factory_explicitly_enables_product_policies():
    policies = standard_execution_policies(24)

    assert isinstance(policies.web_search, WebSearchBudgetPolicy)
