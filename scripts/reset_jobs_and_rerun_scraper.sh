#!/bin/sh
set -eu

usage() {
  cat <<'EOF'
Usage:
  scripts/reset_jobs_and_rerun_scraper.sh --yes [options]

Deletes all rows from jobs, removes saved job references, rebuilds FTS, then
runs the scraper against the same SQLite database.

Options:
  --yes              Required. Confirms destructive reset.
  --db PATH          SQLite database path. Default: $DB_PATH or /data/jobs.db.
  --config PATH      Scraper config path. Default: $SCRAPER_CONFIG or app config.
  --backup-dir DIR   Directory for timestamped backup. Default: DB directory.
  --no-backup        Skip backup. Not recommended.
  --reset-only       Delete/rebuild only; do not run scraper.
  -h, --help         Show this help.

Environment:
  DB_PATH
  SCRAPER_CONFIG
  SCRAPER_LOCK_DIR
  SCRAPER_DIR
  PYTHON_BIN
  PYTHONPATH
  AUTO_ADD_OPTIONAL_COLUMNS
  MAX_DETAIL_PER_SOURCE
  PROGRESS
  ENABLE_BAD_DESCRIPTION_CLEANUP
EOF
}

APP_ROOT="$(CDPATH= cd "$(dirname "$0")/.." && pwd)"
DB_PATH="${DB_PATH:-/data/jobs.db}"
if [ -n "${SCRAPER_CONFIG:-}" ]; then
  SCRAPER_CONFIG="$SCRAPER_CONFIG"
elif [ -f "$APP_ROOT/zimjobs_scraper/config/sources.json" ]; then
  SCRAPER_CONFIG="$APP_ROOT/zimjobs_scraper/config/sources.json"
else
  SCRAPER_CONFIG="/app/zimjobs_scraper/config/sources.json"
fi
if [ -n "${SCRAPER_DIR:-}" ]; then
  SCRAPER_DIR="$SCRAPER_DIR"
elif [ -f "$APP_ROOT/zimjobs_scraper/run_scraper.py" ]; then
  SCRAPER_DIR="$APP_ROOT/zimjobs_scraper"
else
  SCRAPER_DIR="/app/zimjobs_scraper"
fi
if [ -n "${PYTHON_BIN:-}" ]; then
  PYTHON_BIN="$PYTHON_BIN"
elif [ -x "$APP_ROOT/venv/bin/python" ]; then
  PYTHON_BIN="$APP_ROOT/venv/bin/python"
elif command -v python3 >/dev/null 2>&1; then
  PYTHON_BIN="python3"
else
  PYTHON_BIN="python"
fi
BACKUP_DIR=""
CONFIRMED=0
SKIP_BACKUP=0
RESET_ONLY=0

while [ "$#" -gt 0 ]; do
  case "$1" in
    --yes)
      CONFIRMED=1
      shift
      ;;
    --db)
      DB_PATH="${2:?--db requires a path}"
      shift 2
      ;;
    --config)
      SCRAPER_CONFIG="${2:?--config requires a path}"
      shift 2
      ;;
    --backup-dir)
      BACKUP_DIR="${2:?--backup-dir requires a directory}"
      shift 2
      ;;
    --no-backup)
      SKIP_BACKUP=1
      shift
      ;;
    --reset-only)
      RESET_ONLY=1
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "Unknown argument: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
done

if [ "$CONFIRMED" != "1" ]; then
  echo "Refusing to delete all jobs without --yes." >&2
  usage >&2
  exit 2
fi

if [ ! -f "$DB_PATH" ]; then
  echo "Database not found: $DB_PATH" >&2
  exit 1
fi

if [ "$RESET_ONLY" != "1" ] && [ ! -f "$SCRAPER_CONFIG" ]; then
  echo "Scraper config not found: $SCRAPER_CONFIG" >&2
  exit 1
fi

if [ "$RESET_ONLY" != "1" ] && [ ! -f "$SCRAPER_DIR/run_scraper.py" ]; then
  echo "Scraper runner not found: $SCRAPER_DIR/run_scraper.py" >&2
  exit 1
fi

LOCK_DIR="${SCRAPER_LOCK_DIR:-/tmp/zimjobs_scraper.lock}"
if ! mkdir "$LOCK_DIR" 2>/dev/null; then
  echo "$(date -u +'%Y-%m-%dT%H:%M:%SZ') scraper/reset already running; skipping" >&2
  exit 1
