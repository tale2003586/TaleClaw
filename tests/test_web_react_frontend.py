from __future__ import annotations

import re
from pathlib import Path

from web.server import RequestHandler, STATIC_DIR


ROOT = Path(__file__).resolve().parents[1]
BUILD_DIR = ROOT / "web" / "static" / "app"


def _handler() -> RequestHandler:
    return object.__new__(RequestHandler)


def test_react_build_contains_hashed_javascript_and_css() -> None:
    index = BUILD_DIR / "index.html"
    assert index.is_file()
    source = index.read_text(encoding="utf-8")
    assets = re.findall(r'(?:src|href)="([^"]+)"', source)
    assert any(re.fullmatch(r"/static/app/assets/index-[\w-]+\.js", item) for item in assets)
    assert any(re.fullmatch(r"/static/app/assets/index-[\w-]+\.css", item) for item in assets)
    for asset in assets:
        if asset.startswith("/static/app/"):
            assert (STATIC_DIR / asset.removeprefix("/static/")).is_file()


def test_python_root_and_spa_fallback_target_react_index() -> None:
    expected = BUILD_DIR / "index.html"
    handler = _handler()
    assert handler._static_target("/") == expected
    assert handler._static_target("/some-react-route") == expected


def test_login_assets_remain_native_and_react_index_does_not_use_legacy_console() -> None:
    assert (STATIC_DIR / "login.html").is_file()
    assert (STATIC_DIR / "login.js").is_file()
    assert (STATIC_DIR / "auth.css").is_file()
    source = (BUILD_DIR / "index.html").read_text(encoding="utf-8")
    assert "/static/app.js" not in source
    assert "/static/styles.css" not in source


def test_react_index_preloads_theme_before_mount() -> None:
    source = (BUILD_DIR / "index.html").read_text(encoding="utf-8")
    assert "taleclawTheme" in source
    assert "prefers-color-scheme: dark" in source
    assert ".dataset.theme" in source
    assert 'meta[name="theme-color"]' in source
