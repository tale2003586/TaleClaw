from types import SimpleNamespace
import unittest

from models.provider import ToolCall
from runtime.execution.failure_reasons import StopReason
from runtime.execution.recovery import RecoveryAction, RecoveryController, RecoveryJudge
from runtime.execution.state import RunExecutionState
from tools.schema import function_tool
from tools.spec import ToolSpec


class JudgeProvider:
    def __init__(self, content=None, error=None):
        self.content = content
        self.error = error
        self.calls = []

    def chat(self, **kwargs):
        self.calls.append(kwargs)
        if self.error:
            raise self.error
        return SimpleNamespace(content=self.content)


def spec(*, idempotent=True, side_effect=False):
    return ToolSpec(
        schema=function_tool("read", "read", {}, []),
        handler=lambda **_: "ok",
        idempotent=idempotent,
        side_effect=side_effect,
    )


def duplicate():
    return [ToolCall(id="call-1", name="read", arguments={"path": "a.txt"})]


class RecoveryControllerTests(unittest.TestCase):
    def test_side_effect_or_non_idempotent_call_stops_without_judge(self):
        for tool_spec in (spec(side_effect=True), spec(idempotent=False)):
            provider = JudgeProvider('{"action":"correct_once","instruction":"change range"}')
            decision = RecoveryController().duplicate_tool_call(
                calls=duplicate(), specs=[tool_spec], state=RunExecutionState(),
                provider=provider, model="judge",
            )
            self.assertEqual(RecoveryAction.STOP, decision.action)
            self.assertEqual(StopReason.REPEATED_SIDE_EFFECT_RISK, decision.reason)
            self.assertEqual([], provider.calls)

    def test_read_only_call_allows_one_no_tools_judge(self):
        provider = JudgeProvider('{"action":"correct_once","instruction":"read the next range"}')
        decision = RecoveryController().duplicate_tool_call(
            calls=duplicate(), specs=[spec()], state=RunExecutionState(),
            provider=provider, model="judge",
        )
        self.assertEqual(RecoveryAction.CORRECT_ONCE, decision.action)
        self.assertEqual([], provider.calls[0]["tools"])
        self.assertEqual("none", provider.calls[0]["tool_choice"])

    def test_same_incident_gets_at_most_one_correction(self):
        provider = JudgeProvider('{"action":"correct_once","instruction":"change range"}')
        state = RunExecutionState()
        controller = RecoveryController()
        first = controller.duplicate_tool_call(
            calls=duplicate(), specs=[spec()], state=state, provider=provider, model="judge"
        )
        second = controller.duplicate_tool_call(
            calls=duplicate(), specs=[spec()], state=state, provider=provider, model="judge"
        )
        self.assertEqual(RecoveryAction.CORRECT_ONCE, first.action)
        self.assertEqual(RecoveryAction.STOP, second.action)
        self.assertEqual(StopReason.RECOVERY_EXHAUSTED, second.reason)
        self.assertEqual(1, len(provider.calls))

    def test_judge_exception_fails_closed(self):
        provider = JudgeProvider(error=RuntimeError("model unavailable"))
        decision = RecoveryController(RecoveryJudge()).duplicate_tool_call(
            calls=duplicate(), specs=[spec()], state=RunExecutionState(),
            provider=provider, model="judge",
        )
        self.assertEqual(RecoveryAction.STOP, decision.action)
        self.assertEqual(StopReason.RECOVERY_REJECTED, decision.reason)


if __name__ == "__main__":
    unittest.main()
