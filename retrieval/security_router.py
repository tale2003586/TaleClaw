from __future__ import annotations

from dataclasses import asdict, dataclass, field
import json
import math
import os
import re
from typing import Protocol

from knowledge.security_rag import build_security_embedding_provider_from_env
from memory.embeddings import EmbeddingProvider


class LLMRouteClassifier(Protocol):
    def __call__(self, query: str, *, embedding_score: float, matched_intent: str) -> "RetrievalDecision":
        ...


@dataclass(frozen=True)
class SecurityRouteConfig:
    high_threshold: float = 0.72
    low_threshold: float = 0.45
    llm_accept_threshold: float = 0.60
    default_top_k: int = 5
    min_score: float = 0.0


@dataclass(frozen=True)
class RetrievalDecision:
    use_rag: bool
    target: str | None
    query: str
    confidence: float
    reason: str
    route: str
    keyword_matches: list[str] = field(default_factory=list)
    embedding_score: float = 0.0
    matched_intent: str = ""
    llm_required: bool = False
    top_k: int = 5
    min_score: float = 0.0

    def to_dict(self) -> dict:
        return asdict(self)


class LlmSecurityRouteClassifier:
    def __init__(
        self,
        *,
        runner,
        spec,
        accept_threshold: float,
        default_top_k: int,
        min_score: float,
        max_tokens: int = 350,
    ) -> None:
        self.runner = runner
        self.spec = spec
        self.accept_threshold = float(accept_threshold)
        self.default_top_k = max(1, int(default_top_k))
        self.min_score = float(min_score)
        self.max_tokens = max(1, int(max_tokens))

    def __call__(
        self,
        query: str,
        *,
        embedding_score: float,
        matched_intent: str,
    ) -> RetrievalDecision:
        prompt = _llm_classifier_prompt(
            query=query,
            embedding_score=embedding_score,
            matched_intent=matched_intent,
        )
        try:
            content = self.runner.run(
                spec=self.spec,
                messages=[
                    {
                        "role": "system",
                        "content": (
                            "You are a retrieval router. Return only a strict "
                            "JSON object with no markdown."
                        ),
                    },
                    {"role": "user", "content": prompt},
                ],
                max_tokens=self.max_tokens,
            )
        except Exception as exc:
            return RetrievalDecision(
                use_rag=False,
                target=None,
                query=query,
                confidence=float(embedding_score or 0.0),
                reason=f"LLM route classifier failed: {type(exc).__name__}: {exc}",
                route="llm_error",
            )

        payload = _parse_json_object(content)
        confidence = _float(payload.get("confidence"), 0.0)
        use_rag = bool(payload.get("needs_retrieval")) and confidence >= self.accept_threshold
        rewritten = str(payload.get("query") or query).strip() or query
        return RetrievalDecision(
            use_rag=use_rag,
            target="security_kb" if use_rag else None,
            query=rewritten,
            confidence=confidence,
            reason=str(payload.get("reason") or "LLM classified retrieval need."),
            route="llm",
            embedding_score=float(embedding_score or 0.0),
            matched_intent=matched_intent,
            top_k=self.default_top_k,
            min_score=self.min_score,
        )


DEFAULT_SECURITY_KEYWORDS = {
    "sql injection": "SQL injection prevention prepared statements parameterized queries",
    "sqli": "SQL injection prevention prepared statements parameterized queries",
    "xss": "XSS prevention output encoding content security policy sanitization",
    "csrf": "CSRF prevention SameSite anti CSRF token",
    "ssrf": "SSRF prevention metadata service private IP allowlist",
    "rce": "remote code execution command injection prevention",
    "cve": "CVE vulnerability remediation patch advisory",
    "cwe": "CWE vulnerability mitigation secure coding",
    "jwt": "JWT token storage HttpOnly SameSite XSS CSRF",
    "token": "token storage leakage authentication security",
    "deserialization": "unsafe deserialization remote code execution prevention",
    "path traversal": "path traversal canonical path validation file access",
    "command injection": "command injection subprocess shell input validation",
    "file upload": "file upload security validation malware path traversal",
    "权限": "authorization access control bypass privilege escalation",
    "越权": "authorization bypass access control vulnerability",
    "认证": "authentication security session token password",
    "授权": "authorization access control permission check",
    "漏洞": "software vulnerability remediation secure coding",
    "注入": "injection vulnerability SQL command LDAP input validation",
    "路径穿越": "path traversal canonical path validation file access",
    "反序列化": "unsafe deserialization remote code execution prevention",
    "命令执行": "command injection remote code execution prevention",
    "文件上传": "file upload security validation malware path traversal",
}


