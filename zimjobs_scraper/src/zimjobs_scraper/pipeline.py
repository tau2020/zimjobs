from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Iterable

from .db import SQLiteJobRepository
from .dedupe import dedupe_in_memory
from .http_client import HttpClient
from .mapper import map_raw_job
from .models import JobRecord, RawJob
from .parsers import SourceConfig, make_parser
from .validators import JobValidator

log = logging.getLogger(__name__)


def load_sources(path: str | Path) -> list[SourceConfig]:
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    return [SourceConfig.from_dict(item) for item in data if item.get("enabled", True)]


class ScrapePipeline:
    def __init__(self, sources: Iterable[SourceConfig], http: HttpClient | None = None):
        self.sources = list(sources)
        self.http = http or HttpClient()

    def collect(self) -> list[JobRecord]:
        mapped: list[JobRecord] = []
        for config in self.sources:
            source_jobs = self._collect_source(config)
            validator = JobValidator(skip_expired=config.skip_expired, allowed_locations=config.allowed_locations)
            for raw in source_jobs:
                job = map_raw_job(raw, config)
                result = validator.validate(job)
                if result.ok:
                    mapped.append(job)
                else:
                    log.info(
                        "validation_skipped",
                        extra={"source": config.name, "job_title": job.title, "url": job.apply_url, "status": ",".join(result.reasons)},
                    )
        return dedupe_in_memory(mapped)

    def _collect_source(self, config: SourceConfig) -> list[RawJob]:
        parser = make_parser(config)
        detail_urls: list[str] = []
        seen: set[str] = set()
        max_detail = int(os.getenv("MAX_DETAIL_PER_SOURCE", str(config.max_detail_pages)))
        for start_url in config.start_urls[: int(os.getenv("MAX_PAGES", str(config.max_pages)))]:
            html = self.http.get(start_url)
            if not html:
                continue
            urls = parser.list_job_urls(html, start_url)
            if not urls and start_url not in seen:
                urls = [start_url]
            for url in urls:
                if url not in seen:
                    seen.add(url)
                    detail_urls.append(url)
                if len(detail_urls) >= max_detail:
                    break
            if len(detail_urls) >= max_detail:
                break
        log.info("source_detail_urls", extra={"source": config.name, "status": len(detail_urls)})
        raw_jobs: list[RawJob] = []
        for url in detail_urls[:max_detail]:
            html = self.http.get(url)
            if not html:
                continue
            try:
                raw = parser.parse_detail(html, url)
                if raw:
                    raw_jobs.append(raw)
                    log.info("parsed_job", extra={"source": config.name, "job_title": raw.title or "", "url": url})
            except Exception:
                log.exception("parse_failed", extra={"source": config.name, "url": url})
        return raw_jobs


def run(config_path: str, db_path: str, dry_run: bool = False, table_name: str = "jobs") -> dict[str, int]:
    sources = load_sources(config_path)
    pipeline = ScrapePipeline(sources)
    jobs = pipeline.collect()
    repo = SQLiteJobRepository(db_path, table_name=table_name)
    try:
        stats = repo.insert_many(jobs, dry_run=dry_run)
    finally:
        repo.close()
    log.info("pipeline_finished", extra={**stats, "status": "done"})
    return stats
