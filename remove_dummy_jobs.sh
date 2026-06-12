#!/usr/bin/env bash
# remove_dummy_jobs.sh — safely remove seeded dummy jobs from the ZimJobs SQLite DB.
#
# Usage:
#   chmod +x remove_dummy_jobs.sh
#   ./remove_dummy_jobs.sh /data/jobs.db
#
# Dry run:
#   ./remove_dummy_jobs.sh /data/jobs.db --dry-run
#
# No backup:
#   ./remove_dummy_jobs.sh /data/jobs.db --no-backup
#
# Railway:
#   railway ssh
#   bash remove_dummy_jobs.sh /data/jobs.db
#
# This script removes only the obvious seeded demo jobs from the original app:
# - apply_url = https://example.org/apply
# - known dummy titles / dummy companies from the seed data
#
# It also deletes related saved_jobs rows if that table exists.

set -u
IFS=$'\n\t'

DB_PATH="${1:-}"
MODE="${2:-}"
MODE2="${3:-}"

DRY_RUN=0
MAKE_BACKUP=1

for arg in "$MODE" "$MODE2"; do
  case "$arg" in
    --dry-run) DRY_RUN=1 ;;
    --no-backup) MAKE_BACKUP=0 ;;
    "") ;;
    *)
      echo "Unknown option: $arg"
      echo "Usage: ./remove_dummy_jobs.sh /path/to/jobs.db [--dry-run] [--no-backup]"
      exit 1
      ;;
  esac
done

log() {
  printf '[%s] %s\n' "$(date -u '+%Y-%m-%d %H:%M:%S UTC')" "$*"
}

die() {
  log "ERROR: $*"
  exit 1
}

require_cmd() {
  command -v "$1" >/dev/null 2>&1 || die "Required command not found: $1"
}

if [ -z "$DB_PATH" ]; then
  die "Missing database path. Usage: ./remove_dummy_jobs.sh /path/to/jobs.db"
fi

require_cmd sqlite3
require_cmd cp
require_cmd date

if [ ! -f "$DB_PATH" ]; then
  die "Database file does not exist: $DB_PATH"
fi

if [ ! -r "$DB_PATH" ] || [ ! -w "$DB_PATH" ]; then
  die "Database file must be readable and writable: $DB_PATH"
fi

HAS_JOBS_TABLE="$(
  sqlite3 "$DB_PATH" "SELECT name FROM sqlite_master WHERE type='table' AND name='jobs';"
)"

if [ "$HAS_JOBS_TABLE" != "jobs" ]; then
  die "Database does not contain required table: jobs"
fi

log "Using database: $DB_PATH"

TOTAL_BEFORE="$(
  sqlite3 "$DB_PATH" "SELECT COUNT(*) FROM jobs;"
)"

MATCH_COUNT="$(
sqlite3 "$DB_PATH" <<'SQL'
SELECT COUNT(*)
FROM jobs
WHERE
  apply_url = 'https://example.org/apply'
  OR (
    title IN (
      'Programme Officer',
      'Registered General Nurse',
      'Customer Support Agent (Remote)',
      'Accounts Clerk',
      'Graduate Intern — Agriculture',
      'Virtual Assistant (Remote)',
      'Monitoring & Evaluation Officer',
      'Delivery Rider — Own Motorbike'
    )
    AND company IN (
      'Local Development NGO',
      'Private Hospital Group',
      'US SaaS Startup',
      'Retail Group',
      'Government Ministry',
      'UK Agency',
      'International NGO',
      'Food Delivery Service'
    )
  );
SQL
)"

log "Total jobs before cleanup: $TOTAL_BEFORE"
log "Dummy jobs matched: $MATCH_COUNT"

if [ "$MATCH_COUNT" = "0" ]; then
  log "No dummy jobs found. Nothing to delete."
  exit 0
fi

log "Matched dummy rows:"
sqlite3 -header -column "$DB_PATH" <<'SQL'
SELECT id, title, company, location, category, apply_url
FROM jobs
WHERE
  apply_url = 'https://example.org/apply'
  OR (
    title IN (
      'Programme Officer',
      'Registered General Nurse',
      'Customer Support Agent (Remote)',
      'Accounts Clerk',
      'Graduate Intern — Agriculture',
      'Virtual Assistant (Remote)',
      'Monitoring & Evaluation Officer',
      'Delivery Rider — Own Motorbike'
    )
    AND company IN (
      'Local Development NGO',
      'Private Hospital Group',
      'US SaaS Startup',
      'Retail Group',
      'Government Ministry',
      'UK Agency',
      'International NGO',
      'Food Delivery Service'
    )
  )
ORDER BY id;
SQL

if [ "$DRY_RUN" = "1" ]; then
  log "Dry run enabled. No rows deleted."
  exit 0
fi

