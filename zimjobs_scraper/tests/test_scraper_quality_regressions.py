from dataclasses import replace
from datetime import date
from pathlib import Path

import sys

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from job_schema import build_job_posting_json_ld
from zimjobs_scraper.db import SQLiteJobRepository
from zimjobs_scraper.mapper import map_raw_job
from zimjobs_scraper.models import RawJob
from zimjobs_scraper.normalization import normalize_job_text
from zimjobs_scraper.parsers import GenericParser, SourceConfig
from zimjobs_scraper.pipeline import ScrapePipeline
from zimjobs_scraper.validators import JobValidator


class FakeHttp:
    def __init__(self, pages):
        self.pages = pages

    def get(self, url):
        return self.pages.get(url)


def test_normalize_job_text_removes_gaps_noise_duplicates_and_keeps_bullets():
    messy = """
    Latest Jobs


       Finance Officer
       Finance Officer

       -   Manage grants, budgets, invoices, and donor financial reports.
       *   Prepare monthly reconciliations for programme teams.



       Category: NGO & Development
       Search jobs
    """

    cleaned = normalize_job_text(messy)

    assert "\n\n\n" not in cleaned
    assert "Latest Jobs" not in cleaned
    assert "Category:" not in cleaned
    assert cleaned.count("Finance Officer") == 1
    assert "• Manage grants" in cleaned
    assert "• Prepare monthly" in cleaned


def test_generic_parser_extracts_independent_cards_without_merging_page_text():
    cfg = SourceConfig(name="cards", type="generic", start_urls=[], default_location="Zimbabwe")
    parser = GenericParser(cfg)
    html = """
    <html><body>
      <h1>Latest Jobs</h1>
      <article class="job-card" data-job-id="fin-1">
        <h2>Finance Officer</h2>
        <p class="company">Example NGO</p>
        <p class="location">Harare</p>
        <p>Manage grants, invoices, donor reports, reconciliations, and finance controls for a Zimbabwe programme.</p>
      </article>
      <article class="job-card" data-job-id="ops-1">
        <h2>Operations Coordinator</h2>
        <p class="company">Example Logistics</p>
        <p class="location">Bulawayo</p>
        <p>Coordinate fleet scheduling, procurement follow-up, stock controls, and branch operations for regional teams.</p>
      </article>
    </body></html>
    """

    jobs = parser.parse_listing_payload(html, "https://example.org/jobs")

    assert [job.title for job in jobs] == ["Finance Officer", "Operations Coordinator"]
    assert jobs[0].external_id == "fin-1"
    assert jobs[1].external_id == "ops-1"
    assert "Operations Coordinator" not in jobs[0].summary
    assert "Finance Officer" not in jobs[1].summary
    assert parser.list_job_urls(html, "https://example.org/jobs") == []


def test_pipeline_saves_two_listing_cards_as_two_jobs(tmp_path):
    listing_url = "https://example.org/jobs"
    cfg = SourceConfig(
        name="cards",
        type="generic",
        start_urls=[listing_url],
        default_location="Zimbabwe",
        default_category="Private Sector",
        skip_expired=False,
    )
    html = """
    <html><body>
      <h1>Latest Jobs</h1>
      <article class="job-card" data-job-id="fin-1">
        <h2>Finance Officer</h2>
        <p class="company">Example NGO</p>
        <p class="location">Harare</p>
        <p>Manage grants, budgets, donor compliance, reconciliations, procurement support, and monthly reporting for a large programme.</p>
      </article>
      <article class="job-card" data-job-id="ops-1">
        <h2>Operations Coordinator</h2>
        <p class="company">Example Logistics</p>
        <p class="location">Bulawayo</p>
        <p>Coordinate fleet scheduling, stock controls, supplier communication, facility administration, and weekly operations reporting.</p>
      </article>
    </body></html>
    """

    jobs = ScrapePipeline([cfg], http=FakeHttp({listing_url: html})).collect()
    repo = SQLiteJobRepository(str(tmp_path / "jobs.db"), auto_add_optional_columns=True)
    try:
        stats = repo.insert_many(jobs)
        rows = repo.conn.execute("SELECT title, external_job_id FROM jobs ORDER BY title").fetchall()
    finally:
        repo.close()

    assert stats["inserted"] == 2
    assert [row["title"] for row in rows] == ["Finance Officer", "Operations Coordinator"]
    assert {row["external_job_id"] for row in rows} == {"fin-1", "ops-1"}


def test_validator_rejects_accidental_multi_job_merge():
    cfg = SourceConfig(name="bad", type="generic", start_urls=[], default_location="Zimbabwe", skip_expired=False)
    raw = RawJob(
        source_name="bad",
        source_url="https://example.org/jobs",
        title="Finance Officer",
        company="Example NGO",
        location="Harare",
        summary="""
        Job Title: Finance Officer
        Manage donor finance, reconciliations, controls, and monthly financial reports.
        Job Title: Operations Coordinator
        Coordinate procurement, fleet scheduling, and operations reports.
        Job Title: HR Assistant
        Maintain employee records, recruitment files, and onboarding documentation.
        """,
        apply_url="https://example.org/jobs",
    )

    result = JobValidator(skip_expired=False).validate(map_raw_job(raw, cfg))

    assert not result.ok
    assert "probable_merged_listing" in result.reasons


def test_saved_job_is_schema_compatible_and_duplicate_external_id_is_skipped(tmp_path):
    cfg = SourceConfig(name="cards", type="generic", start_urls=[], default_location="Zimbabwe", skip_expired=False)
    raw = RawJob(
        source_name="cards",
        source_url="https://example.org/jobs",
        title="Finance Officer",
        company="Example NGO",
        location="Harare",
        summary="Manage grants, budgets, donor compliance, reconciliations, procurement support, and monthly reporting for a Zimbabwe programme.",
        apply_url="https://example.org/jobs",
        expires_at="2099-12-31",
        employment_type="Full-time",
        external_id="fin-1",
    )
    duplicate = replace(raw)
    jobs = [map_raw_job(raw, cfg), map_raw_job(duplicate, cfg)]

    repo = SQLiteJobRepository(str(tmp_path / "jobs.db"), auto_add_optional_columns=True)
    try:
        stats = repo.insert_many(jobs)
        row = repo.conn.execute("SELECT * FROM jobs").fetchone()
    finally:
        repo.close()

    schema = build_job_posting_json_ld(
        row,
        {"job_url": "https://zimjobs.example/job/1/finance-officer", "default_country_code": "ZW"},
        today=date(2026, 6, 17),
    )

    assert stats["inserted"] == 1
    assert stats["skipped"] == 1
    assert schema["@type"] == "JobPosting"
    assert schema["title"] == "Finance Officer"
    assert schema["employmentType"] == "FULL_TIME"
    assert schema["jobLocation"]["address"]["addressCountry"] == "ZW"
