import json
import unittest

from agents.subagent.failure import classify_subagent_failure
from agents.subagent.prompting import extract_structured_result
from runtime.execution.failure_reasons import SubagentFailureReason
from runtime.units.json_repair import repair_json_object


class SubagentOutputProtocolTests(unittest.TestCase):
    def test_json_repair_extracts_fenced_object_without_inventing_fields(self) -> None:
        result = repair_json_object('prefix\n```json\n{"ok": true}\n```\nsuffix')

        self.assertTrue(result.ok)
        self.assertTrue(result.repaired)
        self.assertEqual({"ok": True}, result.payload)

    def test_json_repair_handles_common_json_syntax_errors(self) -> None:
        result = repair_json_object("{'ok': True, 'items': [1, 2,],}")

        self.assertTrue(result.ok)
        self.assertTrue(result.repaired)
        self.assertEqual({"ok": True, "items": [1, 2]}, result.payload)

    def test_extract_structured_result_preserves_legacy_explore_findings(self) -> None:
        payload = {
            "findings": [{"path": "runtime/runtime.py", "lines": "1-20"}],
            "evidence": [{"path": "runtime/runtime.py", "lines": "1-20"}],
            "covered_scope": ["runtime/runtime.py"],
            "incomplete": False,
        }

        structured = extract_structured_result(json.dumps(payload), agent_type="explore")

        self.assertTrue(structured["format_valid"])
        self.assertEqual("subagent.explore.v1", structured["output_schema"])
        self.assertEqual(payload["findings"], structured["findings"])
        self.assertEqual(payload["findings"], structured["payload"]["findings"])

    def test_invalid_text_becomes_recoverable_output_format_failure(self) -> None:
        raw_text = "not json\nbut keep this full text"
        structured = extract_structured_result(raw_text, agent_type="explore")

        failure = classify_subagent_failure(
            session_messages=[],
            stop_reason=None,
            structured=structured,
        )

        self.assertFalse(structured["format_valid"])
        self.assertEqual(raw_text, structured["summary"])
        self.assertEqual(raw_text, structured["raw_text"])
        self.assertIsNotNone(failure)
        self.assertEqual(SubagentFailureReason.INVALID_OUTPUT_FORMAT.value, failure.reason)
        self.assertTrue(failure.recoverable)

    def test_plan_and_code_payloads_do_not_require_findings(self) -> None:
        plan = extract_structured_result(
            json.dumps({
                "schema_version": "subagent.plan.v1",
                "payload": {"plan": [{"step": "Inspect runner"}]},
            }),
            agent_type="plan",
        )
        code = extract_structured_result(
            json.dumps({
                "schema_version": "subagent.code.v1",
                "payload": {
                    "changes": [{"path": "agents/subagent/result.py"}],
                    "files_touched": ["agents/subagent/result.py"],
                    "risk": "low",
                },
            }),
            agent_type="code",
        )

        self.assertEqual([], plan["findings"])
        self.assertEqual("Inspect runner", plan["payload"]["plan"][0]["step"])
        self.assertEqual([], code["findings"])
        self.assertEqual(["agents/subagent/result.py"], code["payload"]["files_touched"])

    def test_incomplete_plan_payload_is_not_empty_findings_failure(self) -> None:
        structured = extract_structured_result(
            json.dumps({
                "schema_version": "subagent.plan.v1",
                "payload": {"plan": [{"step": "Inspect runner"}]},
                "incomplete": True,
            }),
            agent_type="plan",
        )

        failure = classify_subagent_failure(
            session_messages=[],
            stop_reason=None,
            structured=structured,
        )

        self.assertIsNone(failure)


if __name__ == "__main__":
    unittest.main()
