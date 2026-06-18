from __future__ import annotations

import argparse
import os

from .db import SQLiteJobRepository
from .indexnow import submit_changed_urls_to_indexnow
from .logging_config import configure_logging


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Remove expired and low-quality jobs from the zimjobs SQLite database.")
    parser.add_argument(
        "--db",
        default=os.getenv("JOBS_DB_PATH") or os.getenv("DB_PATH", "/data/jobs.db"),
        help="Path to SQLite jobs.db",
    )
    parser.add_argument("--table", default=os.getenv("JOBS_TABLE", "jobs"), help="Jobs table name")
    parser.add_argument("--dry-run", action="store_true", default=os.getenv("DRY_RUN", "0") == "1", help="Count matching jobs without deleting")
    parser.add_argument(
        "--bad-descriptions",
        action="store_true",
        default=os.getenv("CLEAN_BAD_DESCRIPTIONS", "0") == "1",
        help="Delete jobs containing known scraped spam/boilerplate description markers.",
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        default=os.getenv("CONFIRM_DELETE_EXPIRED", "0") == "1",
        help="Confirm deletion. Back up the database before using this outside scheduled maintenance.",
    )
    parser.add_argument("--log-level", default=os.getenv("LOG_LEVEL", "INFO"))
    return parser


def clean_expired_jobs(db_path: str, table_name: str = "jobs", dry_run: bool = False) -> dict[str, int]:
    repo = SQLiteJobRepository(db_path, table_name=table_name)
    try:
        deleted = repo.delete_expired_jobs(dry_run=dry_run)
        changed_urls = list(repo.deleted_urls)
        fts_rebuilt = 0
        if deleted and not dry_run:
            fts_rebuilt = int(repo.rebuild_fts_if_present())
        indexnow_submitted = 0
        if changed_urls and not dry_run:
            indexnow_submitted = int(submit_changed_urls_to_indexnow(changed_urls))
        return {
            "expired_jobs": deleted,
            "deleted": 0 if dry_run else deleted,
            "dry_run": int(dry_run),
            "fts_rebuilt": fts_rebuilt,
            "indexnow_urls": len(set(changed_urls)) if not dry_run else 0,
            "indexnow_submitted": indexnow_submitted,
            "total_jobs": repo.count_jobs(),
        }
    finally:
        repo.close()


def clean_jobs(
    db_path: str,
    table_name: str = "jobs",
    dry_run: bool = False,
    bad_descriptions: bool = False,
) -> dict[str, int]:
    repo = SQLiteJobRepository(db_path, table_name=table_name)
    try:
        expired = repo.delete_expired_jobs(dry_run=dry_run)
        changed_urls = list(repo.deleted_urls)
        bad = repo.delete_bad_description_jobs(dry_run=dry_run) if bad_descriptions else 0
        deleted = 0 if dry_run else expired + bad
        fts_rebuilt = 0
        if deleted:
            fts_rebuilt = int(repo.rebuild_fts_if_present())
        indexnow_submitted = 0
        if changed_urls and not dry_run:
            indexnow_submitted = int(submit_changed_urls_to_indexnow(changed_urls))
        return {
            "expired_jobs": expired,
            "bad_description_jobs": bad,
            "deleted": deleted,
            "dry_run": int(dry_run),
            "fts_rebuilt": fts_rebuilt,
            "indexnow_urls": len(set(changed_urls)) if not dry_run else 0,
            "indexnow_submitted": indexnow_submitted,
            "total_jobs": repo.count_jobs(),
        }
    finally:
        repo.close()


def main() -> None:
    args = build_arg_parser().parse_args()
    configure_logging(args.log_level)
    if not args.dry_run and not args.yes:
        raise SystemExit(
            "Refusing to delete without --yes or CONFIRM_DELETE_EXPIRED=1. "
            "Back up the SQLite database first, then retry."
        )
    stats = clean_jobs(
        args.db,
        table_name=args.table,
        dry_run=args.dry_run,
        bad_descriptions=args.bad_descriptions,
    )
    print(stats)


if __name__ == "__main__":
    main()
