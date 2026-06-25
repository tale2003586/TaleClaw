from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT = ROOT / "benchmarks" / "security_rag_user_questions_testset.jsonl"
QUESTION_HEADER = re.compile(r"^###\s+Q(?P<number>\d+)\s*$")
SECTION_HEADER = re.compile(r"^##\s+(?P<number>\d+)\.\s+(?P<title>.+?)\s*$")
QUESTION_PREFIX = "- 问题："
TYPE_PREFIX = "- 类型："
DIFFICULTY_PREFIX = "- 难度："
ROLE_PREFIX = "- 用户角色："
TAGS_PREFIX = "- 标签："
RAG_NEED_PREFIX = "- RAG需求："


SECTION_TERMS = {
    "身份认证与登录安全": [
        "authentication",
        "login",
        "password",
        "session",
        "jwt",
        "token",
        "mfa",
        "brute force",
    ],
    "权限控制与越权": [
        "authorization",
        "access control",
        "idor",
        "rbac",
        "privilege",
        "tenant",
    ],
    "输入校验与注入风险": [
        "input validation",
        "injection",
        "sql injection",
        "command injection",
        "nosql",
        "ldap",
        "redos",
    ],
    "XSS / 前端安全": [
        "xss",
        "cross-site scripting",
        "innerhtml",
        "csp",
        "sanitization",
        "output encoding",
    ],
    "SSRF / 请求转发风险": [
        "ssrf",
        "server-side request forgery",
        "metadata",
        "url",
        "redirect",
        "allowlist",
    ],
    "文件上传、路径穿越与文件处理": [
        "file upload",
        "path traversal",
        "zip slip",
        "filename",
        "content type",
        "sandbox",
    ],
    "反序列化与对象注入": [
        "deserialization",
        "object injection",
        "pickle",
        "yaml",
        "rce",
        "schema validation",
    ],
    "敏感信息泄露": [
        "sensitive data",
        "information disclosure",
        "secret",
        "token",
        "pii",
        "sourcemap",
    ],
    "日志、报错与调试信息": [
        "logging",
        "error handling",
        "stack trace",
        "debug",
        "sensitive data",
        "audit",
    ],
    "依赖漏洞与供应链安全": [
        "dependency",
        "advisory",
        "vulnerability",
        "cve",
        "ghsa",
        "package",
        "supply chain",
    ],
    "配置安全、密钥与环境变量": [
        "configuration",
        "secret",
        "environment",
        "cors",
        "tls",
        "hardcoded",
    ],
    "API 安全与接口设计": [
        "api security",
        "rate limiting",
        "csrf",
        "websocket",
        "authentication",
        "authorization",
    ],
    "数据库与 ORM 安全": [
        "database",
        "orm",
        "sql injection",
        "prepared statement",
        "least privilege",
        "nosql injection",
    ],
    "云服务、容器与部署安全": [
        "cloud",
        "container",
        "docker",
        "kubernetes",
        "iam",
        "metadata",
        "ci/cd",
    ],
    "代码审查中的综合判断问题": [
        "code review",
        "triage",
        "authorization",
        "sql injection",
        "taint flow",
        "threat modeling",
    ],
    "安全新手常见问题": [
        "secure coding",
        "trust boundary",
        "input validation",
        "least privilege",
        "risk rating",
        "scanner",
    ],
    "干扰项 / 容易误检索类问题": [
        "xss",
        "sql injection",
        "ssrf",
        "open redirect",
        "csrf",
        "sensitive data",
    ],
    "Coding Agent 代码安全关注点": [
        "ai coding agent",
        "code review",
        "authorization",
        "sql injection",
        "secret detection",
        "regression",
    ],
}


def main() -> int:
    parser = argparse.ArgumentParser(description="Convert user-style Security RAG questions markdown to JSONL.")
    parser.add_argument("input", type=Path, help="security_rag_user_questions.md path")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument(
        "--optional-as-rag",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Map `RAG需求：可选` to should_use_rag=true. Disable to count only `需要` as true.",
    )
    args = parser.parse_args()

    cases = parse_markdown(args.input, optional_as_rag=args.optional_as_rag)
    if not cases:
        raise SystemExit(f"No questions found in {args.input}")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("w", encoding="utf-8") as file:
        for case in cases:
            file.write(json.dumps(case, ensure_ascii=False, separators=(",", ":")) + "\n")

    print(f"wrote {len(cases)} cases -> {args.output}")
    print("by question_type:", dict(sorted(Counter(case["question_type"] for case in cases).items())))
    print("by rag_need:", dict(sorted(Counter(case["rag_need"] for case in cases).items())))
    print("by difficulty:", dict(sorted(Counter(case["difficulty"] for case in cases).items())))
    print("should_use_rag:", dict(sorted(Counter(str(case["should_use_rag"]) for case in cases).items())))
    return 0


