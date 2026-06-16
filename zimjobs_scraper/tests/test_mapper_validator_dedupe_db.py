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


def test_mapping_uses_default_company_when_extraction_is_noisy():
    cfg = SourceConfig(name="zimplats_careers", type="generic", start_urls=[], default_company="Zimplats", default_location="Zimbabwe")
    raw = RawJob(
        source_name="zimplats_careers",
        source_url="https://www.careers-page.com/implats/job/RY7597V6",
        title="Zimplats Apprenticeship Programme",
        company="the job Zimplats Apprenticeship Programme",
        location="Zimbabwe",
        summary="About the job Zimplats Apprenticeship Programme Zimbabwe Platinum Mines is recruiting apprenticeship trainees in Zimbabwe.",
        apply_url="https://www.careers-page.com/implats/job/RY7597V6",
    )
    job = map_raw_job(raw, cfg)
    assert job.company == "Zimplats"


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


def test_sqlite_auto_adds_enriched_optional_columns(tmp_path: Path):
    db_path = tmp_path / "jobs.db"
    repo = SQLiteJobRepository(str(db_path), auto_add_optional_columns=True)
    cfg = SourceConfig(name="psc", type="psc_erecruitment", start_urls=[], default_category="Government")
    job = map_raw_job(
        RawJob(
            source_name="psc",
            source_url="https://erecruitment.psc.gov.zw/jobs/172",
            title="Deputy Director, Monitoring and Evaluation",
            company="Public Service Commission Zimbabwe",
            location="Harare, Zimbabwe",
            category="Government",
            summary="Coordinate monitoring and evaluation work for a ministry. Requirements: A degree in Monitoring and Evaluation and six years relevant experience.",
            apply_url="https://erecruitment.psc.gov.zw/jobs/172",
            department="Ministry of Information",
            requirements="A degree in Monitoring and Evaluation.",
            external_id="A/GEN/13/21",
        ),
        cfg,
    )
    stats = repo.insert_many([job])
    cols = repo.columns()
    total_jobs = repo.count_jobs()
    row = repo.conn.execute("SELECT department, requirements, external_job_id, job_description FROM jobs").fetchone()
    repo.close()
    assert stats["inserted"] == 1
    assert total_jobs == 1
    assert {"department", "requirements", "external_job_id", "job_description"}.issubset(cols)
    assert row[0] == "Ministry of Information"
    assert row[2] == "A/GEN/13/21"


def test_sqlite_delete_expired_jobs_removes_saved_refs(tmp_path: Path):
    db_path = tmp_path / "jobs.db"
    repo = SQLiteJobRepository(str(db_path), auto_add_optional_columns=True)
    repo.conn.execute("CREATE TABLE saved_jobs(user_id INTEGER, job_id INTEGER)")
    repo.conn.execute(
        """INSERT INTO jobs(title,company,location,category,summary,apply_url,expires_at)
           VALUES(?,?,?,?,?,?,?)""",
        ("Expired Role", "Org", "Harare", "Private Sector", "Expired summary", "https://example.com/expired", "2020-01-01"),
    )
    expired_id = repo.conn.execute("SELECT id FROM jobs WHERE title='Expired Role'").fetchone()["id"]
    repo.conn.execute("INSERT INTO saved_jobs(user_id, job_id) VALUES(?, ?)", (1, expired_id))
    repo.conn.execute(
        """INSERT INTO jobs(title,company,location,category,summary,apply_url,expires_at)
           VALUES(?,?,?,?,?,?,?)""",
        ("Active Role", "Org", "Harare", "Private Sector", "Active summary", "https://example.com/active", "2099-01-01"),
    )
    repo.conn.commit()

    deleted = repo.delete_expired_jobs()
    remaining_titles = [r["title"] for r in repo.conn.execute("SELECT title FROM jobs ORDER BY title").fetchall()]
    saved_count = repo.conn.execute("SELECT COUNT(*) c FROM saved_jobs").fetchone()["c"]
    repo.close()

    assert deleted == 1
    assert remaining_titles == ["Active Role"]
    assert saved_count == 0



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
    assert job.title == "Communications and Reporting Officer"
    assert job.company == "Meraki Labs"
    assert job.location == "Remote / Zimbabwe"
    assert job.category == "Remote & International"
    assert "Contents" not in job.summary
    assert "Key Responsibilities" not in job.summary
    assert "Deadline: 2026-06-22" in job.summary