if [ "$MAKE_BACKUP" = "1" ]; then
  BACKUP_PATH="${DB_PATH}.before-dummy-cleanup.$(date -u '+%Y%m%d%H%M%S').bak"
  cp "$DB_PATH" "$BACKUP_PATH" || die "Failed to create backup at $BACKUP_PATH"
  log "Backup created: $BACKUP_PATH"
fi

HAS_SAVED_JOBS_TABLE="$(
  sqlite3 "$DB_PATH" "SELECT name FROM sqlite_master WHERE type='table' AND name='saved_jobs';"
)"

if [ "$HAS_SAVED_JOBS_TABLE" = "saved_jobs" ]; then
  log "saved_jobs table found. Related saved job rows will be removed first."
else
  log "saved_jobs table not found. Skipping related saved job cleanup."
fi

if [ "$HAS_SAVED_JOBS_TABLE" = "saved_jobs" ]; then
sqlite3 "$DB_PATH" <<'SQL'
PRAGMA foreign_keys = OFF;
BEGIN IMMEDIATE;

CREATE TEMP TABLE dummy_job_ids AS
SELECT id
FROM jobs
WHERE
  apply_url = 'https://example.org/apply'
  OR (
    title IN (
      'Programme Officer',
      'Registered General Nurse',
      'Customer Support Agent (Remote)',
      'Accounts Clerk',
      'Graduate Intern — Agriculture',
      'Virtual Assistant (Remote)',
      'Monitoring & Evaluation Officer',
      'Delivery Rider — Own Motorbike'
    )
    AND company IN (
      'Local Development NGO',
      'Private Hospital Group',
      'US SaaS Startup',
      'Retail Group',
      'Government Ministry',
      'UK Agency',
      'International NGO',
      'Food Delivery Service'
    )
  );

DELETE FROM saved_jobs
WHERE job_id IN (SELECT id FROM dummy_job_ids);

DELETE FROM jobs
WHERE id IN (SELECT id FROM dummy_job_ids);

DROP TABLE dummy_job_ids;

COMMIT;
SQL
else
sqlite3 "$DB_PATH" <<'SQL'
PRAGMA foreign_keys = OFF;
BEGIN IMMEDIATE;

DELETE FROM jobs
WHERE
  apply_url = 'https://example.org/apply'
  OR (
    title IN (
      'Programme Officer',
      'Registered General Nurse',
      'Customer Support Agent (Remote)',
      'Accounts Clerk',
      'Graduate Intern — Agriculture',
      'Virtual Assistant (Remote)',
      'Monitoring & Evaluation Officer',
      'Delivery Rider — Own Motorbike'
    )
    AND company IN (
      'Local Development NGO',
      'Private Hospital Group',
      'US SaaS Startup',
      'Retail Group',
      'Government Ministry',
      'UK Agency',
      'International NGO',
      'Food Delivery Service'
    )
  );

COMMIT;
SQL
fi

HAS_FTS="$(
  sqlite3 "$DB_PATH" "SELECT name FROM sqlite_master WHERE name='jobs_fts' LIMIT 1;"
)"

if [ "$HAS_FTS" = "jobs_fts" ]; then
  log "Rebuilding jobs_fts index..."
  sqlite3 "$DB_PATH" "INSERT INTO jobs_fts(jobs_fts) VALUES('rebuild');" \
    || log "WARN: Could not rebuild jobs_fts index. Continuing."
fi

TOTAL_AFTER="$(
  sqlite3 "$DB_PATH" "SELECT COUNT(*) FROM jobs;"
)"

REMAINING_DUMMY="$(
sqlite3 "$DB_PATH" <<'SQL'
SELECT COUNT(*)
FROM jobs
WHERE
  apply_url = 'https://example.org/apply'
  OR company IN (
    'Local Development NGO',
    'Private Hospital Group',
    'US SaaS Startup',
    'Retail Group',
    'Government Ministry',
    'UK Agency',
    'International NGO',
    'Food Delivery Service'
  );
SQL
)"

log "Total jobs after cleanup: $TOTAL_AFTER"
log "Remaining dummy-like rows: $REMAINING_DUMMY"

if [ "$REMAINING_DUMMY" != "0" ]; then
  log "WARN: Some dummy-like rows may remain. Review them manually:"
  sqlite3 -header -column "$DB_PATH" <<'SQL'
SELECT id, title, company, location, category, apply_url
FROM jobs
WHERE
  apply_url = 'https://example.org/apply'
  OR company IN (
    'Local Development NGO',
    'Private Hospital Group',
    'US SaaS Startup',
    'Retail Group',
    'Government Ministry',
    'UK Agency',
    'International NGO',
    'Food Delivery Service'
  )
ORDER BY id;
SQL
fi

log "Dummy job cleanup complete."
exit 0
