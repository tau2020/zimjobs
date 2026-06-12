# ZimJobs Scraper

Production-minded Python scraper and mapping pipeline for the existing `zimjobs.online` Flask/SQLite jobs table.

The current core table supported is:

```sql
id INTEGER PRIMARY KEY,
title TEXT,
company TEXT,
location TEXT,
category TEXT,
summary TEXT,
apply_url TEXT,
featured INTEGER DEFAULT 0,
created_at TEXT DEFAULT (datetime('now'))
```

The scraper keeps this legacy schema safe by inserting only columns that exist. If you later add richer columns like `source_url`, `expires_at`, `job_type`, `salary_range`, `content_hash`, etc., the same pipeline will automatically populate them when present. Set `AUTO_ADD_OPTIONAL_COLUMNS=1` only if you want the scraper to add optional metadata columns itself.

## Sources included

- `applynow_zimbabwe`: ApplyNOW Zimbabwe and Remote pages.
- `impactpool_zimbabwe`: Impactpool Zimbabwe listing and specific NGO job detail pages.
- `somewhere_remote`: Somewhere candidate/recruiting pages. The public board is dynamic/RecruitCRM-backed, so the parser extracts visible public links where available and skips generic landing pages.

## Install

```bash
cd zimjobs_scraper
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## Dry run locally

```bash
PYTHONPATH=src DRY_RUN=1 python run_scraper.py --db ./jobs.db --config config/sources.json --dry-run
```

## Run against Railway SQLite DB

```bash
cd /app
python3 -m pip install -r requirements.txt
PYTHONPATH=src DRY_RUN=0 MAX_PAGES=2 MAX_DETAIL_PER_SOURCE=40 python run_scraper.py --db /data/jobs.db --config config/sources.json
```

## Recommended cron

Daily is enough for these sources:

```cron
15 6 * * * cd /app && PYTHONPATH=src DRY_RUN=0 MAX_PAGES=2 MAX_DETAIL_PER_SOURCE=40 python run_scraper.py --db /data/jobs.db --config config/sources.json >> /data/scraper.log 2>&1
```

## How to add a new source

1. Add a new object to `config/sources.json`.
2. Use `type: "generic"` first.
3. Add `start_urls`, `default_location`, `default_category`, and `allowed_locations`.
4. Run a dry run and inspect logs.
5. If parsing is poor, create a parser class in `src/zimjobs_scraper/parsers.py`, register it in `PARSER_REGISTRY`, and add tests using a saved HTML fixture.

## Data quality behavior

- Respects `robots.txt` using Python's robot parser.
- Uses a polite user agent and request delays.
- Does not bypass login walls, CAPTCHA, paywalls, or anti-bot systems.
- Cleans HTML while preserving useful bullet/heading structure.
- Normalizes category, location, remote status, employment type, salary, dates, and deadlines where possible.
- Skips expired jobs when a deadline is known and `skip_expired` is true.
- Deduplicates by canonical `apply_url`, title/company/location key, `content_hash` when present, and description similarity.
- Preserves source traceability in optional columns when available, and appends a source line into `summary` for the legacy schema.

## Testing

```bash
PYTHONPATH=src pytest -q
```
