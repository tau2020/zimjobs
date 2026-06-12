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

5. **Create a separate Railway Cron service**:
   * Create a new service in your Railway project choosing the same repository (`tau2020/zimjobs`).
   * Set the **Start Command** for this cron service to:
     ```bash
     bash scrape_jobs.sh /data/jobs.db
     ```
   * Set the **Schedule** to run daily (using UTC timezone):
     ```text
     0 6 * * *
     ```
   * **Note**: The Railway Cron service will run on the specified cron schedule (which is in UTC), execute the scraper, and then exit. The main Flask web service should not run the scraper on startup.

---

## Safe Manual Update Command

You can run the scraper manually at any time to import new job listings:

```bash
bash scrape_jobs.sh /data/jobs.db
```

---

## Verification Command

To verify that the database has been successfully updated with the latest scraped jobs, run:

```bash
sqlite3 /data/jobs.db "SELECT id, title, company, category, created_at FROM jobs ORDER BY id DESC LIMIT 10;"
```
