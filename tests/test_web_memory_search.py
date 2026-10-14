import re

import pytest

from tests.test_web_memory_view import ReadOnlyCatalog, Result, install_catalog, item
from web import memory_view


class SearchCatalog(ReadOnlyCatalog):
    def execute(self, query, params=()):
        if "FROM memory_items" not in query or "GROUP BY status" in query:
            return super().execute(query, params)
        self.calls.append((query, params))
        pairs = query.count("(owner_scope = %s AND owner_id = %s)")
        owners = set(zip(params[:pairs * 2:2], params[1:pairs * 2:2]))
        remaining = list(params[pairs * 2:])
        rows = [row for row in self.items if (row["owner_scope"], row["owner_id"]) in owners]
        if "content ILIKE" in query:
            assert query.count("ESCAPE E'\\\\'") == 3
            literal = re.sub(r"\\(.)", r"\1", remaining[0][1:-1]).lower()
            assert remaining[:3] == [remaining[0]] * 3
            remaining = remaining[3:]
            rows = [row for row in rows if any(literal in str(row[field]).lower() for field in ("content", "id", "owner_id"))]
        for field in ("status", "kind"):
            if f"AND {field} = %s" in query:
                value = remaining.pop(0)
                rows = [row for row in rows if row[field] == value]
        if "COUNT(*) AS count" in query:
            assert not remaining
            return Result([{"count": len(rows)}])
        limit, offset = remaining
        return Result(sorted(rows, key=lambda row: row["id"])[offset:offset + limit])


def test_filters_apply_before_pagination_and_counts_remain_scope_wide(monkeypatch):
    catalog = install_catalog(monkeypatch, SearchCatalog([
        item("a", content="not relevant"), item("b", content="Target one", status="candidate", kind="decision"),
        item("c", content="Target two", status="candidate", kind="decision"),
        item("d", content="Target wrong state", kind="decision"),
        item("foreign", content="Target SECRET", status="candidate", kind="decision", owner_id="bob"),
    ]))
    result = memory_view.read_memory_overview("alice", query=" Target ", status="candidate", kind="decision", limit=1)
    assert result["available"] and result["filtered_total"] == 2
    assert result["counts"]["total"] == 4 and result["counts"]["by_status"]["candidate"] == 2
    assert result["filters"] == {"query": "Target", "status": "candidate", "kind": "decision"}
    assert result["items"][0]["id"] == "b" and result["has_more"]
    assert result["items"][0]["manageable"]
    second = memory_view.read_memory_overview("alice", query="Target", status="candidate", kind="decision", limit=1, offset=1)
    assert second["items"][0]["id"] == "c" and not second["has_more"]
    assert all("SECRET" not in str(record) for record in result["items"])
    assert all(query.lstrip().startswith(("SELECT", "SET TRANSACTION")) for query, _ in catalog.calls)


def test_search_keeps_percent_underscore_backslash_literal_and_matches_owner_and_id(monkeypatch):
    install_catalog(monkeypatch, SearchCatalog([item("literal", content="Literal %_\\ fragment"), item("wildcard", content="Literal xanything fragment")]))
    result = memory_view.read_memory_overview("alice", query="%_\\")
    assert result["available"] and [value["id"] for value in result["items"]] == ["literal"]
    assert memory_view.read_memory_overview("alice", query="alice")["filtered_total"] == 2
    assert memory_view.read_memory_overview("alice", query="LITERAL")["filtered_total"] == 2  # content and ID


@pytest.mark.parametrize("filters", [{"status": "all"}, {"kind": "unknown"}, {"status": " active "},
                                    {"query": "x" * 501}, {"query": []}, {"kind": None}])
def test_invalid_filters_fail_before_database_access(monkeypatch, filters):
    calls = []
    monkeypatch.setattr(memory_view, "connect", lambda config: calls.append(config))
    with pytest.raises(ValueError):
        memory_view.read_memory_overview("alice", **filters)
    assert calls == []
