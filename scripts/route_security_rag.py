from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from runtime.env_loader import load_dotenv_file
from retrieval import build_security_route_classifier_from_env, build_security_retrieval_router_from_env


def main() -> int:
    load_dotenv_file(ROOT / ".env")
    parser = argparse.ArgumentParser(description="Route a user query to the code security RAG knowledge base.")
    parser.add_argument("query", help="User query.")
    parser.add_argument("--llm", action="store_true", help="Use an LLM classifier for the ambiguous middle band.")
    args = parser.parse_args()

    router = build_security_retrieval_router_from_env()
    classifier = build_security_route_classifier_from_env(
        config=router.config,
        enabled=args.llm,
    )
    decision = router.route(args.query, llm_classifier=classifier)
    print(json.dumps(decision.to_dict(), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