DEFAULT_SECURITY_INTENTS = [
    "SQL injection prevention prepared statements parameterized queries",
    "XSS prevention output encoding content security policy sanitization",
    "CSRF prevention SameSite anti CSRF token validation",
    "SSRF prevention metadata service private IP allowlist denylist",
    "authorization bypass access control permission check vulnerability",
    "authentication session token password security best practices",
    "JWT token storage HttpOnly Secure SameSite cookie localStorage risk",
    "path traversal file access canonical path validation",
    "file upload security validation malware zip slip path traversal",
    "command injection subprocess shell true input validation escaping",
    "unsafe deserialization pickle yaml load remote code execution",
    "secret leakage credential token API key exposure logs",
    "dependency vulnerability CVE remediation package upgrade",
    "cryptography misuse weak hash encryption random number generation",
    "CORS misconfiguration origin allow credentials security",
    "rate limiting brute force login authentication protection",
    "权限绕过风险 接口越权 访问控制漏洞",
    "用户输入校验是否安全 注入风险 参数处理",
    "token 应该怎么存储 cookie localStorage 安全",
]


class SecurityRetrievalRouter:
    def __init__(
        self,
        *,
        embeddings: EmbeddingProvider,
        config: SecurityRouteConfig | None = None,
        keywords: dict[str, str] | None = None,
        intents: list[str] | None = None,
    ) -> None:
        self.embeddings = embeddings
        self.config = config or SecurityRouteConfig()
        self.keywords = keywords or DEFAULT_SECURITY_KEYWORDS
        self.intents = intents or DEFAULT_SECURITY_INTENTS
        self._intent_vectors: list[list[float]] | None = None

    def route(
        self,
        query: str,
        *,
        llm_classifier: LLMRouteClassifier | None = None,
    ) -> RetrievalDecision:
        query = _normalize_query(query)
        if not query:
            return self._no_rag(query, route="empty", reason="Empty query.")

        keyword_matches = self._keyword_matches(query)
        if keyword_matches:
            rewritten = self.rewrite_query(query, keyword_matches=keyword_matches)
            return RetrievalDecision(
                use_rag=True,
                target="security_kb",
                query=rewritten,
                confidence=0.90,
                reason="Matched explicit security keyword.",
                route="keyword",
                keyword_matches=keyword_matches,
                top_k=self.config.default_top_k,
                min_score=self.config.min_score,
            )

        score, matched_intent = self.intent_similarity(query)
        if score >= self.config.high_threshold:
            return RetrievalDecision(
                use_rag=True,
                target="security_kb",
                query=self.rewrite_query(query, matched_intent=matched_intent, intent_score=score),
                confidence=score,
                reason="Matched security intent by embedding similarity.",
                route="embedding_high",
                embedding_score=score,
                matched_intent=matched_intent,
                top_k=self.config.default_top_k,
                min_score=self.config.min_score,
            )

        if score <= self.config.low_threshold:
            return self._no_rag(
                query,
                route="embedding_low",
                reason="Security intent similarity is below the low threshold.",
                embedding_score=score,
                matched_intent=matched_intent,
            )

        if llm_classifier is not None:
            decision = llm_classifier(query, embedding_score=score, matched_intent=matched_intent)
            if decision.use_rag:
                return RetrievalDecision(
                    use_rag=True,
                    target=decision.target or "security_kb",
                    query=decision.query or self.rewrite_query(query, matched_intent=matched_intent, intent_score=score),
                    confidence=decision.confidence,
                    reason=decision.reason or "LLM classifier accepted retrieval.",
                    route="llm",
                    embedding_score=score,
                    matched_intent=matched_intent,
                    top_k=decision.top_k or self.config.default_top_k,
                    min_score=decision.min_score,
                )
            return RetrievalDecision(
                use_rag=False,
                target=None,
                query=query,
                confidence=decision.confidence,
                reason=decision.reason or "LLM classifier rejected retrieval.",
                route=decision.route or "llm",
                embedding_score=score,
                matched_intent=matched_intent,
            )

        return RetrievalDecision(
            use_rag=False,
            target=None,
            query=query,
            confidence=score,
            reason="Security intent is ambiguous; LLM classifier is required.",
            route="embedding_middle",
            embedding_score=score,
            matched_intent=matched_intent,
            llm_required=True,
        )

    def intent_similarity(self, query: str) -> tuple[float, str]:
        query_vector = self.embeddings.embed(query)
        best_score = -1.0
        best_intent = ""
        for intent, vector in zip(self.intents, self._get_intent_vectors()):
            score = _cosine(query_vector, vector)
            if score > best_score:
                best_score = score
                best_intent = intent
        return max(0.0, best_score), best_intent

    def rewrite_query(
        self,
        query: str,
        *,
        keyword_matches: list[str] | None = None,
        matched_intent: str = "",
        intent_score: float = 0.0,
    ) -> str:
        if keyword_matches:
            keyword = keyword_matches[0]
            expansion = self.keywords.get(keyword)
            if expansion:
                return _dedupe_words(f"{query} {expansion}")
        if matched_intent and float(intent_score or 0.0) >= 0.80:
            return _dedupe_words(f"{query} {matched_intent}")
        return query

    def _keyword_matches(self, query: str) -> list[str]:
        lowered = query.lower()
        matches = []
        for keyword in self.keywords:
            if re.search(rf"(?<![a-z0-9_]){re.escape(keyword.lower())}(?![a-z0-9_])", lowered):
                matches.append(keyword)
        return matches

    def _get_intent_vectors(self) -> list[list[float]]:
        if self._intent_vectors is None:
            self._intent_vectors = [self.embeddings.embed(intent) for intent in self.intents]
        return self._intent_vectors

    def _no_rag(
        self,
        query: str,
        *,
        route: str,
        reason: str,
        embedding_score: float = 0.0,
        matched_intent: str = "",
    ) -> RetrievalDecision:
        return RetrievalDecision(
            use_rag=False,
            target=None,
            query=query,
            confidence=max(0.0, float(embedding_score)),
            reason=reason,
            route=route,
            embedding_score=embedding_score,
            matched_intent=matched_intent,
        )


