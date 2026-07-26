from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from memory.command_service import MemoryCommandService
from memory.migration.legacy_importer import LegacyMemoryImporter
from memory.postgres_repository import PostgresMemoryRepository
from runtime.env_loader import load_dotenv_file


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Dry-run or apply a legacy Markdown/JSON long-term memory import.",
    )
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--user-id", required=True)
    parser.add_argument("--apply", action="store_true", help="Write to PostgreSQL; default is dry-run.")
    parser.add_argument("--include-candidates", action="store_true")
    parser.add_argument("--report-path", type=Path)
    parser.add_argument("--checkpoint", type=Path)
    args = parser.parse_args()

    load_dotenv_file(ROOT / ".env")
    repository = PostgresMemoryRepository()
    report = LegacyMemoryImporter(
        MemoryCommandService(repository),
        repository,
    ).import_root(
        args.source_root,
        user_id=args.user_id,
        dry_run=not args.apply,
        include_candidates=args.include_candidates,
        checkpoint_path=args.checkpoint,
    )
    rendered = json.dumps(report.to_dict(), ensure_ascii=False, indent=2) + "\n"
    if args.report_path:
        args.report_path.parent.mkdir(parents=True, exist_ok=True)
        args.report_path.write_text(rendered, encoding="utf-8")
    sys.stdout.write(rendered)
    return 1 if report.counts().get("failed") else 0


if __name__ == "__main__":
    raise SystemExit(main())