def test_quality_v3_strips_company_salary_and_remote_from_title():
    cfg = SourceConfig(name="applynow_zimbabwe", type="applynow", start_urls=[], default_location="Zimbabwe", default_category="Private Sector")
    raw = RawJob(
        source_name="applynow_zimbabwe",
        source_url="https://applynow.co.zw/2026/06/12/self-investigation/",
        title="The Self-Investigation is hiring: Operations Coordinator (Remote) | Earn EUR 1,400 per month",
        company=None,
        location="Remote",
        summary="""
        The Self-Investigation is hiring: Operations Coordinator (Remote) | Earn EUR 1,400 per month
        Job Title: Operations Coordinator
        Location: Remote
        Salary: EUR 1,400 per month
        The Operations Coordinator will support operations, administration, reporting and team coordination for a remote organization.
        """,
        apply_url="https://applynow.co.zw/2026/06/12/self-investigation/",
    )
    job = map_raw_job(raw, cfg)
    assert job.title == "Operations Coordinator"
    assert job.company == "The Self-Investigation"
    assert job.category == "Remote & International"
    assert "EUR 1,400" in job.summary


def test_applynow_job_title_on_next_line_beats_toc_heading():
    cfg = SourceConfig(name="applynow_zimbabwe", type="applynow", start_urls=[], default_location="Zimbabwe", default_category="Private Sector")
    raw = RawJob(
        source_name="applynow_zimbabwe",
        source_url="https://applynow.co.zw/2026/06/14/innovare-health/",
        title="Innovare Health Collective is recruiting Reproductive Health & Family Planning Consultants (USD 200-300/Day)",
        company=None,
        location="Africa",
        summary="""
        Innovare Health Collective Hiring Consultants Across Africa for Kenya Reproductive Health Project Paying Up to USD 300 Per Day
        The Role
        How to Apply
        Job Title:
        Consultant - Reproductive Health and Family Planning Market Analysis Project
        Innovare Health Collective is expanding its consultant network across Africa.
        Requirements:
        Five years of reproductive health consulting experience.
        """,
        apply_url="https://applynow.co.zw/2026/06/14/innovare-health/",
    )
    job = map_raw_job(raw, cfg)
    assert job.title == "Consultant - Reproductive Health and Family Planning Market Analysis Project"
    assert job.company == "Innovare Health Collective"


def test_applynow_inline_job_title_after_how_to_apply_is_cleaned():
    cfg = SourceConfig(name="applynow_zimbabwe", type="applynow", start_urls=[], default_location="Zimbabwe", default_category="Private Sector")
    raw = RawJob(
        source_name="applynow_zimbabwe",
        source_url="https://applynow.co.zw/2026/06/14/innovare-health/",
        title="Innovare Health Collective is recruiting Reproductive Health & Family Planning Consultants (USD 200-300/Day)",
        company=None,
        location="Africa",
        summary="""
        How to Apply Job Title: Consultant - Reproductive Health and Family Planning Market Analysis Project
        Innovare Health Collective is expanding its consultant network across Africa.
        """,
        apply_url="https://applynow.co.zw/2026/06/14/innovare-health/",
    )
    job = map_raw_job(raw, cfg)
    assert job.title == "Consultant - Reproductive Health and Family Planning Market Analysis Project"


def test_quality_v3_rejects_generic_multi_vacancy_title_and_sentence_company():
    cfg = SourceConfig(name="applynow_zimbabwe", type="applynow", start_urls=[], default_location="Zimbabwe", default_category="Private Sector")
    raw = RawJob(
        source_name="applynow_zimbabwe",
        source_url="https://applynow.co.zw/2026/06/01/chewore/",
        title="3 new job positions",
        company="is implementing an innovative conservation model aimed at securing the long-term protection",
        location="Zimbabwe",
        summary="""
        Chewore Conservation Trust Zimbabwe Jobs June 2026 – Multiple Vacancies
        Chewore Conservation Trust (CCT), operating under the Awe for Nature Foundation, is inviting qualified professionals to apply.
        About Chewore Conservation Trust The organisation is implementing an innovative conservation model aimed at securing wilderness.
        """,
        apply_url="https://applynow.co.zw/2026/06/01/chewore/",
    )
    job = map_raw_job(raw, cfg)
    assert job.company == "Chewore Conservation Trust"
    result = JobValidator(skip_expired=False).validate(job)
    assert not result.ok
    assert "title_not_real_role" in result.reasons


def test_somewhere_marketing_page_is_skipped_by_parser():
    from zimjobs_scraper.parsers import SomewhereParser

    cfg = SourceConfig(name="somewhere_remote", type="somewhere", start_urls=[], default_location="Remote", default_category="Remote & International")
    parser = SomewhereParser(cfg)
    html = """
    <html><head><title>Jobs | Somewhere</title></head><body>
    <h1>Jobs | Somewhere</h1>
    <p>Talent On-Demand Hire remote professionals on demand. No upfront fees.</p>
    <p>Somewhere Browser Manage security and access with the browser built for enterprise.</p>
    </body></html>
    """
    assert parser.parse_detail(html, "https://somewhere.com/jobs") is None