fi

cleanup() {
  rmdir "$LOCK_DIR" 2>/dev/null || true
}
trap cleanup EXIT INT TERM

timestamp="$(date -u +'%Y%m%dT%H%M%SZ')"
db_dir="$(dirname "$DB_PATH")"
BACKUP_DIR="${BACKUP_DIR:-$db_dir}"

echo "$(date -u +'%Y-%m-%dT%H:%M:%SZ') reset starting db=$DB_PATH config=$SCRAPER_CONFIG"

jobs_before="$(sqlite3 "$DB_PATH" "SELECT COUNT(*) FROM jobs;")"
echo "jobs_before=$jobs_before"

if [ "$SKIP_BACKUP" != "1" ]; then
  mkdir -p "$BACKUP_DIR"
  backup_path="$BACKUP_DIR/jobs-reset-$timestamp.db"
  sqlite3 "$DB_PATH" ".backup '$backup_path'"
  echo "backup_created=$backup_path"
else
  echo "backup_skipped=1"
fi

saved_exists="$(sqlite3 "$DB_PATH" "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='saved_jobs';")"
fts_exists="$(sqlite3 "$DB_PATH" "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='jobs_fts';")"

reset_sql="BEGIN IMMEDIATE;
DELETE FROM jobs;
"
if [ "$saved_exists" = "1" ]; then
  reset_sql="BEGIN IMMEDIATE;
DELETE FROM saved_jobs;
DELETE FROM jobs;
"
fi
if [ "$fts_exists" = "1" ]; then
  reset_sql="${reset_sql}INSERT INTO jobs_fts(jobs_fts) VALUES('rebuild');
"
fi
reset_sql="${reset_sql}COMMIT;"

printf '%s\n' "$reset_sql" | sqlite3 "$DB_PATH"

jobs_after_reset="$(sqlite3 "$DB_PATH" "SELECT COUNT(*) FROM jobs;")"
echo "jobs_after_reset=$jobs_after_reset"

if [ "$RESET_ONLY" = "1" ]; then
  echo "$(date -u +'%Y-%m-%dT%H:%M:%SZ') reset finished without scraper"
  exit 0
fi

SCRAPER_DIR="$(CDPATH= cd "$SCRAPER_DIR" && pwd)"
export PYTHONPATH="${PYTHONPATH:-$SCRAPER_DIR/src}"
export DRY_RUN="${DRY_RUN:-0}"
export AUTO_ADD_OPTIONAL_COLUMNS="${AUTO_ADD_OPTIONAL_COLUMNS:-1}"
export MAX_DETAIL_PER_SOURCE="${MAX_DETAIL_PER_SOURCE:-40}"
export PROGRESS="${PROGRESS:-1}"
export ENABLE_BAD_DESCRIPTION_CLEANUP="${ENABLE_BAD_DESCRIPTION_CLEANUP:-1}"

echo "$(date -u +'%Y-%m-%dT%H:%M:%SZ') scraper starting db=$DB_PATH config=$SCRAPER_CONFIG"
cd "$SCRAPER_DIR"
"$PYTHON_BIN" run_scraper.py --db "$DB_PATH" --config "$SCRAPER_CONFIG"

if [ "$ENABLE_BAD_DESCRIPTION_CLEANUP" = "1" ]; then
  echo "$(date -u +'%Y-%m-%dT%H:%M:%SZ') cleaner starting db=$DB_PATH bad_descriptions=1"
  "$PYTHON_BIN" -m zimjobs_scraper.clean_db --db "$DB_PATH" --yes --bad-descriptions
else
  echo "$(date -u +'%Y-%m-%dT%H:%M:%SZ') cleaner starting db=$DB_PATH bad_descriptions=0"
  "$PYTHON_BIN" -m zimjobs_scraper.clean_db --db "$DB_PATH" --yes
fi

jobs_after_scrape="$(sqlite3 "$DB_PATH" "SELECT COUNT(*) FROM jobs;")"
echo "jobs_after_scrape=$jobs_after_scrape"
echo "$(date -u +'%Y-%m-%dT%H:%M:%SZ') reset and scraper finished"
