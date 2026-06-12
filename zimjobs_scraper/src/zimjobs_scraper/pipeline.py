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
from .progress import ProgressReporter
from .validators import JobValidator

log = logging.getLogger(__name__)


def load_sources(path: str | Path) -> list[SourceConfig]:
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    return [SourceConfig.from_dict(item) for item in data if item.get("enabled", True)]


class ScrapePipeline:
    def __init__(
        self,
        sources: Iterable[SourceConfig],
        http: HttpClient | None = None,
        progress: ProgressReporter | None = None,
    ):
        self.sources = list(sources)
        self.http = http or HttpClient()
        self.progress = progress or ProgressReporter(enabled=False)

    def collect(self) -> list[JobRecord]:
        raw_by_source: list[tuple[SourceConfig, list[RawJob]]] = []
        total_sources = len(self.sources)
        for index, config in enumerate(self.sources, start=1):
            self.progress.source_start(index, total_sources, config.name)
            source_jobs = self._collect_source(config)
            raw_by_source.append((config, source_jobs))
            self.progress.source_done(config.name, len(source_jobs))

        total_raw = sum(len(jobs) for _, jobs in raw_by_source)
        mapped: list[JobRecord] = []
        valid = 0
        invalid = 0
        current = 0
        for config, source_jobs in raw_by_source:
            validator = JobValidator(skip_expired=config.skip_expired, allowed_locations=config.allowed_locations)
            for raw in source_jobs:
                current += 1
                job = map_raw_job(raw, config)
                result = validator.validate(job)
                if result.ok:
                    mapped.append(job)
                    valid += 1
                else:
                    invalid += 1
                    log.info(
                        "validation_skipped",
                        extra={"source": config.name, "job_title": job.title, "url": job.apply_url, "status": ",".join(result.reasons)},
                    )
                self.progress.validation_progress(current, total_raw, valid, invalid)

        deduped = dedupe_in_memory(mapped)
        self.progress.dedupe_done(len(mapped), len(deduped))
        return deduped

    def _collect_source(self, config: SourceConfig) -> list[RawJob]:
        parser = make_parser(config)
        detail_urls: list[str] = []
        seen: set[str] = set()
        max_detail = int(os.getenv("MAX_DETAIL_PER_SOURCE", str(config.max_detail_pages)))
        start_urls = config.start_urls[: int(os.getenv("MAX_PAGES", str(config.max_pages)))]
        for page_index, start_url in enumerate(start_urls, start=1):
            self.progress.listing_page(config.name, page_index, len(start_urls), start_url)
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
        self.progress.detail_urls_found(config.name, len(detail_urls))
        raw_jobs: list[RawJob] = []
        parse_failed = 0
        details_to_parse = detail_urls[:max_detail]
        for detail_index, url in enumerate(details_to_parse, start=1):
            html = self.http.get(url)
            if not html:
                continue
            try:
                raw = parser.parse_detail(html, url)
                if raw:
                    raw_jobs.append(raw)
                    log.info("parsed_job", extra={"source": config.name, "job_title": raw.title or "", "url": url})
            except Exception:
                parse_failed += 1
                log.exception("parse_failed", extra={"source": config.name, "url": url})
            finally:
                self.progress.parse_progress(config.name, detail_index, len(details_to_parse), len(raw_jobs), parse_failed)
        return raw_jobs


def run(
    config_path: str,
    db_path: str,
    dry_run: bool = False,
    table_name: str = "jobs",
    show_progress: bool = True,
    progress_every: int = 1,
) -> dict[str, int]:
    sources = load_sources(config_path)
    progress = ProgressReporter.from_env(enabled=show_progress, every=progress_every)
    progress.start(len(sources), dry_run)
    pipeline = ScrapePipeline(sources, progress=progress)
    jobs = pipeline.collect()
    repo = SQLiteJobRepository(db_path, table_name=table_name)
    try:
        stats = repo.insert_many(
            jobs,
            dry_run=dry_run,
            progress_callback=lambda current, total, current_stats: progress.db_progress(
                current, total, current_stats["inserted"], current_stats["skipped"], current_stats["failed"], dry_run
            ),
        )
    finally:
        repo.close()
    log.info("pipeline_finished", extra={**stats, "status": "done"})
    progress.finish(stats)
    return stats
