from __future__ import annotations

import argparse
import os

from .db import SQLiteJobRepository
from .logging_config import configure_logging


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Remove expired jobs from the zimjobs SQLite database.")
    parser.add_argument("--db", default=os.getenv("JOBS_DB_PATH", "/data/jobs.db"), help="Path to SQLite jobs.db")
    parser.add_argument("--table", default=os.getenv("JOBS_TABLE", "jobs"), help="Jobs table name")
    parser.add_argument("--dry-run", action="store_true", default=os.getenv("DRY_RUN", "0") == "1", help="Count expired jobs without deleting")
    parser.add_argument("--log-level", default=os.getenv("LOG_LEVEL", "INFO"))
    return parser


def clean_expired_jobs(db_path: str, table_name: str = "jobs", dry_run: bool = False) -> dict[str, int]:
    repo = SQLiteJobRepository(db_path, table_name=table_name)
    try:
        deleted = repo.delete_expired_jobs(dry_run=dry_run)
        fts_rebuilt = 0
        if deleted and not dry_run:
            fts_rebuilt = int(repo.rebuild_fts_if_present())
        return {
            "expired_jobs": deleted,
            "deleted": 0 if dry_run else deleted,
            "dry_run": int(dry_run),
            "fts_rebuilt": fts_rebuilt,
            "total_jobs": repo.count_jobs(),
        }
    finally:
        repo.close()


def main() -> None:
    args = build_arg_parser().parse_args()
    configure_logging(args.log_level)
    stats = clean_expired_jobs(args.db, table_name=args.table, dry_run=args.dry_run)
    print(stats)


if __name__ == "__main__":
    main()
