from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from memory.domain import MemoryOwnerScope, OwnerKey
from memory.migration.rebuild_index import RebuildSemanticMemoryIndex
from memory.postgres_repository import PostgresMemoryRepository
from memory.vector_runtime import build_semantic_memory_index_from_env
from runtime.env_loader import load_dotenv_file


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Rebuild the derived semantic memory index from PostgreSQL.",
    )
    parser.add_argument("--apply", action="store_true", help="Write points; default is dry-run.")
    parser.add_argument("--owner-scope", choices=[value.value for value in MemoryOwnerScope])
    parser.add_argument("--owner-id")
    args = parser.parse_args()
    if bool(args.owner_scope) != bool(args.owner_id):
        parser.error("--owner-scope and --owner-id must be supplied together")

    load_dotenv_file(ROOT / ".env")
    owners = None
    if args.owner_scope:
        owners = [OwnerKey(MemoryOwnerScope(args.owner_scope), args.owner_id)]
    report = RebuildSemanticMemoryIndex(
        PostgresMemoryRepository(),
        build_semantic_memory_index_from_env(),
    ).rebuild(owners=owners, dry_run=not args.apply)
    sys.stdout.write(json.dumps(asdict(report), ensure_ascii=False, indent=2) + "\n")
    return 1 if report.failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
