from src.report import revenue_by_region


def test_revenue_by_region_sums_repeated_regions(tmp_path):
    csv_path = tmp_path / "sales.csv"
    csv_path.write_text(
        "region,amount\nnorth,10\nsouth,5\nnorth,7\n",
        encoding="utf-8",
    )

    assert revenue_by_region(csv_path) == {"north": 17, "south": 5}
