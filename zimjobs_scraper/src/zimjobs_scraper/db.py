from __future__ import annotations

import logging
import os
import re
import sqlite3
from dataclasses import asdict
from pathlib import Path
from typing import Callable, Iterable

from .dedupe import canonical_url_key, identity_keys, normalized_key, similar
from .models import JobRecord

log = logging.getLogger(__name__)

CORE_COLUMNS = ["title", "company", "location", "category", "summary", "apply_url", "featured", "created_at"]
OPTIONAL_COLUMNS_SQL = {
    "source_name": "TEXT",
    "source_url": "TEXT",
    "posted_at": "TEXT",
    "expires_at": "TEXT",
    "department": "TEXT",
    "employment_type": "TEXT",
    "salary_range": "TEXT",
    "remote_status": "TEXT",
    "job_description": "TEXT",
    "requirements": "TEXT",
    "external_job_id": "TEXT",
    "content_hash": "TEXT",
    "scraped_at": "TEXT",
}


class SQLiteJobRepository:
    def __init__(self, db_path: str, table_name: str = "jobs", auto_add_optional_columns: bool | None = None):
        self.db_path = db_path
        self.table_name = table_name
        self.auto_add_optional_columns = (
            os.getenv("AUTO_ADD_OPTIONAL_COLUMNS", "0") == "1" if auto_add_optional_columns is None else auto_add_optional_columns
        )
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(db_path)
        self.conn.row_factory = sqlite3.Row
        self.ensure_schema()

    def close(self) -> None:
        self.conn.close()

    def ensure_schema(self) -> None:
        self.conn.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {self.table_name} (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                company TEXT NOT NULL,
                location TEXT NOT NULL,
                category TEXT NOT NULL,
                summary TEXT NOT NULL,
                apply_url TEXT NOT NULL,
                featured INTEGER DEFAULT 0,
                created_at TEXT DEFAULT (datetime('now'))
            )
            """
        )
        self.conn.execute(f"CREATE INDEX IF NOT EXISTS idx_{self.table_name}_apply_url ON {self.table_name}(apply_url)")
        self.conn.execute(f"CREATE INDEX IF NOT EXISTS idx_{self.table_name}_category ON {self.table_name}(category)")
        self.conn.execute(f"CREATE INDEX IF NOT EXISTS idx_{self.table_name}_location ON {self.table_name}(location)")
        if self.auto_add_optional_columns:
            existing = self.columns()
            for col, sql_type in OPTIONAL_COLUMNS_SQL.items():
                if col not in existing:
                    self.conn.execute(f"ALTER TABLE {self.table_name} ADD COLUMN {col} {sql_type}")
                    log.info("db_column_added", extra={"status": col})
        self.conn.commit()

    def columns(self) -> set[str]:
        rows = self.conn.execute(f"PRAGMA table_info({self.table_name})").fetchall()
        return {row["name"] for row in rows}

    def existing_candidates(self) -> list[sqlite3.Row]:
        cols = self.columns()
        selected = [
            c
            for c in [
                "id",
                "title",
                "company",
                "location",
                "summary",
                "apply_url",
                "source_name",
                "source_url",
                "external_job_id",
                "content_hash",
            ]
            if c in cols
        ]
        return self.conn.execute(f"SELECT {', '.join(selected)} FROM {self.table_name}").fetchall()

    def exists_duplicate(self, job: JobRecord) -> bool:
        rows = self.existing_candidates()
        incoming_key = normalized_key(job)
        incoming_identities = set(identity_keys(job))
        for row in rows:
            if "content_hash" in row.keys() and row["content_hash"] and job.content_hash and row["content_hash"] == job.content_hash:
                return True
            row_key = "|".join(
                re.sub(r"\W+", " ", str(row[col] or "").lower()).strip()
                for col in ["title", "company", "location"]
            )
            row_identities = set(self._row_identity_keys(row))
            shared_identities = incoming_identities & row_identities
            if any(identity.startswith("external:") for identity in shared_identities):
                return True
            if shared_identities:
                if incoming_key == row_key or similar(row["summary"] or "", job.summary) >= 0.90:
                    return True
            if incoming_key == row_key and similar(row["summary"] or "", job.summary) >= 0.90:
                return True
        return False

    def _row_identity_keys(self, row: sqlite3.Row) -> list[str]:
        source_name = row["source_name"] if "source_name" in row.keys() else None
        external_job_id = row["external_job_id"] if "external_job_id" in row.keys() else None
        if source_name and external_job_id:
            external = re.sub(r"\W+", " ", str(external_job_id).strip().lower()).strip()
            return [f"external:{str(source_name).strip().lower()}:{external}"] if external else []
        keys = []
        source_url = canonical_url_key(row["source_url"] if "source_url" in row.keys() else "")
        apply_url = canonical_url_key(row["apply_url"] if "apply_url" in row.keys() else "")
        if source_url:
            keys.append(f"source:{source_url}")
        if apply_url and apply_url != source_url:
            keys.append(f"apply:{apply_url}")
        return keys

    def insert(self, job: JobRecord) -> bool:
        if self.exists_duplicate(job):
            log.info("db_skip_duplicate", extra={"job_title": job.title, "url": job.apply_url})
            return False
        available = self.columns()
        data = asdict(job)
        insert_cols = [col for col in CORE_COLUMNS + list(OPTIONAL_COLUMNS_SQL.keys()) if col in available and data.get(col) is not None]
        placeholders = ", ".join([":" + col for col in insert_cols])
        sql = f"INSERT INTO {self.table_name} ({', '.join(insert_cols)}) VALUES ({placeholders})"
        self.conn.execute(sql, {col: data[col] for col in insert_cols})
        self.conn.commit()
        log.info("db_inserted", extra={"job_title": job.title, "url": job.apply_url})
        return True

    def insert_many(
        self,
        jobs: Iterable[JobRecord],
        dry_run: bool = False,
        progress_callback: Callable[[int, int, dict[str, int]], None] | None = None,
    ) -> dict[str, int]:
        jobs = list(jobs)
        total = len(jobs)
        stats = {"inserted": 0, "skipped": 0, "failed": 0}
        for index, job in enumerate(jobs, start=1):
            try:
                if self.exists_duplicate(job):
                    stats["skipped"] += 1
                    continue
                if dry_run:
                    log.info("dry_run_insert", extra={"job_title": job.title, "url": job.apply_url})
                    stats["inserted"] += 1
                else:
                    inserted = self.insert(job)
                    stats["inserted" if inserted else "skipped"] += 1
            except Exception:
                stats["failed"] += 1
                log.exception("db_insert_failed", extra={"job_title": job.title, "url": job.apply_url})
            finally:
                if progress_callback:
                    progress_callback(index, total, stats)
        return stats

    def count_jobs(self) -> int:
        row = self.conn.execute(f"SELECT COUNT(*) AS count FROM {self.table_name}").fetchone()
        return int(row["count"])

    def delete_expired_jobs(self, dry_run: bool = False) -> int:
        available = self.columns()
        if "expires_at" not in available:
            return 0
        where = (
            "expires_at IS NOT NULL AND TRIM(expires_at) <> '' "
            "AND date(substr(expires_at, 1, 10)) < date('now')"
        )
        row = self.conn.execute(f"SELECT COUNT(*) AS count FROM {self.table_name} WHERE {where}").fetchone()
        count = int(row["count"])
        if dry_run or count == 0:
            return count

        self.rebuild_fts_if_present()

        saved_exists = self.conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='saved_jobs'"
        ).fetchone()
        if saved_exists:
            self.conn.execute(
                f"DELETE FROM saved_jobs WHERE job_id IN "
                f"(SELECT id FROM {self.table_name} WHERE {where})"
            )
        self.conn.execute(f"DELETE FROM {self.table_name} WHERE {where}")
        self.conn.commit()
        log.info("db_expired_deleted", extra={"status": count})
        return count

    def rebuild_fts_if_present(self) -> bool:
        row = self.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
            (f"{self.table_name}_fts",),
        ).fetchone()
        if not row:
            return False
        self.conn.execute(f"INSERT INTO {self.table_name}_fts({self.table_name}_fts) VALUES('rebuild')")
        self.conn.commit()
        log.info("db_fts_rebuilt", extra={"status": f"{self.table_name}_fts"})
        return True
