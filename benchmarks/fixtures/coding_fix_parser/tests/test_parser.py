import pytest

from src.parser import parse_port


def test_parse_port_number():
    assert parse_port("8080") == 8080


def test_parse_port_default():
    assert parse_port("") == 8000


def test_parse_port_rejects_invalid():
    with pytest.raises(ValueError):
        parse_port("abc")
