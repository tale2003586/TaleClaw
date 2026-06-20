from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from runtime.env_loader import load_dotenv_file
from knowledge.security_rag import (
    DEFAULT_SOURCE_ROOT,
    build_security_index_from_env,
    chunks_from_file,
    iter_source_files,
)
from knowledge.incremental import IncrementalIngester


def main() -> int:
    load_dotenv_file(ROOT / ".env")
    parser = argparse.ArgumentParser(description="Ingest code security documents into Qdrant.")
    parser.add_argument(
        "--source",
        default=os.getenv("SECURITY_RAG_SOURCE_ROOT", str(DEFAULT_SOURCE_ROOT)),
        help="Knowledge source root.",
    )
    parser.add_argument("--collection", default=None, help="Qdrant collection name.")
    parser.add_argument("--recreate", action="store_true", help="Delete and recreate the collection first.")
    parser.add_argument("--limit-files", type=int, default=None, help="Limit number of files for smoke tests.")
    parser.add_argument("--max-file-bytes", type=int, default=1_000_000)
    parser.add_argument("--chunk-chars", type=int, default=1800)
    parser.add_argument("--overlap-chars", type=int, default=220)
    parser.add_argument("--batch-size", type=int, default=64)
    parser.add_argument("--incremental", action="store_true", help="Only index changed files and delete removed files.")
    parser.add_argument(
        "--state-file",
        default=os.getenv("SECURITY_RAG_INCREMENTAL_STATE", "~/.claude/rag_ingest_state.json"),
        help="Incremental ingestion state JSON path.",
    )
    args = parser.parse_args()

    source = Path(args.source).expanduser().resolve()
    if not source.exists():
        raise SystemExit(f"Source path does not exist: {source}")

    index = build_security_index_from_env(collection=args.collection)
    if args.recreate:
        index.recreate_collection()

    if args.incremental:
        result = IncrementalIngester(
            index=index,
            source_root=source,
            state_path=Path(args.state_file),
            iter_files=iter_source_files,
            chunk_file=chunks_from_file,
            max_file_bytes=args.max_file_bytes,
            chunk_chars=args.chunk_chars,
            overlap_chars=args.overlap_chars,
            batch_size=args.batch_size,
            limit_files=args.limit_files,
        ).sync()
        print(json.dumps({
            "source": str(source),
            "collection": index.collection,
            "state_file": str(Path(args.state_file).expanduser()),
            **result.to_dict(),
        }, ensure_ascii=False, indent=2))
        return 0

    total_files = 0
    total_chunks = 0
    indexed = 0
    for path in iter_source_files(
        source,
        max_file_bytes=args.max_file_bytes,
        limit=args.limit_files,
    ):
        chunks = chunks_from_file(
            path,
            root=source,
            chunk_chars=args.chunk_chars,
            overlap_chars=args.overlap_chars,
        )
        total_files += 1
        total_chunks += len(chunks)
        indexed += index.upsert_chunks(chunks, batch_size=args.batch_size)
        if total_files % 100 == 0:
            print(json.dumps({"files": total_files, "chunks": total_chunks, "indexed": indexed}, ensure_ascii=False), flush=True)

    print(
        json.dumps(
            {
                "source": str(source),
                "collection": index.collection,
                "files": total_files,
                "chunks": total_chunks,
                "indexed": indexed,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
