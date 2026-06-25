import os


def get_port():
    return int(os.environ["APP_PORT"])
