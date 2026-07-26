import pytest

from applications.minecraft.parser import GoalParseError, parse_resource_goal


@pytest.mark.parametrize(
    ("text", "resource", "quantity"),
    [
        ("收集 4 个原木", "oak_log", 4),
        ("/minecraft 挖30个钻石", "diamond", 30),
        ("获取 16 个铁原矿", "raw_iron", 16),
    ],
)
def test_parse_resource_goal(text, resource, quantity):
    goal = parse_resource_goal(text)
    assert (goal.resource, goal.quantity) == (resource, quantity)


@pytest.mark.parametrize(
    ("text", "code"),
    [
        ("收集 0 个原木", "invalid_quantity"),
        ("收集 -2 个原木", "invalid_quantity"),
        ("收集一些原木", "missing_quantity"),
        ("建造一座房子", "missing_quantity"),
        ("收集 3 个不存在的矿", "unknown_resource"),
        ("收集 4 个原木和 2 个钻石", "multiple_goals"),
    ],
)
def test_parse_resource_goal_rejections(text, code):
    with pytest.raises(GoalParseError) as exc:
        parse_resource_goal(text)
    assert exc.value.code == code
