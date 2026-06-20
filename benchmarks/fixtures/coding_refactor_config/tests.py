from app.main import describe


def test_describe_config():
    assert describe() == "timeout=30 retries=3"
