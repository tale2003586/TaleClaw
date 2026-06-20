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
from retrieval import build_security_retrieval_router_from_env


DEFAULT_OUTPUT = ROOT / "benchmarks" / "security_rag_testset_v2.jsonl"


def main() -> int:
    load_dotenv_file(ROOT / ".env")
    parser = argparse.ArgumentParser(description="Interactively label Security RAG relevance.")
    parser.add_argument("query")
    parser.add_argument("--id", default=None, help="Case id. Default: sec-rag-labeled-N.")
    parser.add_argument("--category", default="")
    parser.add_argument("--language", default="")
    parser.add_argument("--collection", default=None)
    parser.add_argument("--top-k", type=int, default=10)
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT))
    args = parser.parse_args()

    router = build_security_retrieval_router_from_env()
    decision = router.route(args.query)
    index = build_security_index_from_env(collection=args.collection)
    hits = index.search(decision.query, top_k=args.top_k, min_score=decision.min_score)

    print(f"route={decision.route} use_rag={decision.use_rag}")
    print(f"rewritten_query={decision.query}")
    print()
    for index_num, hit in enumerate(hits, start=1):
        preview = hit.text[:600].replace("\n", " ")
        print(f"[{index_num}] id={hit.id}")
        print(f"    score={hit.score:.4f} source={hit.source_relpath}")
        print(f"    title={hit.title}")
        print(f"    {preview}")
        print()

    relevant = parse_indexes(input("Relevant hit numbers, comma separated: "), len(hits))
    irrelevant = parse_indexes(input("Irrelevant hit numbers, comma separated: "), len(hits))
    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    case_id = args.id or f"sec-rag-labeled-{line_count(output_path) + 1:04d}"
    record = {
        "id": case_id,
        "query": args.query,
        "language": args.language,
        "category": args.category,
        "should_use_rag": True,
        "relevant_chunk_ids": [hits[index - 1].id for index in relevant],
        "irrelevant_chunk_ids": [hits[index - 1].id for index in irrelevant],
        "min_relevant_hits_in_top5": min(2, len(relevant)) if relevant else 1,
        "notes": "Labeled with scripts/label_rag_relevance.py",
    }
    with output_path.open("a", encoding="utf-8") as file:
        file.write(json.dumps(record, ensure_ascii=False) + "\n")
    print(f"Wrote {case_id} to {output_path}")
    return 0


def parse_indexes(value: str, max_index: int) -> list[int]:
    indexes = []
    for part in value.replace(" ", "").split(","):
        if not part:
            continue
        try:
            index = int(part)
        except ValueError:
            continue
        if 1 <= index <= max_index:
            indexes.append(index)
    return sorted(set(indexes))


def line_count(path: Path) -> int:
    if not path.exists():
        return 0
    return sum(1 for line in path.read_text(encoding="utf-8").splitlines() if line.strip())


if __name__ == "__main__":
    raise SystemExit(main())
