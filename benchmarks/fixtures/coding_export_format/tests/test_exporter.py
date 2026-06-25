from src.exporter import to_lines


def test_to_lines_exports_csv_with_header():
    records = [{"id": 1, "name": "Ada"}, {"id": 2, "name": "Lin"}]
    assert to_lines(records) == ["id,name", "1,Ada", "2,Lin"]
