import sqlite3
from pathlib import Path

from zimjobs_scraper.db import SQLiteJobRepository
from zimjobs_scraper.dedupe import dedupe_in_memory
from zimjobs_scraper.mapper import map_raw_job
from zimjobs_scraper.models import RawJob
from zimjobs_scraper.parsers import SourceConfig
from zimjobs_scraper.validators import JobValidator


def test_mapping_into_legacy_fields_preserves_source():
    cfg = SourceConfig(name="applynow", type="applynow", start_urls=[], default_location="Zimbabwe", default_category="Other")
    raw = RawJob(
        source_name="applynow",
        source_url="https://applynow.co.zw/job1",
        title="UNICEF seeks Data Analyst in Zimbabwe",
        company="UNICEF",
        location="Harare",
        summary="UNICEF seeks a Data Analyst to support health and education reporting. Apply by 22 June 2026.",
        apply_url="/apply",
    )
    job = map_raw_job(raw, cfg)
    assert job.title == "Data Analyst in Zimbabwe"
    assert job.company == "UNICEF"
    assert job.category == "NGO & Development"
    assert "Source: applynow" in job.summary


def test_validator_rejects_bad_url():
    job = map_raw_job(
        RawJob(source_name="x", source_url="https://example.com/job", title="Bad Job", company="ACME", summary="Short", apply_url="mailto:test@example.com"),
        SourceConfig(name="x", type="generic", start_urls=[]),
    )
    result = JobValidator(skip_expired=True).validate(job)
    assert not result.ok
    assert "apply_url_invalid" in result.reasons or "summary_too_short" in result.reasons


def test_dedupe_by_url():
    cfg = SourceConfig(name="x", type="generic", start_urls=[])
    raw1 = RawJob(source_name="x", source_url="https://example.com/jobs/1", title="Finance Officer", company="Org", location="Harare", summary="A long finance role description " * 10, apply_url="https://example.com/jobs/1")
    raw2 = RawJob(source_name="x", source_url="https://example.com/jobs/1#apply", title="Finance Officer", company="Org", location="Harare", summary="A long finance role description " * 10, apply_url="https://example.com/jobs/1#apply")
    jobs = [map_raw_job(raw1, cfg), map_raw_job(raw2, cfg)]
    assert len(dedupe_in_memory(jobs)) == 1


def test_sqlite_insert_legacy_schema(tmp_path: Path):
    db_path = tmp_path / "jobs.db"
    repo = SQLiteJobRepository(str(db_path), auto_add_optional_columns=False)
    cfg = SourceConfig(name="x", type="generic", start_urls=[])
    job = map_raw_job(
        RawJob(source_name="x", source_url="https://example.com/jobs/2", title="Operations Coordinator", company="Org", location="Harare", summary="A detailed operations coordinator role description " * 10, apply_url="https://example.com/jobs/2"),
        cfg,
    )
    stats = repo.insert_many([job])
    repo.close()
    assert stats["inserted"] == 1
    con = sqlite3.connect(db_path)
    row = con.execute("SELECT title, company, location, category, summary, apply_url FROM jobs").fetchone()
    assert row[0] == "Operations Coordinator"
    assert row[5] == "https://example.com/jobs/2"



def test_applynow_title_summary_and_remote_category_are_cleaned():
    cfg = SourceConfig(name="applynow_zimbabwe", type="applynow", start_urls=[], default_location="Zimbabwe", default_category="Private Sector")
    raw = RawJob(
        source_name="applynow_zimbabwe",
        source_url="https://applynow.co.zw/2026/06/12/meraki-labs/",
        title="Meraki Labs is hiring a Communications and Reporting Officer (Remote) | Apply by 22 June 2026",
        company=None,
        location="Zimbabwe",
        summary="""
        Meraki Labs is hiring a Communications and Reporting Officer (Remote) | Apply by 22 June 2026
        Meraki Labs is seeking a Communications and Reporting Officer to support internal and external communications, reporting, project coordination, and operational processes.
        Contents
        • Communications and Reporting Officer (Remote) – Meraki Labs
        • Key Responsibilities
        • Internal and External Communications Support
        • Reporting, Editing, and Knowledge Management
        • Project Coordination and Operational Support
        • Required Skills
        • Qualifications and Experience
        • Working Arrangements
        • Compensation and Benefits
        • Application Process
        • Important Dates
        • Selection Process
        Job Title: Communications and Reporting Officer
        Location: Remote
        Closing Date: 22 June 2026
        Contract Type: Full-Time
        Salary: USD 900 per month
        This position is ideal for a highly organized communicator with strong writing skills.
        """,
        apply_url="https://applynow.co.zw/2026/06/12/meraki-labs/",
    )
    job = map_raw_job(raw, cfg)
    assert job.title == "Communications and Reporting Officer (Remote)"
    assert job.company == "Meraki Labs"
    assert job.location == "Remote / Zimbabwe"
    assert job.category == "Remote & International"
    assert "Contents" not in job.summary
    assert "Key Responsibilities" not in job.summary
    assert "Deadline: 2026-06-22" in job.summary
