from __future__ import annotations

import argparse
import os
from pathlib import Path

from .logging_config import configure_logging
from .pipeline import run


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Scrape and ingest Zimbabwe/remote job listings into the zimjobs SQLite database.")
    parser.add_argument("--db", default=os.getenv("JOBS_DB_PATH", "/data/jobs.db"), help="Path to SQLite jobs.db")
    parser.add_argument("--config", default="config/sources.json", help="Path to source config JSON")
    parser.add_argument("--table", default=os.getenv("JOBS_TABLE", "jobs"), help="Jobs table name")
    parser.add_argument("--dry-run", action="store_true", default=os.getenv("DRY_RUN", "0") == "1", help="Parse and validate but do not write")
    parser.add_argument("--log-level", default=os.getenv("LOG_LEVEL", "INFO"))
    return parser


def main() -> None:
    args = build_arg_parser().parse_args()
    configure_logging(args.log_level)
    config_path = Path(args.config)
    if not config_path.exists():
        # Allows `python -m zimjobs_scraper.cli` from inside the project root or from src layout installs.
        candidate = Path(__file__).resolve().parents[2] / args.config
        if candidate.exists():
            config_path = candidate
    stats = run(str(config_path), args.db, dry_run=args.dry_run, table_name=args.table)
    print(stats)


if __name__ == "__main__":
    main()
