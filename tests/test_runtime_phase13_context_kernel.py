from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

from runtime.context import ContextBuilder
from runtime.context.providers import (
    DEFAULT_CONTEXT_PROVIDERS,
    EmptyMemoryContextProvider,
    EmptyRetrievalContextProvider,
    MINIMAL_CONTEXT_PROVIDERS,
    MemoryContextProvider,
    NoCodingContextProvider,
    RetrievalContextProvider,
)
from runtime.sessions import Session


ROOT = Path(__file__).resolve().parents[1]
OPTIONAL_IMPORT_PREFIXES = (
    "applications",
    "knowledge",
    "memory",
    "retrieval",
    "runtime.working_memory",
    "runtime.trace.rag",
    "skill_runtime",
)


def test_context_builder_has_no_top_level_optional_capability_imports():
    tree = ast.parse((ROOT / "runtime/context/builder.py").read_text(encoding="utf-8"))
    imported = []
    for node in tree.body:
        if isinstance(node, ast.Import):
            imported.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.append(node.module or "")

    assert not [
        name
        for name in imported
        if name.startswith(OPTIONAL_IMPORT_PREFIXES)
    ]


def test_importing_context_does_not_load_optional_capability_packages():
    script = """
import json
import sys
import runtime.context
prefixes = (
    "applications", "knowledge", "memory", "retrieval",
    "runtime.working_memory", "runtime.trace.rag", "skill_runtime",
)
print(json.dumps(sorted(
    name for name in sys.modules if name.startswith(prefixes)
)))
"""
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    assert result.stdout.strip() == "[]"


def test_minimal_context_is_the_zero_capability_default():
    builder = ContextBuilder()
    session = Session(id="phase13:minimal")
    session.add_message("user", "hello")

    context = builder.build(
        session=session,
        agent_spec=SimpleNamespace(instructions="system", tool_mode="bot"),
    )

    assert tuple(builder.context_providers) == (
        "prompt",
        "history",
        "memory",
        "retrieval",
        "coding",
    )
    assert isinstance(builder.context_providers["memory"], EmptyMemoryContextProvider)
    assert isinstance(
        builder.context_providers["retrieval"],
        EmptyRetrievalContextProvider,
    )
    assert isinstance(builder.context_providers["coding"], NoCodingContextProvider)
    assert context.messages[-1]["role"] == "user"
    assert context.messages[-1]["content"] == "hello"


def test_full_product_context_remains_an_explicit_composition():
    minimal = {provider.name: provider for provider in MINIMAL_CONTEXT_PROVIDERS}
    full = {provider.name: provider for provider in DEFAULT_CONTEXT_PROVIDERS}

    assert isinstance(minimal["memory"], EmptyMemoryContextProvider)
    assert isinstance(minimal["retrieval"], EmptyRetrievalContextProvider)
    assert isinstance(full["memory"], MemoryContextProvider)
    assert isinstance(full["retrieval"], RetrievalContextProvider)
