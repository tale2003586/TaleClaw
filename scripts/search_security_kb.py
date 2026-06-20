from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from runtime.env_loader import load_dotenv_file
from knowledge.security_rag import build_security_index_from_env


def main() -> int:
    load_dotenv_file(ROOT / ".env")
    parser = argparse.ArgumentParser(description="Search the code security RAG knowledge base.")
    parser.add_argument("query", help="Search query.")
    parser.add_argument("--collection", default=None)
    parser.add_argument("--top-k", type=int, default=6)
    parser.add_argument("--min-score", type=float, default=0.0)
    parser.add_argument("--chars", type=int, default=700)
    args = parser.parse_args()

    index = build_security_index_from_env(collection=args.collection)
    hits = index.search(args.query, top_k=args.top_k, min_score=args.min_score)
    for rank, hit in enumerate(hits, start=1):
        preview = hit.text[: max(80, args.chars)].replace("\n", "\n  ")
        print(f"[{rank}] score={hit.score:.4f} source={hit.source_relpath} title={hit.title}")
        print(f"  {preview}")
        print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