def parse_markdown(path: Path, *, optional_as_rag: bool = True) -> list[dict[str, Any]]:
    cases: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    section = ""

    def flush() -> None:
        nonlocal current
        if current is None:
            return
        missing = [
            key
            for key in ["source_id", "query", "question_type", "rag_need", "difficulty", "user_role"]
            if not current.get(key)
        ]
        if missing:
            raise ValueError(f"Missing {', '.join(missing)} for {current.get('source_id', '<unknown>')}")
        cases.append(build_case(current, optional_as_rag=optional_as_rag))
        current = None

    with path.open("r", encoding="utf-8") as file:
        for raw_line in file:
            line = raw_line.rstrip("\r\n")
            if line == "# 覆盖统计":
                flush()
                break
            section_match = SECTION_HEADER.match(line)
            if section_match:
                section = section_match.group("title").strip()
                continue
            question_match = QUESTION_HEADER.match(line)
            if question_match:
                flush()
                number = int(question_match.group("number"))
                current = {"number": number, "source_id": f"Q{number:03d}", "section": section}
                continue
            if current is None:
                continue
            if line.startswith(QUESTION_PREFIX):
                current["query"] = line[len(QUESTION_PREFIX) :]
            elif line.startswith(TYPE_PREFIX):
                current["question_type"] = line[len(TYPE_PREFIX) :].strip()
            elif line.startswith(RAG_NEED_PREFIX):
                current["rag_need"] = line[len(RAG_NEED_PREFIX) :].strip()
            elif line.startswith(DIFFICULTY_PREFIX):
                current["difficulty"] = line[len(DIFFICULTY_PREFIX) :].strip()
            elif line.startswith(ROLE_PREFIX):
                current["user_role"] = line[len(ROLE_PREFIX) :].strip()
            elif line.startswith(TAGS_PREFIX):
                current["tags"] = [item.strip() for item in line[len(TAGS_PREFIX) :].split(",") if item.strip()]
    flush()
    return cases


def build_case(raw: dict[str, Any], *, optional_as_rag: bool = True) -> dict[str, Any]:
    question_type = str(raw["question_type"])
    rag_need = str(raw["rag_need"])
    should_use_rag = rag_need_to_bool(rag_need, optional_as_rag=optional_as_rag)
    tags = list(raw.get("tags") or [])
    return {
        "id": f"sec-rag-user-{int(raw['number']):03d}",
        "source_id": raw["source_id"],
        "query": raw["query"],
        "language": "zh" if has_cjk(raw["query"]) else "en",
        "category": question_type,
        "question_type": question_type,
        "rag_need": rag_need,
        "topic": slugify(str(raw.get("section") or "")),
        "topic_label": str(raw.get("section") or ""),
        "difficulty": str(raw["difficulty"]),
        "user_role": str(raw["user_role"]),
        "tags": tags,
        "should_use_rag": should_use_rag,
        "expected_terms": expected_terms(
            question_type=question_type,
            section=str(raw.get("section") or ""),
            tags=tags,
            should_use_rag=should_use_rag,
        ),
        "expected_source_hints": expected_source_hints(section=str(raw.get("section") or ""), tags=tags)
        if should_use_rag
        else [],
        "notes": f"User-style blind test question from {raw['source_id']}; query preserved exactly.",
    }


def rag_need_to_bool(value: str, *, optional_as_rag: bool) -> bool:
    normalized = str(value or "").strip()
    if normalized == "需要":
        return True
    if normalized == "可选":
        return bool(optional_as_rag)
    if normalized == "不适用":
        return False
    raise ValueError(f"Unknown RAG需求 value: {value!r}")


def expected_terms(*, question_type: str, section: str, tags: list[str], should_use_rag: bool) -> list[str]:
    if not should_use_rag:
        return []
    terms: list[str] = []
    for tag in tags:
        tag = tag.lower()
        terms.extend([tag, tag.replace("-", " "), tag.replace("-", "_")])
    terms.extend(item.lower() for item in SECTION_TERMS.get(section, []))
    if question_type in {"practical_debugging", "rule_mapping"}:
        terms.extend(["semgrep", "code review", "taint"])
    return dedupe([term for term in terms if len(term) >= 3])


def expected_source_hints(*, section: str, tags: list[str]) -> list[str]:
    tag_blob = " ".join(tags).lower()
    hints: list[str] = []
    if any(term in tag_blob for term in ["dependency", "cve", "ghsa", "package", "lockfile", "base-image"]):
        hints.append("advisory-database")
    if any(
        term in tag_blob or term in section.lower()
        for term in ["docker", "kubernetes", "terraform", "container", "ci-cd", "github", "iac"]
    ):
        hints.append("semgrep-rules")
    if not hints:
        hints.extend(["CheatSheetSeries", "semgrep-rules"])
    return dedupe(hints)


def has_cjk(value: str) -> bool:
    return any("\u4e00" <= char <= "\u9fff" for char in value)


def slugify(value: str) -> str:
    mapping = {
        "身份认证与登录安全": "authentication_login_security",
        "权限控制与越权": "authorization_access_control",
        "输入校验与注入风险": "input_validation_injection",
        "XSS / 前端安全": "xss_frontend_security",
        "SSRF / 请求转发风险": "ssrf_request_forwarding",
        "文件上传、路径穿越与文件处理": "file_upload_path_traversal",
        "反序列化与对象注入": "deserialization_object_injection",
        "敏感信息泄露": "sensitive_information_disclosure",
        "日志、报错与调试信息": "logging_error_debug",
        "依赖漏洞与供应链安全": "dependency_supply_chain",
        "配置安全、密钥与环境变量": "configuration_secrets_env",
        "API 安全与接口设计": "api_security_design",
        "数据库与 ORM 安全": "database_orm_security",
        "云服务、容器与部署安全": "cloud_container_deployment",
        "代码审查中的综合判断问题": "code_review_triage",
        "安全新手常见问题": "security_basics",
        "不可回答 / 证据不足类问题": "unanswerable_insufficient_evidence",
        "干扰项 / 容易误检索类问题": "distractor_ambiguous_retrieval",
        "Coding Agent 代码安全关注点": "coding_agent_security",
        "反例 / 不应直接回答类问题": "negative_non_security_or_no_rag",
    }
    return mapping.get(value, re.sub(r"[^a-z0-9]+", "_", value.lower()).strip("_"))


def dedupe(values: list[str]) -> list[str]:
    seen: set[str] = set()
    output: list[str] = []
    for value in values:
        key = value.lower()
        if key in seen:
            continue
        seen.add(key)
        output.append(value)
    return output


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(1)
