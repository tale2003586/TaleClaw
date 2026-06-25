import unittest

from knowledge.security_rag import KnowledgeHit
from memory.embeddings import HashEmbeddingProvider
from retrieval.security_router import (
    LLMQueryRewriteProvider,
    LlmSecurityRouteClassifier,
    SecurityRetrievalRouter,
    SecurityRouteConfig,
    RewriteResult,
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


class FakeRewriteProvider:
    def rewrite(self, request):
        if request.mode == "decompose":
            return RewriteResult(
                query=request.query,
                queries=[request.query, "authorization SQL injection code review"],
                reason="fake decompose",
                provider="fake",
            )
        return RewriteResult(
            query="jwt token storage httponly samesite xss csrf",
            queries=["jwt token storage httponly samesite xss csrf"],
            reason="fake rewrite",
            provider="fake",
        )


class EmptyDecomposeRewriteProvider:
    def rewrite(self, request):
        if request.mode == "decompose":
            return RewriteResult(
                query="",
                queries=["", "   "],
                reason="empty decompose",
                provider="empty",
            )
        return FakeRewriteProvider().rewrite(request)


class FakeIndex:
    def __init__(self, hits_by_stage=None, *, hybrid_enabled: bool = False) -> None:
        self.calls = []
        self.hits_by_stage = list(hits_by_stage or [])
        self.hybrid_enabled = hybrid_enabled

    def search(self, query, **kwargs):
        self.calls.append({"query": query, **kwargs})
        if self.hits_by_stage:
            return self.hits_by_stage.pop(0)
        return []


def fake_hit(score: float, *, source: str = "CheatSheetSeries/Auth.md", hit_id: str | None = None) -> KnowledgeHit:
    return KnowledgeHit(
        id=hit_id or f"hit-{score}",
        text="authentication rate limiting brute force",
        score=score,
        source_path=f"/kb/{source}",
        source_relpath=source,
        title="Auth",
        chunk_index=0,
        metadata={},
    )


class SecurityRouterTests(unittest.TestCase):
    def test_negative_gate_runs_before_security_keyword(self) -> None:
        router = SecurityRetrievalRouter(embeddings=HashEmbeddingProvider())

        decision = router.route("这个漏洞在我们线上到底有没有被利用过？")

        self.assertFalse(decision.use_rag)
        self.assertEqual("insufficient_evidence", decision.route)

    def test_non_blocked_query_defaults_to_dense_rag(self) -> None:
        router = SecurityRetrievalRouter(embeddings=HashEmbeddingProvider())

        decision = router.route("登录接口已经有验证码，还需要限制密码错误次数吗？")

        self.assertTrue(decision.use_rag)
        self.assertIn(decision.route, {"fast_dense_low", "fast_dense_default", "embedding_high", "keyword"})

    def test_empty_keywords_really_disables_keyword_route(self) -> None:
        router = SecurityRetrievalRouter(embeddings=HashEmbeddingProvider(), keywords={})

        decision = router.route("JWT 放在 localStorage 里会有什么风险？")

        self.assertNotEqual("keyword", decision.route)
        self.assertEqual([], decision.keyword_matches)

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

    def test_route_with_retrieval_uses_fast_dense_direct_evidence(self) -> None:
        router = SecurityRetrievalRouter(embeddings=HashEmbeddingProvider())
        index = FakeIndex([[fake_hit(0.85), fake_hit(0.65)]])

        plan = router.route_with_retrieval(
            "登录接口已经有验证码，还需要限制密码错误次数吗？",
            index=index,
            top_k=2,
        )

        self.assertTrue(plan.decision.use_rag)
        self.assertEqual("direct", plan.action)
        self.assertEqual("dense", index.calls[0]["retrieval_mode"])
        self.assertFalse(index.calls[0]["use_reranker"])

    def test_route_with_retrieval_can_rewrite_before_fast_dense(self) -> None:
        router = SecurityRetrievalRouter(
            embeddings=HashEmbeddingProvider(),
            config=SecurityRouteConfig(pre_dense_rewrite_enabled=True),
        )
        index = FakeIndex([[fake_hit(0.85), fake_hit(0.65)]])

        plan = router.route_with_retrieval("JWT 放在 localStorage 里会有什么风险？", index=index)

        self.assertEqual("direct", plan.action)
        self.assertEqual("fast_dense", plan.searches[0].stage)
        self.assertIn("token storage", index.calls[0]["query"])
        self.assertIn("pre_dense_rule", plan.decision.route)

    def test_route_with_retrieval_can_use_configured_pre_dense_provider(self) -> None:
        router = SecurityRetrievalRouter(
            embeddings=HashEmbeddingProvider(),
            config=SecurityRouteConfig(
                pre_dense_rewrite_enabled=True,
                pre_dense_rewrite_provider="llm",
                pre_dense_parallel_enabled=False,
            ),
            rewrite_provider=FakeRewriteProvider(),
        )
        index = FakeIndex([[fake_hit(0.85), fake_hit(0.65)]])

        plan = router.route_with_retrieval("JWT 放在 localStorage 里会有什么风险？", index=index)

        self.assertEqual("jwt token storage httponly samesite xss csrf", index.calls[0]["query"])
        self.assertIn("pre_dense_fake", plan.decision.route)

    def test_parallel_pre_dense_runs_original_dense_before_rewritten_dense(self) -> None:
        router = SecurityRetrievalRouter(
            embeddings=HashEmbeddingProvider(),
            config=SecurityRouteConfig(
                pre_dense_rewrite_enabled=True,
                pre_dense_rewrite_provider="llm",
                pre_dense_parallel_enabled=True,
            ),
            rewrite_provider=FakeRewriteProvider(),
        )
        index = FakeIndex([[fake_hit(0.50)], [fake_hit(0.85), fake_hit(0.65)]])

        plan = router.route_with_retrieval("JWT 放在 localStorage 里会有什么风险？", index=index)

        self.assertEqual("JWT 放在 localStorage 里会有什么风险？", index.calls[0]["query"])
        self.assertEqual("jwt token storage httponly samesite xss csrf", index.calls[1]["query"])
        self.assertEqual("direct", plan.action)

    def test_topic_expansion_does_not_force_complex_route_when_dense_is_high(self) -> None:
        router = SecurityRetrievalRouter(embeddings=HashEmbeddingProvider())
        index = FakeIndex([[fake_hit(0.78), fake_hit(0.66)]])

        plan = router.route_with_retrieval("默认 admin 密码只在首次部署用，还需要改吗？", index=index)

        self.assertEqual("direct", plan.action)
        self.assertIn("dense_direct", plan.decision.route)
        self.assertEqual(1, len(index.calls))

    def test_high_top_score_still_needs_margin_before_direct(self) -> None:
        router = SecurityRetrievalRouter(embeddings=HashEmbeddingProvider())
        index = FakeIndex([[fake_hit(0.70), fake_hit(0.67)]])

        plan = router.route_with_retrieval("默认 admin 密码只在首次部署用，还需要改吗？", index=index)

        self.assertNotEqual("direct", plan.action)
        self.assertEqual("dense", plan.action)

    def test_dense_direct_can_use_hybrid_confirmation_overlap(self) -> None:
        router = SecurityRetrievalRouter(embeddings=HashEmbeddingProvider())
        index = FakeIndex(
            [
                [fake_hit(0.78, hit_id="auth-a"), fake_hit(0.66, hit_id="auth-b")],
                [fake_hit(0.74, hit_id="auth-a"), fake_hit(0.60, hit_id="auth-c")],
            ],
            hybrid_enabled=True,
        )

        plan = router.route_with_retrieval("登录接口已经有验证码，还需要限制密码错误次数吗？", index=index)

        self.assertEqual("direct", plan.action)
        self.assertEqual("hybrid_confirmation", plan.searches[1].stage)
        self.assertEqual("hybrid", index.calls[1]["retrieval_mode"])
        self.assertIn("dense_sparse_overlap", plan.reason)

    def test_route_with_retrieval_emits_search_trace_callback(self) -> None:
        router = SecurityRetrievalRouter(embeddings=HashEmbeddingProvider())
        index = FakeIndex([[fake_hit(0.85), fake_hit(0.65)]])
        events = []

        router.route_with_retrieval(
            "登录接口已经有验证码，还需要限制密码错误次数吗？",
            index=index,
            top_k=2,
            trace_callback=events.append,
        )

        self.assertTrue(events)
        self.assertEqual("security.rag.search.completed", events[0]["event"])
        self.assertEqual("fast_dense", events[0]["payload"]["stage"])
        self.assertEqual("high", events[0]["payload"]["tier"])

    def test_route_with_retrieval_abstains_without_evidence(self) -> None:
        router = SecurityRetrievalRouter(embeddings=HashEmbeddingProvider())
        index = FakeIndex([[]])

        plan = router.route_with_retrieval("这个少见安全问题是否存在？", index=index)

        self.assertFalse(plan.decision.use_rag)
        self.assertEqual("abstain", plan.action)
        self.assertEqual("abstain_no_evidence", plan.decision.route)

    def test_route_with_retrieval_asks_clarification_for_complex_no_evidence(self) -> None:
        router = SecurityRetrievalRouter(embeddings=HashEmbeddingProvider())
        index = FakeIndex([[], [], []])

        plan = router.route_with_retrieval("这个 PR 同时改鉴权和 SQL，我审查顺序怎么排？", index=index)

        self.assertFalse(plan.decision.use_rag)
        self.assertEqual("ask_clarification", plan.action)
        self.assertEqual("ask_clarification", plan.decision.route)
        self.assertIn("需要补充", plan.decision.clarification)

    def test_route_with_retrieval_expands_only_after_medium_evidence(self) -> None:
        router = SecurityRetrievalRouter(embeddings=HashEmbeddingProvider())
        index = FakeIndex([[fake_hit(0.50)], [fake_hit(0.83)]])

        plan = router.route_with_retrieval("JWT 放在 localStorage 里会有什么风险？", index=index)

        self.assertTrue(plan.decision.use_rag)
        self.assertEqual("expansion", plan.action)
        self.assertEqual("JWT 放在 localStorage 里会有什么风险？", index.calls[0]["query"])
        self.assertIn("token storage", index.calls[1]["query"])
        self.assertTrue(index.calls[1]["use_reranker"])
        self.assertEqual("medium", plan.searches[0].tier)
        self.assertEqual("expansion_rerank", plan.searches[1].stage)

    def test_route_with_retrieval_uses_rewrite_provider_for_medium_evidence(self) -> None:
        router = SecurityRetrievalRouter(
            embeddings=HashEmbeddingProvider(),
            rewrite_provider=FakeRewriteProvider(),
        )
        index = FakeIndex([[fake_hit(0.50)], [fake_hit(0.83)]])

        plan = router.route_with_retrieval("JWT 放在 localStorage 里会有什么风险？", index=index)

        self.assertEqual("expansion", plan.action)
        self.assertEqual("jwt token storage httponly samesite xss csrf", index.calls[1]["query"])

    def test_route_with_retrieval_decomposes_complex_query(self) -> None:
        router = SecurityRetrievalRouter(embeddings=HashEmbeddingProvider())
        index = FakeIndex([[fake_hit(0.40)], [fake_hit(0.84)]])

        plan = router.route_with_retrieval(
            "用户上传 ZIP 后服务端解压并用文件名执行转换命令，可能同时涉及哪些漏洞链？",
            index=index,
        )

        self.assertTrue(plan.decision.use_rag)
        self.assertEqual("decompose", plan.action)
        self.assertGreaterEqual(len(index.calls), 2)
        self.assertEqual("decompose_dense", plan.searches[1].stage)
        self.assertTrue(any(record.stage == "decompose_rerank" and record.use_reranker for record in plan.searches))

    def test_decompose_filters_empty_rewrite_queries(self) -> None:
        router = SecurityRetrievalRouter(
            embeddings=HashEmbeddingProvider(),
            rewrite_provider=EmptyDecomposeRewriteProvider(),
        )
        index = FakeIndex([[fake_hit(0.40)], [fake_hit(0.84)]])

        router.route_with_retrieval(
            "用户上传 ZIP 后服务端解压并用文件名执行转换命令，可能同时涉及哪些漏洞链？",
            index=index,
        )

        self.assertTrue(index.calls)
        self.assertTrue(all(call["query"].strip() for call in index.calls))

    def test_llm_rewrite_provider_parses_json(self) -> None:
        router = SecurityRetrievalRouter(embeddings=HashEmbeddingProvider())
        provider = LLMQueryRewriteProvider(
            runner=FakeRunner('{"query":"authorization bypass","queries":["authorization bypass","access control"]}'),
            spec=FakeSpec(),
            fallback=FakeRewriteProvider(),
            max_tokens=64,
            max_queries=3,
        )
        decision = router.route("这个接口安全吗")
        request = router._rewrite_request("这个接口安全吗", mode="expansion", decision=decision)

        result = provider.rewrite(request)

        self.assertEqual("authorization bypass", result.query)
        self.assertEqual(["authorization bypass", "access control"], result.queries)
        self.assertEqual("llm", result.provider)
        self.assertEqual(
            '{"query":"authorization bypass","queries":["authorization bypass","access control"]}',
            result.metadata["llm_raw_content"],
        )

    def test_llm_rewrite_provider_falls_back_on_error(self) -> None:
        router = SecurityRetrievalRouter(embeddings=HashEmbeddingProvider())
        provider = LLMQueryRewriteProvider(
            runner=FakeRunner("", fail=True),
            spec=FakeSpec(),
            fallback=FakeRewriteProvider(),
            max_tokens=64,
        )
        decision = router.route("JWT 放在 localStorage 里会有什么风险？")
        request = router._rewrite_request("JWT 放在 localStorage 里会有什么风险？", mode="expansion", decision=decision)

        result = provider.rewrite(request)

        self.assertEqual("fake", result.provider)
        self.assertEqual("jwt token storage httponly samesite xss csrf", result.query)

    def test_llm_rewrite_provider_falls_back_on_empty_content(self) -> None:
        router = SecurityRetrievalRouter(embeddings=HashEmbeddingProvider())
        provider = LLMQueryRewriteProvider(
            runner=FakeRunner(""),
            spec=FakeSpec(),
            fallback=FakeRewriteProvider(),
            max_tokens=64,
        )
        decision = router.route("JWT 放在 localStorage 里会有什么风险？")
        request = router._rewrite_request("JWT 放在 localStorage 里会有什么风险？", mode="pre_dense", decision=decision)

        result = provider.rewrite(request)

        self.assertEqual("fake", result.provider)
        self.assertEqual("jwt token storage httponly samesite xss csrf", result.query)
        self.assertIn("empty content", result.reason)
        self.assertEqual("", result.metadata["llm_raw_content"])

    def test_llm_rewrite_provider_caches_by_query_and_mode(self) -> None:
        runner = FakeRunner('{"query":"authorization bypass","queries":["authorization bypass"]}')
        router = SecurityRetrievalRouter(embeddings=HashEmbeddingProvider())
        provider = LLMQueryRewriteProvider(
            runner=runner,
            spec=FakeSpec(),
            fallback=FakeRewriteProvider(),
            max_tokens=64,
            cache_max_size=8,
            cache_ttl_seconds=60,
        )
        decision = router.route("这个接口安全吗")
        request = router._rewrite_request("这个接口安全吗", mode="expansion", decision=decision)

        first = provider.rewrite(request)
        second = provider.rewrite(request)

        self.assertEqual("authorization bypass", first.query)
        self.assertEqual("authorization bypass", second.query)
        self.assertEqual(1, len(runner.calls))
        self.assertEqual("true", second.metadata["rewrite_cache_hit"])


if __name__ == "__main__":
    unittest.main()
