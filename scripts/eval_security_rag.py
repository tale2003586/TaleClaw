from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from runtime.env_loader import load_dotenv_file
from knowledge.security_rag import KnowledgeHit, build_security_index_from_env
from retrieval import build_security_route_classifier_from_env, build_security_retrieval_router_from_env


DEFAULT_TESTSET = ROOT / "benchmarks" / "security_rag_testset.jsonl"


@dataclass(frozen=True)
class CaseResult:
    case_id: str
    category: str
    should_use_rag: bool
    route_use_rag: bool
    route: str
    route_ok: bool
    query: str
    rewritten_query: str
    top_score: float
    top_source: str
    term_hit_count: int
    expected_term_count: int
    source_hint_hit: bool
    hit_count: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.case_id,
            "category": self.category,
            "should_use_rag": self.should_use_rag,
            "route_use_rag": self.route_use_rag,
            "route": self.route,
            "route_ok": self.route_ok,
            "query": self.query,
            "rewritten_query": self.rewritten_query,
            "top_score": self.top_score,
            "top_source": self.top_source,
            "term_hit_count": self.term_hit_count,
            "expected_term_count": self.expected_term_count,
            "source_hint_hit": self.source_hint_hit,
            "hit_count": self.hit_count,
        }


def main() -> int:
    load_dotenv_file(ROOT / ".env")
    parser = argparse.ArgumentParser(description="Evaluate the code security RAG testset.")
    parser.add_argument("--testset", default=str(DEFAULT_TESTSET), help="JSONL testset path.")
    parser.add_argument("--collection", default=None, help="Qdrant collection override.")
    parser.add_argument("--top-k", type=int, default=None)
    parser.add_argument("--min-score", type=float, default=None)
    parser.add_argument("--search-all", action="store_true", help="Search even when the router says use_rag=false.")
    parser.add_argument("--llm-classifier", action="store_true", help="Use the LLM classifier for ambiguous routing.")
    parser.add_argument("--no-llm-classifier", action="store_true", help="Disable the LLM classifier for this run.")
    parser.add_argument("--jsonl", action="store_true", help="Print machine-readable JSONL results.")
    parser.add_argument("--show-hits", type=int, default=0, help="Print first N hits per searched case.")
    args = parser.parse_args()

    cases = load_cases(Path(args.testset))
    router = build_security_retrieval_router_from_env()
    classifier = build_security_route_classifier_from_env(
        config=router.config,
        enabled=_llm_enabled(args),
    )
    index = build_security_index_from_env(collection=args.collection)

    results: list[CaseResult] = []
    for case in cases:
        decision = router.route(str(case["query"]), llm_classifier=classifier)
        should_use_rag = bool(case.get("should_use_rag"))
        do_search = decision.use_rag or args.search_all
        hits: list[KnowledgeHit] = []
        if do_search:
            hits = index.search(
                decision.query,
                top_k=args.top_k or decision.top_k,
                min_score=args.min_score if args.min_score is not None else decision.min_score,
            )
        result = evaluate_case(case, decision, hits)
        results.append(result)
        if args.jsonl:
            print(json.dumps(result.to_dict(), ensure_ascii=False))
        else:
            print_result(result, hits=hits, show_hits=args.show_hits)

    if not args.jsonl:
        print_summary(results)
    return 0


def load_cases(path: Path) -> list[dict[str, Any]]:
    cases: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as file:
        for line_number, line in enumerate(file, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                item = json.loads(line)
            except json.JSONDecodeError as exc:
                raise SystemExit(f"Invalid JSON at {path}:{line_number}: {exc}") from exc
            if "id" not in item or "query" not in item:
                raise SystemExit(f"Missing id/query at {path}:{line_number}")
            cases.append(item)
    return cases


def _llm_enabled(args) -> bool:
    if args.no_llm_classifier:
        return False
    if args.llm_classifier:
        return True
    return _env_bool("SECURITY_RAG_ROUTE_LLM_ENABLED", False)


def _env_bool(name: str, default: bool = False) -> bool:
    import os

    value = os.getenv(name)
    if value is None or value == "":
        return bool(default)
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def evaluate_case(case: dict[str, Any], decision: Any, hits: list[KnowledgeHit]) -> CaseResult:
    expected_terms = [str(term).lower() for term in case.get("expected_terms", []) if str(term).strip()]
    expected_source_hints = [
        str(hint).lower() for hint in case.get("expected_source_hints", []) if str(hint).strip()
    ]
    hit_blob = "\n".join(
        f"{hit.source_relpath}\n{hit.title}\n{hit.text}" for hit in hits
    ).lower()
    source_blob = "\n".join(f"{hit.source_relpath}\n{hit.title}" for hit in hits).lower()
    term_hit_count = sum(1 for term in expected_terms if term in hit_blob)
    source_hint_hit = any(hint in source_blob for hint in expected_source_hints) if expected_source_hints else False
    top = hits[0] if hits else None
    should_use_rag = bool(case.get("should_use_rag"))
    return CaseResult(
        case_id=str(case["id"]),
        category=str(case.get("category", "")),
        should_use_rag=should_use_rag,
        route_use_rag=bool(decision.use_rag),
        route=str(decision.route),
        route_ok=bool(decision.use_rag) == should_use_rag,
        query=str(case["query"]),
        rewritten_query=str(decision.query),
        top_score=float(top.score) if top else 0.0,
        top_source=str(top.source_relpath) if top else "",
        term_hit_count=term_hit_count,
        expected_term_count=len(expected_terms),
        source_hint_hit=source_hint_hit,
        hit_count=len(hits),
    )


def print_result(result: CaseResult, *, hits: list[KnowledgeHit], show_hits: int) -> None:
    status = "OK" if result.route_ok else "ROUTE_MISMATCH"
    print(
        f"{result.case_id} [{status}] category={result.category} "
        f"route={result.route} use_rag={result.route_use_rag}/{result.should_use_rag} "
        f"hits={result.hit_count} terms={result.term_hit_count}/{result.expected_term_count} "
        f"source_hint={result.source_hint_hit} top={result.top_score:.4f}"
    )
    print(f"  query: {result.query}")
    if result.rewritten_query != result.query:
        print(f"  rewritten: {result.rewritten_query}")
    if result.top_source:
        print(f"  top_source: {result.top_source}")
    for rank, hit in enumerate(hits[: max(0, show_hits)], start=1):
        preview = hit.text[:240].replace("\n", " ")
        print(f"  hit[{rank}] score={hit.score:.4f} source={hit.source_relpath} title={hit.title}")
        print(f"    {preview}")


def print_summary(results: list[CaseResult]) -> None:
    total = len(results)
    route_ok = sum(1 for item in results if item.route_ok)
    positives = [item for item in results if item.should_use_rag]
    searched = [item for item in results if item.hit_count > 0]
    term_hits = sum(1 for item in positives if item.expected_term_count and item.term_hit_count > 0)
    source_hits = sum(1 for item in positives if item.source_hint_hit)
    print()
    print("Summary")
    print(f"  cases: {total}")
    print(f"  route_ok: {route_ok}/{total}")
    print(f"  searched_cases: {len(searched)}/{total}")
    print(f"  positive_term_hit: {term_hits}/{len(positives)}")
    print(f"  positive_source_hint_hit: {source_hits}/{len(positives)}")


if __name__ == "__main__":
    raise SystemExit(main())
