import logging
import tempfile
import unittest
from pathlib import Path

from runtime.logging import setup_logging


class RuntimeLoggingTests(unittest.TestCase):
    def test_setup_logging_adds_stdout_and_file_handlers(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            logger = setup_logging(log_file=Path(tmp) / "runtime.log")

            self.assertEqual(logging.DEBUG, logger.level)
            self.assertEqual(2, len(logger.handlers))
            self.assertEqual(logging.INFO, logger.handlers[0].level)
            self.assertEqual(logging.DEBUG, logger.handlers[1].level)


if __name__ == "__main__":
    unittest.main()
