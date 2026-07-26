from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def test_minecraft_is_opt_in_and_documented():
    env = (ROOT / ".env.example").read_text(encoding="utf-8")
    compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    docs = (ROOT / "docs/minecraft-agent.md").read_text(encoding="utf-8")
    assert "MINECRAFT_AGENT_ENABLED=0" in env
    assert "profiles:\n      - minecraft" in compose
    assert "offline" in docs
    assert "--connect" in docs
    assert "MINECRAFT_BRIDGE_TOKEN=" in env
    assert "replace-with-a-long-random-token" in docs