def build_security_retrieval_router_from_env(
    *,
    embeddings: EmbeddingProvider | None = None,
) -> SecurityRetrievalRouter:
    config = SecurityRouteConfig(
        high_threshold=_env_float("SECURITY_RAG_ROUTE_HIGH_THRESHOLD", 0.72),
        low_threshold=_env_float("SECURITY_RAG_ROUTE_LOW_THRESHOLD", 0.45),
        llm_accept_threshold=_env_float("SECURITY_RAG_ROUTE_LLM_ACCEPT_THRESHOLD", 0.60),
        default_top_k=_env_int("SECURITY_RAG_ROUTE_TOP_K", 5),
        min_score=_env_float("SECURITY_RAG_ROUTE_MIN_SCORE", 0.0),
    )
    return SecurityRetrievalRouter(
        embeddings=embeddings or build_security_embedding_provider_from_env(),
        config=config,
    )


def build_security_route_classifier_from_env(
    *,
    config: SecurityRouteConfig | None = None,
    enabled: bool | None = None,
    model_pool=None,
) -> LLMRouteClassifier | None:
    config = config or SecurityRouteConfig(
        high_threshold=_env_float("SECURITY_RAG_ROUTE_HIGH_THRESHOLD", 0.72),
        low_threshold=_env_float("SECURITY_RAG_ROUTE_LOW_THRESHOLD", 0.45),
        llm_accept_threshold=_env_float("SECURITY_RAG_ROUTE_LLM_ACCEPT_THRESHOLD", 0.60),
        default_top_k=_env_int("SECURITY_RAG_ROUTE_TOP_K", 5),
        min_score=_env_float("SECURITY_RAG_ROUTE_MIN_SCORE", 0.0),
    )
    if enabled is None:
        enabled = _env_bool("SECURITY_RAG_ROUTE_LLM_ENABLED", False)
    if not enabled:
        return None

    from models.model_task_runner import ModelTaskRunner
    from runtime.agent_spec import AgentSpec
    if model_pool is None:
        from runtime.bootstrap import get_model_pool

        model_pool = get_model_pool()

    max_tokens = _env_int("SECURITY_RAG_ROUTE_LLM_MAX_TOKENS", 350)
    purpose = os.getenv("SECURITY_RAG_ROUTE_LLM_PURPOSE", "summary").strip() or "summary"
    runner = ModelTaskRunner(model_pool=model_pool, default_max_tokens=max_tokens)
    spec = AgentSpec(
        name="security_rag_route_classifier",
        profile=None,
        model_purpose=purpose,
        max_tokens=max_tokens,
    )
    return LlmSecurityRouteClassifier(
        runner=runner,
        spec=spec,
        accept_threshold=config.llm_accept_threshold,
        default_top_k=config.default_top_k,
        min_score=config.min_score,
        max_tokens=max_tokens,
    )


