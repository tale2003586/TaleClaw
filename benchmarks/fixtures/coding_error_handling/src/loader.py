from pathlib import Path


def load_lines(path):
    return Path(path).read_text().splitlines()
