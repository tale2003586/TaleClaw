from app.config import build_config


def describe():
    config = build_config()
    return f"timeout={config['timeout']} retries={config['retries']}"
