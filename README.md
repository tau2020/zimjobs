# ZimJobs Hub

ZimJobs Hub is a Flask and SQLite job board platform designed for deployment on Railway with automated scraping of public Zimbabwean job listings.

## Railway Database Update Setup

To ensure the SQLite database is kept persistent and updated daily, configure your Railway services as follows:

1. **Add a Railway Volume**:
   * Add a Railway Volume to your Flask web service.
   * Set the mount path of the volume to `/data`.

2. **Configure Environment Variables**:
   * In your main Flask service settings, add the environment variable:
     `DB_PATH=/data/jobs.db`

3. **Deploy the Flask App**:
   * Deploy the Flask application once. During startup, the application will automatically create the database file and initialize the `jobs` and `users` tables if they do not already exist.

4. **Verify the Database**:
   * Open the Railway SSH or shell console for the Flask service and verify the database exists and has been initialized:
     ```bash
     ls -lah /data
     sqlite3 /data/jobs.db ".tables"
     sqlite3 /data/jobs.db "SELECT COUNT(*) FROM jobs;"
     ```

5. **Daily scraper cron**:
   * The Docker image starts Alpine `crond` beside Gunicorn.
   * The live Flask app and scraper both use `DB_PATH=/data/jobs.db`, so scraped jobs are written to the same persistent SQLite database that the website reads.
   * The default schedule is:
     ```text
     15 6 * * *
     ```
     This runs daily at 06:15 UTC, which is 08:15 in Africa/Harare.
   * To change the schedule, set `SCRAPER_CRON_SCHEDULE` on Railway.
   * To disable the cron without changing the image, set `ENABLE_SCRAPER_CRON=0`.
   * Cron output is appended to `/data/scraper.log`.

## Transactional Email With Resend

The web app can send transactional email through Resend for account welcomes,
email-alert confirmations, and admin job-published notifications.

Configure these environment variables on Railway or in your local ignored
secrets file:

```bash
RESEND_API_KEY=re_xxxxxxxxxxxxxxxxxxxxx
RESEND_FROM_EMAIL="ZimJobs Hub <jobs@yourdomain.com>"
RESEND_REPLY_TO="support@yourdomain.com"
ADMIN_EMAIL="admin@yourdomain.com"
```

`RESEND_API_KEY` is required to send. If it is missing, the app logs
`email_skipped` and continues without blocking the user action. Use a verified
sender/domain in Resend before production sending. Set
`TRANSACTIONAL_EMAILS_ENABLED=0` to temporarily disable email sends.

Email alert signups are stored locally in SQLite with frequency, active status,
unsubscribe token, last-send metadata, and delivery-failure fields. Existing
rows are migrated additively on startup. The current implementation does not
run weekly digest campaigns yet; see `docs/tooling-decision-note.md` before
adding Listmonk or another campaign service.

Unsubscribe links use:

```text
/alerts/email/unsubscribe/<token>
```

Search uses SQLite FTS5 with a LIKE fallback if the FTS table/query fails.
Meilisearch or Typesense should be added only after measured search relevance
or latency issues justify a separate service.

---

## Safe Manual Update Command

You can run the scraper manually at any time to import new job listings:

```bash
cd /app/zimjobs_scraper
PYTHONPATH=src DRY_RUN=0 AUTO_ADD_OPTIONAL_COLUMNS=1 PROGRESS=1 \
python run_scraper.py --db "${DB_PATH:-/data/jobs.db}" --config config/sources.json
```

If you are inside Railway SSH, verify you are writing to the same DB the web app uses:

```bash
echo "DB_PATH=${DB_PATH:-/data/jobs.db}"
ls -lah /data
sqlite3 /data/jobs.db "SELECT COUNT(*) FROM jobs;"
cd /app/zimjobs_scraper
PYTHONPATH=src DRY_RUN=0 AUTO_ADD_OPTIONAL_COLUMNS=1 PROGRESS=1 \
python run_scraper.py --db "${DB_PATH:-/data/jobs.db}" --config config/sources.json
sqlite3 /data/jobs.db "SELECT id, title, company, created_at FROM jobs ORDER BY id DESC LIMIT 10;"
```

---

## Verification Command

To verify that the database has been successfully updated with the latest scraped jobs, run:

```bash
sqlite3 /data/jobs.db "SELECT id, title, company, category, created_at FROM jobs ORDER BY id DESC LIMIT 10;"
```
