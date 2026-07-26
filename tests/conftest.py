import pytest


@pytest.fixture(autouse=True)
def _disable_shared_trace_index(monkeypatch):
    """Keep ordinary tests from writing trace indexes to a configured shared DB."""
    monkeypatch.setenv("TRACE_INDEX_ENABLED", "0")
