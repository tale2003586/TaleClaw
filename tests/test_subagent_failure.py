import unittest

from agents.subagent.failure import classify_subagent_failure
from runtime.failure_reasons import SubagentFailureReason


class SubagentFailureClassificationTests(unittest.TestCase):
    def test_successful_read_content_with_valueerror_is_not_tool_error(self) -> None:
        failure = classify_subagent_failure(
            session_messages=[
                {
                    "role": "tool",
                    "status": "success",
                    "content": "except ValueError as exc:\n    raise exc",
                }
            ],
            stop_reason=None,
            structured={},
        )

        self.assertIsNone(failure)

    def test_successful_missing_file_error_is_still_classified(self) -> None:
        failure = classify_subagent_failure(
            session_messages=[
                {
                    "role": "tool",
                    "status": "success",
                    "content": "Error: FileNotFoundError: File not found: missing.py",
                }
            ],
            stop_reason=None,
            structured={},
        )

        self.assertIsNotNone(failure)
        self.assertEqual(
            SubagentFailureReason.MISSING_REQUIRED_FILES.value,
            failure.reason,
        )

    def test_status_error_uses_structured_error_message(self) -> None:
        failure = classify_subagent_failure(
            session_messages=[
                {
                    "role": "tool",
                    "status": "error",
                    "content": "Tool error: noisy fallback",
                    "error_message": "clean structured error",
                }
            ],
            stop_reason=None,
            structured={},
        )

        self.assertIsNotNone(failure)
        self.assertEqual(SubagentFailureReason.TOOL_ERROR.value, failure.reason)
        self.assertEqual("clean structured error", failure.message)


if __name__ == "__main__":
    unittest.main()
