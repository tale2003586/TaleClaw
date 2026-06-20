import unittest
from types import SimpleNamespace

from runtime.reflection import ReflectionAgent


class ReflectionRateLimitTests(unittest.TestCase):
    def test_periodic_reflection_after_min_steps(self) -> None:
        agent = ReflectionAgent(
            provider=object(),
            model="reflection-model",
            min_reasoning_steps=10,
            reflection_interval=5,
        )
        execution = SimpleNamespace(
            loop_guard_denied=False,
            unavailable_tools=[],
            tool_results=[{"status": "success"}],
        )

        self.assertFalse(agent.should_reflect(
            session=None,
            profile=None,
            response=None,
            execution=execution,
            reasoning_steps=9,
        ))
        self.assertTrue(agent.should_reflect(
            session=None,
            profile=None,
            response=None,
            execution=execution,
            reasoning_steps=10,
        ))
        self.assertFalse(agent.should_reflect(
            session=None,
            profile=None,
            response=None,
            execution=execution,
            reasoning_steps=11,
        ))
        self.assertTrue(agent.should_reflect(
            session=None,
            profile=None,
            response=None,
            execution=execution,
            reasoning_steps=15,
        ))

    def test_failures_reflect_immediately(self) -> None:
        agent = ReflectionAgent(
            provider=object(),
            model="reflection-model",
            min_reasoning_steps=10,
            reflection_interval=5,
        )
        execution = SimpleNamespace(
            loop_guard_denied=False,
            unavailable_tools=[],
            tool_results=[{"status": "error"}],
        )

        self.assertTrue(agent.should_reflect(
            session=None,
            profile=None,
            response=None,
            execution=execution,
            reasoning_steps=1,
        ))


if __name__ == "__main__":
    unittest.main()
