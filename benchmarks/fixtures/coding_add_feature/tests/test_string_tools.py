from src.string_tools import slugify


def test_slugify_existing_behavior():
    assert slugify("Hello World") == "hello-world"
