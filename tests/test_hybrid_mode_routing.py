import unittest

from models.provider import LLMResponse
from agents.definitions import BOT_AGENT_SPEC
from agents.definitions import CODING_AGENT_SPEC
from runtime.routing.hybrid_classifier import HybridModeClassifier
from runtime.routing.agent_router import AgentRouter
from runtime.sessions.session import Session


class RecordingClassifier:
    def __init__(self, result: bool) -> None:
        self.result = result
        self.calls = []

    def should_use_coding(self, user_text: str) -> bool:
        self.calls.append(user_text)
        return self.result


class AgentRouterHybridClassificationTests(unittest.TestCase):
    def test_keyword_candidate_uses_classifier_before_coding_route(self) -> None:
        classifier = RecordingClassifier(False)
        router = AgentRouter(hybrid_classifier=classifier)

        route = router.route(
            Session(id="web:default"),
            "我想写一篇关于测试焦虑的文章",
        )

        self.assertIs(BOT_AGENT_SPEC, route.agent_spec)
        self.assertEqual(["我想写一篇关于测试焦虑的文章"], classifier.calls)

    def test_classifier_can_accept_real_coding_request(self) -> None:
        classifier = RecordingClassifier(True)
        router = AgentRouter(hybrid_classifier=classifier)
        session = Session(id="web:default")

        route = router.route(
            session,
            "请修改 Python 文件并运行测试",
        )

        self.assertIs(CODING_AGENT_SPEC, route.agent_spec)
        self.assertEqual("coding", route.intent)
        self.assertEqual("coding_application", route.execution)
        self.assertEqual("coding", session.metadata["last_route"]["intent"])
        self.assertEqual(["请修改 Python 文件并运行测试"], classifier.calls)

    def test_storage_request_stays_in_bot_and_skips_classifier(self) -> None:
        classifier = RecordingClassifier(True)

        route = AgentRouter(hybrid_classifier=classifier).route(
            Session(id="web:default"),
            "帮我下载 storage 里的报告文件",
        )

        self.assertIs(BOT_AGENT_SPEC, route.agent_spec)
        self.assertEqual("storage_file", route.intent)
        self.assertEqual([], classifier.calls)

    def test_revoked_admin_session_leaves_coding_mode(self) -> None:
        session = Session(
            id="web:guest",
            active_agent="coding",
            metadata={"user_role": "user"},
        )

        route = AgentRouter().route(session, "继续修改代码")

        self.assertIs(BOT_AGENT_SPEC, route.agent_spec)
        self.assertEqual("bot", session.active_agent)
        self.assertEqual("chat", route.intent)

    def test_code_file_with_storage_name_still_uses_coding_candidate(self) -> None:
        classifier = RecordingClassifier(True)

        route = AgentRouter(hybrid_classifier=classifier).route(
            Session(id="web:default"),
            "请修改 gateway/telegram/storage.py 并运行测试",
        )

        self.assertIs(CODING_AGENT_SPEC, route.agent_spec)
        self.assertEqual("coding", route.intent)
        self.assertEqual(["请修改 gateway/telegram/storage.py 并运行测试"], classifier.calls)

    def test_non_candidate_skips_classifier(self) -> None:
        classifier = RecordingClassifier(True)
        router = AgentRouter(hybrid_classifier=classifier)

        route = router.route(Session(id="web:default"), "帮我润色这段文字")

        self.assertIs(BOT_AGENT_SPEC, route.agent_spec)
        self.assertEqual([], classifier.calls)

    def test_missing_classifier_falls_back_to_bot(self) -> None:
        route = AgentRouter().route(
            Session(id="web:default"),
            "请修改 Python 文件并运行测试",
        )

        self.assertIs(BOT_AGENT_SPEC, route.agent_spec)

    def test_explicit_coding_switch_skips_classifier(self) -> None:
        classifier = RecordingClassifier(False)
        session = Session(id="web:default")

        route = AgentRouter(hybrid_classifier=classifier).route(session, "/coding")

        self.assertTrue(route.switched)
        self.assertEqual("coding", session.active_agent)
        self.assertEqual([], classifier.calls)


class HybridModeClassifierTests(unittest.TestCase):
    def test_classifier_parses_json_response(self) -> None:
        class Provider:
            def __init__(self) -> None:
                self.calls = []

            def chat(self, **kwargs):
                self.calls.append(kwargs)
                return LLMResponse(
                    content='{"mode":"coding","reason":"Repository edit requested."}',
                )

        provider = Provider()
        classifier = HybridModeClassifier(provider=provider, model="route-model")

        self.assertTrue(classifier.should_use_coding("修复 Python 文件中的 bug"))
        self.assertEqual("route-model", provider.calls[0]["model"])
        self.assertEqual([], provider.calls[0]["tools"])
        self.assertEqual("none", provider.calls[0]["tool_choice"])

    def test_classifier_failure_falls_back_to_bot(self) -> None:
        class Provider:
            def chat(self, **kwargs):
                raise RuntimeError("offline")

        classifier = HybridModeClassifier(provider=Provider(), model="route-model")

        self.assertFalse(classifier.should_use_coding("修复 Python 文件中的 bug"))


if __name__ == "__main__":
    unittest.main()
