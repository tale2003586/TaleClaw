from __future__ import annotations

from html.parser import HTMLParser
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
INDEX = ROOT / "web" / "static" / "index.html"
APP = ROOT / "web" / "static" / "app.js"


class ConsoleMarkupParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.ids: list[str] = []
        self.views: set[str] = set()
        self.nav_items: list[dict[str, str]] = []
        self.preview_notice = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = {key: value or "" for key, value in attrs}
        if values.get("id"):
            self.ids.append(values["id"])
        if values.get("data-main-view-panel"):
            self.views.add(values["data-main-view-panel"])
        if values.get("data-main-view"):
            self.nav_items.append(values)
        if values.get("data-settings-preview") == "notice":
            self.preview_notice = True


def _markup() -> ConsoleMarkupParser:
    parser = ConsoleMarkupParser()
    parser.feed(INDEX.read_text(encoding="utf-8"))
    return parser


def test_console_has_unique_ids_and_all_required_views() -> None:
    markup = _markup()
    assert len(markup.ids) == len(set(markup.ids))
    assert {
        "chat",
        "logs",
        "runs",
        "settings",
        "files",
        "analysis",
        "memory",
        "status",
    }.issubset(markup.views)


def test_navigation_targets_existing_views_and_protects_observability() -> None:
    markup = _markup()
    targets = {item["data-main-view"] for item in markup.nav_items}
    assert targets <= markup.views
    protected = {
        item["data-main-view"]
        for item in markup.nav_items
        if item.get("data-required-role") == "admin"
    }
    assert {"logs", "runs"} <= protected


def test_settings_is_explicitly_a_non_persistent_preview() -> None:
    markup = _markup()
    source = INDEX.read_text(encoding="utf-8")
    app_source = APP.read_text(encoding="utf-8")
    assert markup.preview_notice
    assert "尚未接入配置保存" in source
    assert "/api/settings" not in app_source
    assert "/api/config" not in app_source
    assert 'els.settingsForm?.addEventListener("submit"' in app_source


def test_frontend_uses_existing_run_apis_and_guarded_view_switching() -> None:
    source = APP.read_text(encoding="utf-8")
    assert 'fetchJson("/api/runs")' in source
    assert "/api/run?run_id=" in source
    assert "function isViewAllowed" in source
    assert "function applyRoleVisibility" in source
    assert "function traceEventToLogRow" in source
