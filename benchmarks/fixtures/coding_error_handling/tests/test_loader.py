from src.loader import load_lines


def test_load_lines_reads_existing_file(tmp_path):
    source = tmp_path / "items.txt"
    source.write_text("alpha\nbeta\n", encoding="utf-8")
    assert load_lines(source) == ["alpha", "beta"]


def test_load_lines_returns_empty_for_missing_file(tmp_path):
    assert load_lines(tmp_path / "missing.txt") == []
