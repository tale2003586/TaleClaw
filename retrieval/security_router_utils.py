from __future__ import annotations

import json
import math
import os
import re

from .security_router_models import RewriteRequest, RewriteResult


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


def _llm_rewrite_prompt(*, request: RewriteRequest, fallback: RewriteResult, max_queries: int) -> str:
    hits_json = json.dumps(request.top_hits[:3], ensure_ascii=False)
    fallback_json = json.dumps(fallback.queries or [fallback.query], ensure_ascii=False)
    mode_instruction = {
        "pre_dense": "生成 1 条第一跳 fast dense 检索前使用的轻量 query，尽量短，保留原问题关键词，只补充必要安全术语；不要直接照抄 fallback_queries。",
        "expansion": "生成 1 条更适合向量检索的中英混合 query，保留原问题含义，并补充安全术语。",
        "low_retry": "生成 1 条更明确的重试检索 query，优先补充漏洞类型、攻击面、修复关键词。",
        "decompose": f"把复杂安全问题拆成 2-{max_queries} 条可独立检索的中英混合 query。",
    }.get(request.mode, "生成适合本地代码安全知识库检索的 query。")
    return f"""
你只负责“改写本地代码安全知识库的检索 query”，不要回答用户问题。

输出必须是 JSON，schema:
{{
  "query": "最重要的一条检索 query",
  "queries": ["query1", "query2"],
  "reason": "一句话说明改写依据"
}}

硬性要求：
- 不要改变用户原意，不要引入原问题没有的具体 CVE/GHSA/包名/框架。
- 保留用户提到的语言、框架、组件名、漏洞类型、CVE/CWE/GHSA、文件/配置关键词。
- 可以补充常见英文安全术语，例如 SQL injection、authorization、SSRF、path traversal、XSS、CSP、token storage。
- queries 最多 {max_queries} 条；如果 mode 不是 decompose，通常只返回 1 条。
- 如果不确定，参考 fallback_queries，但可以用更好的英文安全术语重写；pre_dense 模式下不要逐字照抄 fallback_queries。

当前模式：{request.mode}
任务说明：{mode_instruction}

用户原问题：
{request.query}

路由上下文：
- route: {request.route}
- tier: {request.tier}
- matched_intent: {request.matched_intent}
- embedding_score: {request.embedding_score:.4f}
- top_score: {request.top_score:.4f}
- score_gap: {request.score_gap:.4f}
- source_concentration: {request.source_concentration:.4f}
- keyword_matches: {", ".join(request.keyword_matches)}

当前 fast retrieval top hits:
{hits_json}

fallback_queries:
{fallback_json}
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


def _summarize_hits_for_rewrite(hits: list) -> list[dict[str, str]]:
    output = []
    for hit in hits[:2]:
        output.append({
            "score": f"{float(getattr(hit, 'score', 0.0) or 0.0):.4f}",
            "source": str(getattr(hit, "source_relpath", ""))[:120],
            "title": str(getattr(hit, "title", ""))[:80],
            "text": _normalize_query(str(getattr(hit, "text", "")))[:160],
        })
    return output


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


def _dedupe_ordered(values: list[str]) -> list[str]:
    seen = set()
    output = []
    for value in values:
        normalized = _normalize_query(value)
        if not normalized:
            continue
        key = normalized.lower()
        if key in seen:
            continue
        seen.add(key)
        output.append(normalized)
    return output


def _clarification_for_query(query: str) -> str:
    lowered = query.lower()
    needs = []
    if any(marker in lowered for marker in ["代码", "源码", "函数", "接口", "pr"]):
        needs.append("相关代码片段或 PR diff")
    if any(marker in lowered for marker in ["日志", "报警", "被攻击", "被利用", "泄露", "线上"]):
        needs.append("运行日志、告警详情或事件时间线")
    if any(marker in lowered for marker in ["架构", "威胁模型", "攻击面", "内网拓扑", "资产清单"]):
        needs.append("架构图、信任边界或资产清单")
    if any(marker in lowered for marker in ["权限", "iam", "合规", "等保", "监管"]):
        needs.append("权限配置、合规要求或控制项证据")
    if not needs:
        needs.append("可验证的代码、配置、日志或架构上下文")
    return "需要补充：" + "；".join(_dedupe_ordered(needs)) + "。"


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


def _int(value, default: int = 0) -> int:
    try:
        if value is None:
            return int(default)
        return int(value)
    except (TypeError, ValueError):
        return int(default)


def _is_number(value) -> bool:
    try:
        float(value)
        return True
    except (TypeError, ValueError):
        return False
