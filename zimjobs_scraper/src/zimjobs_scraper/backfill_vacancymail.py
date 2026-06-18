from __future__ import annotations

import argparse
import logging
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from .normalization import (
    clean_text,
    content_hash,
    is_vacancy_mail_company,
    is_zimbabwe_vacancy_mail_context,
    resolve_vacancy_mail_zimbabwe_company,
)

log = logging.getLogger(__name__)


@dataclass(slots=True)
class BackfillStats:
    scanned: int = 0
    updated: int = 0
    skipped: int = 0
    fts_rebuilt: bool = False


def _safe_identifier(value: str) -> str:
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", value):
        raise ValueError(f"Unsafe SQL identifier: {value!r}")
    return value


def _columns(conn: sqlite3.Connection, table_name: str) -> set[str]:
    rows = conn.execute(f"PRAGMA table_info({_safe_identifier(table_name)})").fetchall()
    return {str(row["name"]) for row in rows}


def _row_text(row: sqlite3.Row) -> str:
    return "\n".join(
        clean_text(row[col], max_spaces=False)
        for col in ("summary", "job_description", "requirements")
        if col in row.keys() and row[col]
    )


def backfill_vacancy_mail_companies(
    db_path: str,
    *,
    table_name: str = "jobs",
    dry_run: bool = False,
) -> BackfillStats:
    table = _safe_identifier(table_name)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    stats = BackfillStats()
    try:
        available = _columns(conn, table)
        selected = [
            col
            for col in (
                "id",
                "title",
                "company",
                "location",
                "summary",
                "apply_url",
                "source_name",
                "source_url",
                "job_description",
                "requirements",
                "external_job_id",
                "content_hash",
            )
            if col in available
        ]
        rows = conn.execute(
            f"SELECT {', '.join(selected)} FROM {table} "
            "WHERE lower(replace(trim(company), ' ', '')) = 'vacancymail'"
        ).fetchall()
        for row in rows:
            stats.scanned += 1
            source_name = row["source_name"] if "source_name" in row.keys() else None
            source_url = row["source_url"] if "source_url" in row.keys() else None
            apply_url = row["apply_url"] if "apply_url" in row.keys() else None
            if not is_vacancy_mail_company(row["company"]):
                stats.skipped += 1
                log.info("backfill_skip", extra={"status": "company_not_vacancy_mail", "job_id": row["id"]})
                continue
            if not is_zimbabwe_vacancy_mail_context(row["location"], source_name=source_name, source_url=source_url):
                stats.skipped += 1
                log.info("backfill_skip", extra={"status": "not_zimbabwe_context", "job_id": row["id"]})
                continue

            new_company = resolve_vacancy_mail_zimbabwe_company(
                row["company"],
                row["title"],
                _row_text(row),
                row["location"],
                source_name=source_name,
                source_url=source_url,
                apply_url=apply_url,
            )
            if not new_company:
                stats.skipped += 1
                log.info("backfill_skip", extra={"status": "no_confident_company", "job_id": row["id"]})
                continue

            values: dict[str, object] = {"company": new_company, "id": row["id"]}
            if "content_hash" in available:
                values["content_hash"] = content_hash(
                    [
                        row["title"],
                        new_company,
                        row["location"],
                        row["summary"] if "summary" in row.keys() else "",
                        apply_url,
                        source_url,
                        row["external_job_id"] if "external_job_id" in row.keys() else "",
                    ]
                )
            if dry_run:
                log.info("backfill_update_dry_run", extra={"status": new_company, "job_id": row["id"]})
            else:
                set_sql = "company=:company"
                if "content_hash" in values:
                    set_sql += ", content_hash=:content_hash"
                conn.execute(f"UPDATE {table} SET {set_sql} WHERE id=:id", values)
            stats.updated += 1

        if not dry_run and stats.updated:
            fts_table = f"{table}_fts"
            fts_exists = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                (fts_table,),
            ).fetchone()
            if fts_exists:
                conn.execute(f"INSERT INTO {_safe_identifier(fts_table)}({_safe_identifier(fts_table)}) VALUES('rebuild')")
                stats.fts_rebuilt = True
            conn.commit()
        elif dry_run:
            conn.rollback()
        return stats
    finally:
        conn.close()


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Backfill Vacancy Mail Zimbabwe rows with inferred hiring company names.")
    parser.add_argument("--db", required=True, help="Path to SQLite jobs.db")
    parser.add_argument("--table", default="jobs", help="Jobs table name")
    parser.add_argument("--dry-run", action="store_true", help="Log updates without writing")
    parser.add_argument("--log-level", default="INFO")
    return parser


def main() -> None:
    args = build_arg_parser().parse_args()
    logging.basicConfig(level=getattr(logging, args.log_level.upper(), logging.INFO), format="%(levelname)s %(message)s")
    stats = backfill_vacancy_mail_companies(str(Path(args.db)), table_name=args.table, dry_run=args.dry_run)
    print(
        {
            "scanned": stats.scanned,
            "updated": stats.updated,
            "skipped": stats.skipped,
            "fts_rebuilt": stats.fts_rebuilt,
            "dry_run": args.dry_run,
        }
    )


if __name__ == "__main__":
    main()