def _normalize_query(query: str) -> str:
    return re.sub(r"\s+", " ", str(query or "")).strip()


def _llm_classifier_prompt(*, query: str, embedding_score: float, matched_intent: str) -> str:
    return f"""
判断用户问题是否需要查询“代码安全 RAG 知识库”。

只输出 JSON，不要输出解释文字。schema:
{{
  "needs_retrieval": true,
  "confidence": 0.0,
  "reason": "...",
  "query": "适合检索的中英混合 query"
}}

应该检索的范围：
- 代码安全、漏洞、CVE/CWE/GHSA、依赖漏洞、补丁建议
- 认证、授权、权限绕过、访问控制
- SQL/命令/模板/LDAP 等注入、XSS、CSRF、SSRF
- 反序列化、文件上传、路径穿越、token/JWT/密钥泄露
- 加密误用、CORS、限流、云配置安全

不应该检索：
- 普通聊天、写作、项目计划、非安全代码问题、天气/新闻等外部实时信息

要求：
- 如果 needs_retrieval=true，query 应改写成适合本地安全知识库召回的关键词，优先保留 CVE/GHSA/CWE、漏洞类型、语言/框架、关键组件名。
- confidence 使用 0 到 1。

用户问题：{query}
embedding_score: {embedding_score:.4f}
matched_intent: {matched_intent}
""".strip()


def _parse_json_object(text: str) -> dict:
    text = str(text or "").strip()
    try:
        payload = json.loads(text)
        return payload if isinstance(payload, dict) else {}
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", text, re.S)
        if match:
            try:
                payload = json.loads(match.group(0))
                return payload if isinstance(payload, dict) else {}
            except json.JSONDecodeError:
                return {}
    return {}


def _dedupe_words(text: str) -> str:
    words = _normalize_query(text).split(" ")
    seen = set()
    output = []
    for word in words:
        key = word.lower()
        if key in seen:
            continue
        seen.add(key)
        output.append(word)
    return " ".join(output)


def _cosine(left: list[float], right: list[float]) -> float:
    if not left or not right:
        return 0.0
    size = min(len(left), len(right))
    dot = sum(left[index] * right[index] for index in range(size))
    left_norm = math.sqrt(sum(value * value for value in left[:size]))
    right_norm = math.sqrt(sum(value * value for value in right[:size]))
    if left_norm <= 0 or right_norm <= 0:
        return 0.0
    return dot / (left_norm * right_norm)


def _env_float(name: str, default: float) -> float:
    value = os.getenv(name)
    if value is None or value == "":
        return float(default)
    try:
        return float(value)
    except ValueError:
        return float(default)


def _env_int(name: str, default: int) -> int:
    value = os.getenv(name)
    if value is None or value == "":
        return int(default)
    try:
        return int(value)
    except ValueError:
        return int(default)


def _env_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None or value == "":
        return bool(default)
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _float(value, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return float(default)
