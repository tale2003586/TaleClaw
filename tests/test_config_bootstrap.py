import importlib
import os
import sys
import unittest
from unittest.mock import patch


class ConfigBootstrapTests(unittest.TestCase):
    def test_importing_config_does_not_mutate_proxy_env_or_build_model_pool(self) -> None:
        sys.modules.pop("config", None)
        with patch.dict(
            os.environ,
            {
                "USE_LOCAL_PROXY": "1",
                "LOCAL_PROXY_URL": "http://127.0.0.1:7897",
                "HTTP_PROXY": "http://existing-http",
                "HTTPS_PROXY": "http://existing-https",
            },
            clear=False,
        ):
            config = importlib.import_module("config")

            self.assertEqual("http://existing-http", os.environ["HTTP_PROXY"])
            self.assertEqual("http://existing-https", os.environ["HTTPS_PROXY"])
            self.assertFalse(hasattr(config, "MODEL_POOL"))

    def test_runtime_proxy_disabled_preserves_existing_proxy_env(self) -> None:
        from runtime.bootstrap import _configure_proxy_from_env

        with patch.dict(
            os.environ,
            {
                "USE_LOCAL_PROXY": "0",
                "HTTP_PROXY": "http://existing-http",
                "HTTPS_PROXY": "http://existing-https",
                "ALL_PROXY": "socks5://existing-all",
            },
            clear=True,
        ):
            _configure_proxy_from_env()

            self.assertEqual("http://existing-http", os.environ["HTTP_PROXY"])
            self.assertEqual("http://existing-https", os.environ["HTTPS_PROXY"])
            self.assertEqual("socks5://existing-all", os.environ["ALL_PROXY"])

    def test_runtime_proxy_enabled_sets_http_https_and_all_proxy(self) -> None:
        from runtime.bootstrap import _configure_proxy_from_env

        with patch.dict(
            os.environ,
            {
                "USE_LOCAL_PROXY": "1",
                "LOCAL_PROXY_URL": "http://127.0.0.1:7897",
            },
            clear=True,
        ):
            _configure_proxy_from_env()

            self.assertEqual("http://127.0.0.1:7897", os.environ["HTTP_PROXY"])
            self.assertEqual("http://127.0.0.1:7897", os.environ["HTTPS_PROXY"])
            self.assertEqual("http://127.0.0.1:7897", os.environ["ALL_PROXY"])


if __name__ == "__main__":
    unittest.main()
