from src.coerce import parse_bool


def test_parse_bool_handles_string_false():
    assert parse_bool("false") is False


def test_parse_bool_handles_true_values():
    assert parse_bool("true") is True
    assert parse_bool(True) is True
