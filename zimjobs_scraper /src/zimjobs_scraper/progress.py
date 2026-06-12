from __future__ import annotations

import os
import sys
from dataclasses import dataclass


def _truthy(value: str | None, default: bool = True) -> bool:
    if value is None:
        return default
    return value.strip().lower() not in {"0", "false", "no", "off", ""}


@dataclass(slots=True)
class ProgressReporter:
    """Line-based progress output that works well in Railway/container logs.

    This intentionally avoids carriage-return terminal tricks because Railway logs
    preserve line output more reliably than live TTY repainting.
    """

    enabled: bool = True
    every: int = 1
    prefix: str = "zimjobs"

    @classmethod
    def from_env(cls, enabled: bool | None = None, every: int | None = None) -> "ProgressReporter":
        return cls(
            enabled=_truthy(os.getenv("PROGRESS"), True) if enabled is None else enabled,
            every=max(1, int(os.getenv("PROGRESS_EVERY", "1"))) if every is None else max(1, every),
        )

    def write(self, message: str) -> None:
        if not self.enabled:
            return
        print(f"[{self.prefix}] {message}", file=sys.stdout, flush=True)

    def bar(self, current: int, total: int, width: int = 22) -> str:
        if total <= 0:
            return "[" + "░" * width + "] 0%"
        current = max(0, min(current, total))
        filled = int(round((current / total) * width))
        pct = int(round((current / total) * 100))
        return "[" + "█" * filled + "░" * (width - filled) + f"] {pct}%"

    def should_print(self, current: int, total: int) -> bool:
        return current == 1 or current == total or current % self.every == 0

    def start(self, sources_count: int, dry_run: bool) -> None:
        mode = "DRY RUN - no database writes" if dry_run else "LIVE INSERT"
        self.write("=" * 72)
        self.write(f"Starting scraper | sources={sources_count} | mode={mode}")
        self.write("=" * 72)

    def source_start(self, index: int, total: int, source: str) -> None:
        self.write(f"Source {index}/{total}: {source}")

    def listing_page(self, source: str, current: int, total: int, url: str) -> None:
        if self.should_print(current, total):
            self.write(f"  {source} listing pages {current}/{total} {self.bar(current, total)} | {url}")

    def detail_urls_found(self, source: str, count: int) -> None:
        self.write(f"  {source} detail URLs found: {count}")

    def parse_progress(self, source: str, current: int, total: int, parsed: int, failed: int) -> None:
        if self.should_print(current, total):
            self.write(
                f"  {source} parsing {current}/{total} {self.bar(current, total)} "
                f"| parsed={parsed} failed={failed}"
            )

    def source_done(self, source: str, raw_count: int) -> None:
        self.write(f"  {source} done | raw_jobs={raw_count}")

    def validation_progress(self, current: int, total: int, valid: int, invalid: int) -> None:
        if self.should_print(current, total):
            self.write(
                f"Validation {current}/{total} {self.bar(current, total)} "
                f"| valid={valid} invalid={invalid}"
            )

    def dedupe_done(self, before: int, after: int) -> None:
        removed = before - after
        self.write(f"Deduplication complete | before={before} after={after} removed={removed}")

    def db_progress(self, current: int, total: int, inserted: int, skipped: int, failed: int, dry_run: bool) -> None:
        if self.should_print(current, total):
            action = "dry-run" if dry_run else "db-write"
            self.write(
                f"Database {action} {current}/{total} {self.bar(current, total)} "
                f"| inserted={inserted} skipped={skipped} failed={failed}"
            )

    def finish(self, stats: dict[str, int]) -> None:
        rendered = " ".join(f"{key}={value}" for key, value in stats.items())
        self.write("=" * 72)
        self.write(f"Finished scraper | {rendered}")
        self.write("=" * 72)
