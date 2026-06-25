from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from runtime.env_loader import load_dotenv_file
from knowledge.security_rag import build_security_index_from_env
from retrieval import build_security_route_classifier_from_env, build_security_retrieval_router_from_env


def main() -> int:
    load_dotenv_file(ROOT / ".env")
    parser = argparse.ArgumentParser(description="Route a query and search the security RAG when needed.")
    parser.add_argument("query", help="User query.")
    parser.add_argument("--collection", default=None)
    parser.add_argument("--top-k", type=int, default=None)
    parser.add_argument("--min-score", type=float, default=None)
    parser.add_argument("--chars", type=int, default=700)
    args = parser.parse_args()

    router = build_security_retrieval_router_from_env()
    classifier = build_security_route_classifier_from_env(config=router.config)
    index = build_security_index_from_env(collection=args.collection)
    plan = router.route_with_retrieval(
        args.query,
        index=index,
        llm_classifier=classifier,
        top_k=args.top_k,
        min_score=args.min_score,
    )
    decision = plan.decision
    print(json.dumps({"route_plan": plan.to_dict()}, ensure_ascii=False, indent=2))
    if not decision.use_rag:
        return 0

    hits = plan.hits
    for rank, hit in enumerate(hits, start=1):
        preview = hit.text[: max(80, args.chars)].replace("\n", "\n  ")
        print(f"[{rank}] score={hit.score:.4f} source={hit.source_relpath} title={hit.title}")
        print(f"  {preview}")
        print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
