import csv
from pathlib import Path


def revenue_by_region(path):
    rows = csv.DictReader(Path(path).read_text().splitlines())
    totals = {}
    for row in rows:
        totals[row["region"]] = int(row["amount"])
    return totals
