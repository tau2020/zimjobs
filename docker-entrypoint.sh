#!/bin/sh
set -eu

DB_PATH="${DB_PATH:-/data/jobs.db}"
ENABLE_SCRAPER_CRON="${ENABLE_SCRAPER_CRON:-1}"
SCRAPER_CRON_SCHEDULE="${SCRAPER_CRON_SCHEDULE:-15 6 * * *}"

mkdir -p "$(dirname "$DB_PATH")"

if [ "$ENABLE_SCRAPER_CRON" = "1" ]; then
  {
    printf '%s\n' 'SHELL=/bin/sh'
    printf '%s\n' 'PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin'
    printf '%s /app/run_daily_scraper.sh >> /data/scraper.log 2>&1\n' "$SCRAPER_CRON_SCHEDULE"
  } > /etc/crontabs/root

  crond -b -l 8
  echo "scraper cron enabled: $SCRAPER_CRON_SCHEDULE UTC"
else
  echo "scraper cron disabled"
fi

exec "$@"
