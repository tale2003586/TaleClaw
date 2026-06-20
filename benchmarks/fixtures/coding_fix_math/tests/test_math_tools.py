from src.math_tools import clamp


def test_clamp_below_range():
    assert clamp(-1, 0, 10) == 0


def test_clamp_above_range():
    assert clamp(11, 0, 10) == 10


def test_clamp_inside_range():
    assert clamp(5, 0, 10) == 5
