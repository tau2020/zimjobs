#!/bin/sh
set -eu

DB_PATH="${DB_PATH:-/data/jobs.db}"
SCRAPER_CONFIG="${SCRAPER_CONFIG:-/app/zimjobs_scraper/config/sources.json}"
LOCK_DIR="${SCRAPER_LOCK_DIR:-/tmp/zimjobs_scraper.lock}"

mkdir -p "$(dirname "$DB_PATH")"

if ! mkdir "$LOCK_DIR" 2>/dev/null; then
  echo "$(date -u +'%Y-%m-%dT%H:%M:%SZ') scraper already running; skipping"
  exit 0
fi

cleanup() {
  rmdir "$LOCK_DIR" 2>/dev/null || true
}
trap cleanup EXIT INT TERM

echo "$(date -u +'%Y-%m-%dT%H:%M:%SZ') scraper starting db=$DB_PATH config=$SCRAPER_CONFIG"

cd /app/zimjobs_scraper
export PYTHONPATH="${PYTHONPATH:-/app/zimjobs_scraper/src}"
export DRY_RUN="${DRY_RUN:-0}"
export AUTO_ADD_OPTIONAL_COLUMNS="${AUTO_ADD_OPTIONAL_COLUMNS:-1}"
export MAX_DETAIL_PER_SOURCE="${MAX_DETAIL_PER_SOURCE:-40}"
export PROGRESS="${PROGRESS:-1}"
export ENABLE_BAD_DESCRIPTION_CLEANUP="${ENABLE_BAD_DESCRIPTION_CLEANUP:-1}"

echo "$(date -u +'%Y-%m-%dT%H:%M:%SZ') cleaner starting phase=pre db=$DB_PATH"
python -m zimjobs_scraper.clean_db --db "$DB_PATH" --yes

python run_scraper.py --db "$DB_PATH" --config "$SCRAPER_CONFIG"

if [ "$ENABLE_BAD_DESCRIPTION_CLEANUP" = "1" ]; then
  echo "$(date -u +'%Y-%m-%dT%H:%M:%SZ') cleaner starting phase=post db=$DB_PATH bad_descriptions=1"
  python -m zimjobs_scraper.clean_db --db "$DB_PATH" --yes --bad-descriptions
else
  echo "$(date -u +'%Y-%m-%dT%H:%M:%SZ') cleaner starting phase=post db=$DB_PATH bad_descriptions=0"
  python -m zimjobs_scraper.clean_db --db "$DB_PATH" --yes
fi

echo "$(date -u +'%Y-%m-%dT%H:%M:%SZ') scraper finished"
