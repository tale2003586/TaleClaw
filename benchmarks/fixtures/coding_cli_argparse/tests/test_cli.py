from app.cli import greet


def test_greet_uses_name_argument():
    assert greet(["--name", "Ada"]) == "hello Ada"


def test_greet_default():
    assert greet([]) == "hello world"
