import argparse


def greet(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--name", default="world")
    args = parser.parse_args(argv)
    return "hello world"


if __name__ == "__main__":
    print(greet())
