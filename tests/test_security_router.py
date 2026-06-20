import unittest

from memory.embeddings import HashEmbeddingProvider
from retrieval.security_router import (
    LlmSecurityRouteClassifier,
    SecurityRetrievalRouter,
    SecurityRouteConfig,
)


class FakeRunner:
    def __init__(self, response: str, *, fail: bool = False) -> None:
        self.response = response
        self.fail = fail
        self.calls = []

    def run(self, **kwargs):
        self.calls.append(kwargs)
        if self.fail:
            raise RuntimeError("boom")
        return self.response


class FakeSpec:
    max_tokens = 350


class SecurityRouterTests(unittest.TestCase):
    def test_rewrite_uses_only_first_keyword_expansion(self) -> None:
        router = SecurityRetrievalRouter(embeddings=HashEmbeddingProvider())

        rewritten = router.rewrite_query(
            "token xss csrf jwt",
            keyword_matches=["token", "jwt", "xss", "csrf"],
        )

        self.assertEqual("token xss csrf jwt storage leakage authentication security", rewritten)
        self.assertNotIn("HttpOnly", rewritten)
        self.assertNotIn("content policy", rewritten)

    def test_rewrite_appends_intent_only_when_high_confidence(self) -> None:
        router = SecurityRetrievalRouter(embeddings=HashEmbeddingProvider())

        low = router.rewrite_query("这个接口安全吗", matched_intent="authorization bypass", intent_score=0.72)
        high = router.rewrite_query("这个接口安全吗", matched_intent="authorization bypass", intent_score=0.85)

        self.assertEqual("这个接口安全吗", low)
        self.assertEqual("这个接口安全吗 authorization bypass", high)

    def test_middle_band_uses_llm_classifier(self) -> None:
        router = SecurityRetrievalRouter(
            embeddings=HashEmbeddingProvider(),
            config=SecurityRouteConfig(high_threshold=2.0, low_threshold=-1.0),
        )
        classifier = LlmSecurityRouteClassifier(
            runner=FakeRunner(
                '{"needs_retrieval": true, "confidence": 0.91, '
                '"reason": "security question", "query": "authorization bypass"}'
            ),
            spec=FakeSpec(),
            accept_threshold=0.60,
            default_top_k=7,
            min_score=0.1,
        )

        decision = router.route("这个接口这样设计安全吗", llm_classifier=classifier)

        self.assertTrue(decision.use_rag)
        self.assertEqual("llm", decision.route)
        self.assertEqual("authorization bypass", decision.query)
        self.assertEqual(7, decision.top_k)
        self.assertEqual(0.1, decision.min_score)

    def test_llm_classifier_failure_is_non_fatal(self) -> None:
        classifier = LlmSecurityRouteClassifier(
            runner=FakeRunner("", fail=True),
            spec=FakeSpec(),
            accept_threshold=0.60,
            default_top_k=5,
            min_score=0.0,
        )

        decision = classifier(
            "这个接口安全吗",
            embedding_score=0.52,
            matched_intent="authorization bypass",
        )

        self.assertFalse(decision.use_rag)
        self.assertEqual("llm_error", decision.route)
        self.assertIn("failed", decision.reason)


if __name__ == "__main__":
    unittest.main()
