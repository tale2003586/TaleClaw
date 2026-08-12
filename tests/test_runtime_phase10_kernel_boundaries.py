import ast
from pathlib import Path

from runtime.context import ContextBuilder
from runtime.ports import (
    ContextPort,
    ModelPort,
    ObservabilityPort,
    ToolExecutorPort,
    ToolPort,
)
from tests.fakes.fake_tools import RecordingTool, registry_with_tool
from tests.fakes.scripted_model import FinalResponse, ScriptedModel
from tools.executor import ToolExecutor


ROOT = Path(__file__).resolve().parents[1]
EXECUTION_ROOT = ROOT / "runtime" / "execution"
FORBIDDEN_EXECUTION_IMPORTS = (
    "applications",
    "agents.subagent",
    "gateway",
    "knowledge",
    "plugins",
    "retrieval",
    "web",
)


class ObservabilityFixture:
    def append_event(self, run_state, event, payload, **kwargs):
        return None

    def write_run_state(self, run_state):
        return None


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    imports: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imports.add(node.module)
    return imports


def test_execution_kernel_does_not_import_product_layers():
    violations = []
    for path in sorted(EXECUTION_ROOT.glob("*.py")):
        if path.name.startswith("._"):
            continue
        for imported in _imports(path):
            if imported.startswith(FORBIDDEN_EXECUTION_IMPORTS):
                violations.append(f"{path.name}: {imported}")
    assert violations == []


def test_reasoning_loop_has_no_direct_working_memory_dependency():
    imports = _imports(EXECUTION_ROOT / "reasoning_loop.py")
    assert "runtime.working_memory" not in imports


def test_current_components_satisfy_kernel_ports():
    registry = registry_with_tool(
        "example_tool",
        RecordingTool(output="ok"),
        modes={"bot"},
    )
    provider = ScriptedModel([FinalResponse("done")])

    assert isinstance(ContextBuilder(), ContextPort)
    assert isinstance(provider, ModelPort)
    assert isinstance(registry, ToolPort)
    assert isinstance(ToolExecutor([]), ToolExecutorPort)
    assert isinstance(ObservabilityFixture(), ObservabilityPort)
