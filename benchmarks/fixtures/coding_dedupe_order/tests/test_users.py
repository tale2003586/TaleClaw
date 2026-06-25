from src.users import unique_names


def test_unique_names_preserves_first_seen_order():
    assert unique_names(["ada", "lin", "ada", "grace", "lin"]) == [
        "ada",
        "lin",
        "grace",
    ]
