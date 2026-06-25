from src.settings import get_port


def test_get_port_uses_default(monkeypatch):
    monkeypatch.delenv("APP_PORT", raising=False)
    assert get_port() == 8080


def test_get_port_reads_env(monkeypatch):
    monkeypatch.setenv("APP_PORT", "9001")
    assert get_port() == 9001
