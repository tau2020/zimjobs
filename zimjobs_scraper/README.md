# ZimJobs Scraper

Production-minded Python scraper and mapping pipeline for the existing `zimjobs.online` Flask/SQLite jobs table.

The core table supported is still the current legacy table:

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

The scraper inserts only columns that exist. If you later add richer columns like `source_url`, `expires_at`, `job_type`, `salary_range`, `content_hash`, etc., the pipeline will populate them when present. Set `AUTO_ADD_OPTIONAL_COLUMNS=1` only if you want the scraper to add optional metadata columns itself.

## What changed in this update

This version shifts the scraper from weak HTML-only sources toward a safer source mix:

- Official API/RSS sources for remote and NGO jobs.
- Better Zimbabwe local source coverage.
- A parser framework that can consume API/RSS payloads directly without fetching every detail page.
- Disabled-by-default partner/legal-review sources separated into `config/sources_partner_review.json`.
- Remote-location restriction filtering to avoid adding `US only`, `UK only`, `Europe only`, etc. jobs unless they clearly allow worldwide/Africa/EMEA candidates.
- ATS support for Greenhouse and Lever public job board APIs.
- More tests for RSS/API parsers.

## Enabled source types

The default `config/sources.json` includes:

### Official API / RSS sources

- `reliefweb_zimbabwe_api` — ReliefWeb jobs API for Zimbabwe NGO/development jobs.
- `weworkremotely_rss` — We Work Remotely public RSS feeds.
- `jobicy_remote_api` — Jobicy public remote jobs API.
- `remoteok_api` — Remote OK public JSON feed.

### Zimbabwe local / official pages

- `vacancymail_zimbabwe`
- `iharare_jobs`
- `psc_zimbabwe_erecruitment`
- `unjobs_zimbabwe`
- `zimplats_careers`
- `delta_corporation_vacancies`
- `msu_vacancies`
- `applynow_zimbabwe`

### Disabled by default

- `impactpool_zimbabwe` — useful, but better with legal review or partnership/API.
- `gitlab_greenhouse_remote` — example Greenhouse ATS source. Enable only after confirming remote-location rules.

Partner/API-first sources are listed separately in:

```text
config/sources_partner_review.json
```

## Source compliance rules

This scraper does **not** bypass CAPTCHAs, login walls, paywalls, anti-bot systems, or application flows.

Before live crawling any HTML source, review:

- `robots.txt`
- terms of service
- rate limits
- copyright/database rights
- attribution requirements
- whether the site is an original employer source or an aggregator

For API/RSS sources, keep attribution and direct links visible in your job listing page. Do not rewrite job ownership or send users through redirect links when direct links are required.

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

For first validation, use a smaller run:

```bash
PYTHONPATH=src \
DRY_RUN=1 \
MAX_PAGES=1 \
MAX_DETAIL_PER_SOURCE=10 \
PROGRESS=1 \
python run_scraper.py --db ./jobs.db --config config/sources.json --dry-run
```

## Run against Railway SQLite DB

```bash
cd /app
python3 -m pip install -r requirements.txt
PYTHONPATH=src \
DRY_RUN=0 \
MAX_PAGES=1 \
MAX_DETAIL_PER_SOURCE=40 \
PROGRESS=1 \
python run_scraper.py --db /data/jobs.db --config config/sources.json
```

## Recommended clean refresh on Railway

Back up first, then dry run, then live insert:

```bash
cp /data/jobs.db /data/jobs_backup_before_source_update_$(date +%Y%m%d_%H%M%S).db
sqlite3 /data/jobs.db "DELETE FROM jobs;"
sqlite3 /data/jobs.db "DELETE FROM sqlite_sequence WHERE name='jobs';"

PYTHONPATH=src PROGRESS=1 DRY_RUN=1 MAX_PAGES=1 MAX_DETAIL_PER_SOURCE=20 \
python run_scraper.py --db /data/jobs.db --config config/sources.json --dry-run

PYTHONPATH=src PROGRESS=1 DRY_RUN=0 MAX_PAGES=1 MAX_DETAIL_PER_SOURCE=40 \
python run_scraper.py --db /data/jobs.db --config config/sources.json
```

## Recommended cron

Daily is enough for local sources. API/RSS sources should not be over-polled.

```cron
15 6 * * * cd /app && PYTHONPATH=src DRY_RUN=0 MAX_PAGES=1 MAX_DETAIL_PER_SOURCE=40 PROGRESS=1 python run_scraper.py --db /data/jobs.db --config config/sources.json >> /data/scraper.log 2>&1
```

## Adding Greenhouse / Lever ATS sources

### Greenhouse

Use the public Job Board API URL pattern:

```text
https://boards-api.greenhouse.io/v1/boards/{board_token}/jobs?content=true
```

Example config:

```json
{
  "name": "example_greenhouse_remote",
  "type": "greenhouse_api",
  "enabled": true,
  "start_urls": ["https://boards-api.greenhouse.io/v1/boards/example/jobs?content=true"],
  "default_location": "Remote / Worldwide",
  "default_category": "Remote & International",
  "allowed_locations": ["Remote", "Worldwide", "EMEA", "Africa", "Global"],
  "skip_expired": true
}
```

### Lever

Use the public Postings API URL pattern:

```text
https://api.lever.co/v0/postings/{site}?mode=json
```

Example config:

```json
{
  "name": "example_lever_remote",
  "type": "lever_api",
  "enabled": true,
  "start_urls": ["https://api.lever.co/v0/postings/example?mode=json"],
  "default_location": "Remote / Worldwide",
  "default_category": "Remote & International",
  "allowed_locations": ["Remote", "Worldwide", "EMEA", "Africa", "Global"],
  "skip_expired": true
}
```

Always enable ATS sources one employer at a time and inspect dry-run output before live ingestion.

## Data quality behavior

- Respects `robots.txt` using Python's robot parser.
- Uses a polite user agent and request delays.
- Does not bypass login walls, CAPTCHA, paywalls, or anti-bot systems.
- Cleans HTML while preserving useful bullet/heading structure.
- Normalizes category, location, remote status, employment type, salary, dates, and deadlines where possible.
- Skips expired jobs when a deadline is known and `skip_expired` is true.
- Deduplicates by canonical `apply_url`, title/company/location key, `content_hash` when present, and description similarity.
- Preserves source traceability in optional columns when available, and appends a source line into `summary` for the legacy schema.
- Rejects generic landing-page and marketing-copy records.
- Rejects remote jobs that are clearly restricted to countries Zimbabwe-based candidates cannot normally apply from.

## Testing

```bash
PYTHONPATH=src pytest -q
```

Current test coverage includes:

- ApplyNOW detail parsing.
- Impactpool detail parsing fixture.
- RSS feed parsing.
- Jobicy API parsing.
- Remote OK API parsing.
- ReliefWeb API parsing.
- Mapping, validation, dedupe, and SQLite insertion.

## Progress view in Railway logs

Maximum visibility:

```bash
PYTHONPATH=src \
PROGRESS=1 \
PROGRESS_EVERY=1 \
DRY_RUN=1 \
MAX_PAGES=1 \
MAX_DETAIL_PER_SOURCE=30 \
python run_scraper.py \
  --db /data/jobs.db \
  --config config/sources.json \
  --dry-run
```

Disable progress output:

```bash
PYTHONPATH=src PROGRESS=0 python run_scraper.py \
  --db /data/jobs.db \
  --config config/sources.json \
  --no-progress
```
