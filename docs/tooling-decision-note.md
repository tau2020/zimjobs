# Tooling Decision Note: Alerts, Search, and Scraping

Date: 2026-06-17

## Current Implementation

- Job alerts: local `email_alerts` SQLite table, signup form, CSRF protection, rate limiting, and Resend confirmation email.
- Weekly digests: no digest generation or campaign scheduler currently exists.
- Email delivery: Resend is already used for transactional email. Missing API keys cause logged skips rather than blocking user flows.
- Search/discovery: SQLite FTS5 powers keyword search, with SQL filters for category, location, employment type, remote status, seniority, deadlines, and sort order.
- Scraping/importing: a custom Python pipeline uses `requests`, BeautifulSoup parsers, source-specific mappers, validation, in-memory dedupe, SQLite duplicate checks, expired-job cleanup, and FTS rebuilds.
- Deployment: one Railway web service with a persistent SQLite volume and an optional cron process inside the same container.

## Decision

Do not add Listmonk, Meilisearch, Typesense, Scrapy, or Playwright yet.

Resend stays in place for transactional email because it is already integrated and low operational overhead. Listmonk should be deferred until weekly digests or segmented campaigns exist and there is enough subscriber volume to justify running another service.

SQLite FTS5 stays in place for search. It matches the current single-node SQLite deployment, avoids another service, supports the existing filters, and is simpler to roll back. Meilisearch or Typesense can be reconsidered if query latency, typo tolerance, faceting, or result relevance become measurable problems at larger job volume.

The custom scraper stays in place. It already has source-specific parsers, validation, dedupe, expiry handling, and tests. Scrapy can be reconsidered if crawl orchestration becomes hard to maintain across many sources. Playwright should remain a targeted fallback only for JavaScript-heavy sources that cannot be parsed from HTML/API/RSS.

## Changes Made

- Added alert preference fields to `email_alerts`: `frequency`, `active`, `unsubscribe_token`, `last_sent_at`, `last_error`, `delivery_failures`, and `unsubscribed_at`.
- Added tokenized unsubscribe route for email alerts.
- Kept alert signup idempotent and made resubscribe reactivate an existing alert.
- Added FTS failure fallback to a LIKE-based search query while preserving existing filters.
- Added tests for alert preferences, unsubscribe, and search fallback.

## Deployment

No new services, migrations, Docker changes, or environment variables are required.

Deploy the app normally. On startup, `init_db()` applies additive SQLite columns and backfills unsubscribe tokens for existing alert rows.

## Rollback

Rollback is the normal application rollback: deploy the previous app version. The added SQLite columns are backward-compatible and can remain unused. If cleanup is ever required, remove the columns with an explicit SQLite table rebuild during a maintenance window.

## Deferred Tool Triggers

Adopt Listmonk only when digest/campaign operations need segmentation, list management, campaign analytics, or non-transactional sending workflows that exceed simple Resend emails.

Adopt Meilisearch or Typesense only after measuring search latency/relevance issues that SQLite FTS cannot address with indexes or query tuning.

Adopt Scrapy only when the scraper needs queueing, crawl scheduling, middleware, item pipelines, or broader crawler observability across many sites.

Adopt Playwright only for specific JavaScript-rendered sources, behind a per-source setting, with strict page limits and timeouts.
