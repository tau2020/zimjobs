-- Preview jobs with scraped spam/boilerplate descriptions before deleting.
-- Usage:
--   sqlite3 /data/jobs.db < scripts/preview_bad_description_jobs.sql

.headers on
.mode column

DROP TABLE IF EXISTS temp.bad_description_jobs;

CREATE TEMP TABLE bad_description_jobs AS
WITH job_text AS (
    SELECT
        id,
        title,
        company,
        location,
        category,
        COALESCE(job_description, '') || CHAR(10) ||
        COALESCE(summary, '') || CHAR(10) ||
        COALESCE(requirements, '') || CHAR(10) ||
        COALESCE(apply_url, '') AS haystack,
        LOWER(
            COALESCE(job_description, '') || CHAR(10) ||
            COALESCE(summary, '') || CHAR(10) ||
            COALESCE(requirements, '') || CHAR(10) ||
            COALESCE(apply_url, '')
        ) AS haystack_lower
    FROM jobs
)
SELECT
    id,
    title,
    company,
    location,
    category,
    CASE
        WHEN INSTR(haystack, 'RMTUyLjU1LjE3Ny44Mw==') > 0 THEN 'spam verification tag'
        WHEN haystack_lower LIKE '%please mention the word%' THEN 'spam verification phrase'
        WHEN haystack_lower LIKE '%show you read the job post completely%' THEN 'spam verification phrase'
        WHEN haystack_lower LIKE '%beta feature to avoid spam applicants%' THEN 'spam applicant filter boilerplate'
        WHEN haystack_lower LIKE '%companies can search these words%' THEN 'spam applicant filter boilerplate'
        WHEN haystack_lower LIKE '%see this and similar jobs on linkedin%' THEN 'linkedin snippet boilerplate'
        WHEN haystack_lower LIKE '%remoteok.com/remote-jobs%' THEN 'remoteok job url'
        ELSE 'bad description marker'
    END AS reason,
    SUBSTR(REPLACE(REPLACE(haystack, CHAR(13), ' '), CHAR(10), ' '), 1, 260) AS sample
FROM job_text
WHERE
    INSTR(haystack, 'RMTUyLjU1LjE3Ny44Mw==') > 0
    OR haystack_lower LIKE '%please mention the word%'
    OR haystack_lower LIKE '%show you read the job post completely%'
    OR haystack_lower LIKE '%beta feature to avoid spam applicants%'
    OR haystack_lower LIKE '%companies can search these words%'
    OR haystack_lower LIKE '%see this and similar jobs on linkedin%'
    OR haystack_lower LIKE '%remoteok.com/remote-jobs%';

SELECT COUNT(*) AS jobs_matched FROM bad_description_jobs;

SELECT
    id,
    title,
    company,
    location,
    category,
    reason,
    sample
FROM bad_description_jobs
ORDER BY id DESC
LIMIT 200;
