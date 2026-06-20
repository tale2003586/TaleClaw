DEFAULT_TIMEOUT = 30
RETRY_COUNT = 3


def build_config():
    return {
        "timeout": DEFAULT_TIMEOUT,
        "retries": RETRY_COUNT,
    }
