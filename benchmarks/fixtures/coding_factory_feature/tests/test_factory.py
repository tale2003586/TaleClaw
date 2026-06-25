from src.factory import make_user


def test_make_user():
    assert make_user("Ada") == {"name": "Ada"}
