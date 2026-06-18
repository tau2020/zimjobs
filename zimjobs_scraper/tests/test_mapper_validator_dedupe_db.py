import sqlite3
from pathlib import Path

from zimjobs_scraper.db import SQLiteJobRepository
from zimjobs_scraper.dedupe import dedupe_in_memory
from zimjobs_scraper.backfill_vacancymail import backfill_vacancy_mail_companies
from zimjobs_scraper.clean_db import clean_jobs
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


def test_validator_rejects_known_deceptive_scraped_remoteok_boilerplate():
    job = map_raw_job(
        RawJob(
            source_name="remoteok_api",
            source_url="https://remoteok.com/remote-jobs/456-venture-capital-investment-analyst",
            title="Venture Capital Investment Analyst",
            company="Remote Company",
            location="Remote",
            summary=(
                "Please mention the word ADVOCATES and tag RMTUyLjU1LjE3Ny44Mw== "
                "when applying to show you read the job post completely. "
                "The analyst will review investment data and prepare portfolio reports for clients."
            ),
            apply_url="https://remoteok.com/remote-jobs/456-venture-capital-investment-analyst",
        ),
        SourceConfig(name="remoteok_api", type="remoteok_api", start_urls=[]),
    )

    result = JobValidator(skip_expired=True).validate(job)

    assert not result.ok
    assert "unsafe_scraped_content" in result.reasons


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


def test_vacancy_mail_zimbabwe_explicit_company_in_description():
    cfg = SourceConfig(name="vacancymail_zimbabwe", type="generic", start_urls=[], default_location="Zimbabwe")
    job = map_raw_job(
        RawJob(
            source_name="vacancymail_zimbabwe",
            source_url="https://vacancymail.co.zw/jobs/lab-tech-1/",
            title="Laboratory Technician",
            company="Vacancy Mail",
            location="Zimbabwe",
            summary="Company: The Union Zimbabwe Trust\nThe role supports laboratory operations and reporting.",
            apply_url="https://vacancymail.co.zw/jobs/lab-tech-1/",
        ),
        cfg,
    )
    assert job.company == "The Union Zimbabwe Trust"


def test_vacancy_mail_zimbabwe_organization_name_in_description():
    cfg = SourceConfig(name="vacancymail_zimbabwe", type="generic", start_urls=[], default_location="Zimbabwe")
    job = map_raw_job(
        RawJob(
            source_name="vacancymail_zimbabwe",
            source_url="https://vacancymail.co.zw/jobs/lab-tech-2/",
            title="Laboratory Technician",
            company="Vacancy Mail",
            location="Zimbabwe",
            summary="The Union Zimbabwe Trust seeks a laboratory technician to support testing, quality control, and reporting.",
            apply_url="https://vacancymail.co.zw/jobs/lab-tech-2/",
        ),
        cfg,
    )
    assert job.company == "The Union Zimbabwe Trust"


def test_vacancy_mail_zimbabwe_company_from_custom_email_domain():
    cfg = SourceConfig(name="vacancymail_zimbabwe", type="generic", start_urls=[], default_location="Zimbabwe")
    job = map_raw_job(
        RawJob(
            source_name="vacancymail_zimbabwe",
            source_url="https://vacancymail.co.zw/jobs/lab-tech-3/",
            title="LABORATORY TECHNICIAN",
            company=" vacancy   mail ",
            location="Zimbabwe",
            summary="Send applications to hr@supremebrands.co.zw with a CV and cover letter.",
            apply_url="https://vacancymail.co.zw/jobs/lab-tech-3/",
        ),
        cfg,
    )
    assert job.company == "Supreme Brands"


def test_vacancy_mail_zimbabwe_generic_email_domain_ignored():
    cfg = SourceConfig(name="vacancymail_zimbabwe", type="generic", start_urls=[], default_location="Zimbabwe")
    job = map_raw_job(
        RawJob(
            source_name="vacancymail_zimbabwe",
            source_url="https://vacancymail.co.zw/jobs/admin-1/",
            title="Administrative Assistant",
            company="Vacancy Mail",
            location="Zimbabwe",
            summary="Send applications to hiringteam@gmail.com. The role supports office records and scheduling.",
            apply_url="https://vacancymail.co.zw/jobs/admin-1/",
        ),
        cfg,
    )
    assert job.company == "Vacancy Mail"


def test_vacancy_mail_zimbabwe_existing_valid_company_preserved():
    cfg = SourceConfig(name="vacancymail_zimbabwe", type="generic", start_urls=[], default_location="Zimbabwe")
    job = map_raw_job(
        RawJob(
            source_name="vacancymail_zimbabwe",
            source_url="https://vacancymail.co.zw/jobs/finance-1/",
            title="Finance Officer",
            company="Actual Employer",
            location="Zimbabwe",
            summary="Company: Another Employer\nThe role supports finance controls and reporting.",
            apply_url="https://vacancymail.co.zw/jobs/finance-1/",
        ),
        cfg,
    )
    assert job.company == "Actual Employer"


def test_vacancy_mail_non_zimbabwe_posting_unaffected():
    cfg = SourceConfig(name="vacancymail_south_africa", type="generic", start_urls=[], default_location="South Africa")
    job = map_raw_job(
        RawJob(
            source_name="vacancymail_south_africa",
            source_url="https://example.com/jobs/admin-2/",
            title="Administrative Assistant",
            company="Vacancy Mail",
            location="South Africa",
            summary="Company: Example Employer\nThe role supports office records and scheduling.",
            apply_url="https://example.com/jobs/admin-2/",
        ),
        cfg,
    )
    assert job.company == "Vacancy Mail"


def test_vacancy_mail_zimbabwe_accented_company_name_preserved():
    cfg = SourceConfig(name="vacancymail_zimbabwe", type="generic", start_urls=[], default_location="Zimbabwe")
    job = map_raw_job(
        RawJob(
            source_name="vacancymail_zimbabwe",
            source_url="https://vacancymail.co.zw/jobs/programme-intern-1/",
            title="Programme Intern",
            company="Vacancy Mail",
            location="Zimbabwe",
            summary="About Trócaire\nTrócaire is recruiting a programme intern to support partner coordination.",
            apply_url="https://vacancymail.co.zw/jobs/programme-intern-1/",
        ),
        cfg,
    )
    assert job.company == "Trócaire"


def test_vacancy_mail_backfill_updates_confident_rows_and_skips_weak_guesses(tmp_path: Path):
    db_path = tmp_path / "jobs.db"
    repo = SQLiteJobRepository(str(db_path), auto_add_optional_columns=True)
    repo.conn.execute(
        """INSERT INTO jobs(title, company, location, category, summary, apply_url, source_name, source_url, job_description)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            "LABORATORY TECHNICIAN",
            "Vacancy Mail",
            "Zimbabwe",
            "Private Sector",
            "Send applications to hr@supremebrands.co.zw with a CV.",
            "https://vacancymail.co.zw/jobs/lab-tech-4/",
            "vacancymail_zimbabwe",
            "https://vacancymail.co.zw/jobs/lab-tech-4/",
            "Send applications to hr@supremebrands.co.zw with a CV.",
        ),
    )
    repo.conn.execute(
        """INSERT INTO jobs(title, company, location, category, summary, apply_url, source_name, source_url, job_description)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            "Administrative Assistant",
            "Vacancy Mail",
            "Zimbabwe",
            "Private Sector",
            "Send applications to hiringteam@gmail.com.",
            "https://vacancymail.co.zw/jobs/admin-3/",
            "vacancymail_zimbabwe",
            "https://vacancymail.co.zw/jobs/admin-3/",
            "Send applications to hiringteam@gmail.com.",
        ),
    )
    repo.conn.commit()
    repo.close()

    stats = backfill_vacancy_mail_companies(str(db_path))

    con = sqlite3.connect(db_path)
    rows = con.execute("SELECT title, company FROM jobs ORDER BY id").fetchall()
    con.close()
    assert stats.scanned == 2
    assert stats.updated == 1
    assert stats.skipped == 1
    assert rows == [("LABORATORY TECHNICIAN", "Supreme Brands"), ("Administrative Assistant", "Vacancy Mail")]


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


def test_sqlite_insert_many_tracks_changed_urls_and_material_updates(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("BASE_URL", "https://zimjobs.example")
    db_path = tmp_path / "jobs.db"
    repo = SQLiteJobRepository(str(db_path), auto_add_optional_columns=True)
    cfg = SourceConfig(name="x", type="generic", start_urls=[])
    first = map_raw_job(
        RawJob(
            source_name="x",
            source_url="https://example.com/jobs/changed",
            title="Operations Coordinator",
            company="Org",
            location="Harare",
            summary="Coordinate field operations, logistics, documentation, stakeholder updates, and team reporting across Zimbabwe.",
            apply_url="https://example.com/jobs/changed",
        ),
        cfg,
    )
    unchanged = map_raw_job(
        RawJob(
            source_name="x",
            source_url="https://example.com/jobs/changed",
            title="Operations Coordinator",
            company="Org",
            location="Harare",
            summary="Coordinate field operations, logistics, documentation, stakeholder updates, and team reporting across Zimbabwe.",
            apply_url="https://example.com/jobs/changed",
        ),
        cfg,
    )
    changed = map_raw_job(
        RawJob(
            source_name="x",
            source_url="https://example.com/jobs/changed",
            title="Operations Coordinator",
            company="Org",
            location="Harare",
            summary="Coordinate field operations, logistics, compliance files, fleet scheduling, stakeholder updates, and weekly team reporting across Zimbabwe.",
            apply_url="https://example.com/jobs/changed",
        ),
        cfg,
    )

    inserted = repo.insert_many([first])
    inserted_urls = list(repo.changed_urls)
    skipped = repo.insert_many([unchanged])
    skipped_urls = list(repo.changed_urls)
    updated = repo.insert_many([changed])
    updated_urls = list(repo.changed_urls)
    row = repo.conn.execute("SELECT summary FROM jobs WHERE id=1").fetchone()
    repo.close()

    assert inserted["inserted"] == 1
    assert inserted["updated"] == 0
    assert inserted_urls == ["https://zimjobs.example/job/1/operations-coordinator"]
    assert skipped["skipped"] == 1
    assert skipped["updated"] == 0
    assert skipped_urls == []
    assert updated["updated"] == 1
    assert updated_urls == ["https://zimjobs.example/job/1/operations-coordinator"]
    assert "fleet scheduling" in row["summary"]


def test_sqlite_delete_expired_jobs_removes_saved_refs(tmp_path: Path):
    db_path = tmp_path / "jobs.db"
    repo = SQLiteJobRepository(str(db_path), auto_add_optional_columns=True)
    repo.conn.execute("CREATE TABLE saved_jobs(user_id INTEGER, job_id INTEGER)")
    repo.conn.execute(
        """INSERT INTO jobs(title,company,location,category,summary,apply_url,expires_at)
           VALUES(?,?,?,?,?,?,?)""",
        (
            "Expired Role",
            "Org",
            "Harare",
            "Private Sector",
            "Expired summary",
            "https://example.com/expired",
            "2020-01-01",
        ),
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


def test_sqlite_delete_expired_jobs_rebuilds_stale_fts_before_delete(tmp_path: Path):
    db_path = tmp_path / "jobs.db"
    repo = SQLiteJobRepository(str(db_path), auto_add_optional_columns=True)
    repo.conn.execute(
        """INSERT INTO jobs(title,company,location,category,summary,apply_url,expires_at)
           VALUES(?,?,?,?,?,?,?)""",
        ("Expired Role", "Org", "Harare", "Private Sector", "Expired summary", "https://example.com/expired", "2020-01-01"),
    )
    repo.conn.execute(
        """CREATE VIRTUAL TABLE jobs_fts USING fts5(
           title, company, summary, location,
           content='jobs', content_rowid='id')"""
    )
    repo.conn.execute(
        """CREATE TRIGGER jobs_ad AFTER DELETE ON jobs BEGIN
           INSERT INTO jobs_fts(jobs_fts,rowid,title,company,summary,location)
           VALUES('delete',old.id,old.title,old.company,old.summary,old.location); END"""
    )
    repo.conn.commit()

    deleted = repo.delete_expired_jobs()
    remaining = repo.count_jobs()
    repo.close()

    assert deleted == 1
    assert remaining == 0


def test_sqlite_delete_bad_description_jobs_removes_spam_and_saved_refs(tmp_path: Path):
    db_path = tmp_path / "jobs.db"
    repo = SQLiteJobRepository(str(db_path), auto_add_optional_columns=True)
    repo.conn.execute("CREATE TABLE saved_jobs(user_id INTEGER, job_id INTEGER)")
    repo.conn.execute(
        """INSERT INTO jobs(title,company,location,category,summary,apply_url,job_description)
           VALUES(?,?,?,?,?,?,?)""",
        (
            "Venture Capital Investment Analyst",
            "Remote Company",
            "Remote",
            "Private Sector",
            "Role Overview",
            "https://example.com/bad",
            "Please mention the word ADVOCATES and tag RMTUyLjU1LjE3Ny44Mw== when applying to show you read the job post completely.",
        ),
    )
    bad_id = repo.conn.execute("SELECT id FROM jobs WHERE title='Venture Capital Investment Analyst'").fetchone()["id"]
    repo.conn.execute("INSERT INTO saved_jobs(user_id, job_id) VALUES(?, ?)", (1, bad_id))
    repo.conn.execute(
        """INSERT INTO jobs(title,company,location,category,summary,apply_url,job_description)
           VALUES(?,?,?,?,?,?,?)""",
        (
            "Clean Analyst",
            "Remote Company",
            "Harare",
            "Private Sector",
            "A normal analyst role with enough detail for applicants.",
            "https://example.com/clean",
            "Analyze investments and prepare reports for a local portfolio team.",
        ),
    )
    repo.conn.commit()

    deleted = repo.delete_bad_description_jobs()
    remaining_titles = [r["title"] for r in repo.conn.execute("SELECT title FROM jobs ORDER BY title").fetchall()]
    saved_count = repo.conn.execute("SELECT COUNT(*) c FROM saved_jobs").fetchone()["c"]
    repo.close()

    assert deleted == 1
    assert remaining_titles == ["Clean Analyst"]
    assert saved_count == 0


def test_clean_jobs_bad_description_cleanup_rebuilds_fts(tmp_path: Path):
    db_path = tmp_path / "jobs.db"
    repo = SQLiteJobRepository(str(db_path), auto_add_optional_columns=True)
    repo.conn.execute(
        """CREATE VIRTUAL TABLE jobs_fts USING fts5(
           title, company, summary, location,
           content='jobs', content_rowid='id')"""
    )
    repo.conn.execute(
        """INSERT INTO jobs(title,company,location,category,summary,apply_url,job_description)
           VALUES(?,?,?,?,?,?,?)""",
        (
            "Junior Front-End Developer",
            "Example Ltd",
            "Remote",
            "Remote & International",
            "Posted 12:31:23 PM. Job Title: Junior Front-End Developer. See this and similar jobs on LinkedIn.",
            "https://example.com/linkedin",
            "Please mention the word LUCK and tag RMTUyLjU1LjE3Ny44Mw== when applying.",
        ),
    )
    repo.conn.execute(
        """INSERT INTO jobs(title,company,location,category,summary,apply_url,job_description)
           VALUES(?,?,?,?,?,?,?)""",
        (
            "Frontend Developer",
            "Example Ltd",
            "Harare",
            "Private Sector",
            "Build accessible interfaces and maintain a Flask-backed job board.",
            "https://example.com/frontend",
            "Build accessible interfaces and maintain a Flask-backed job board.",
        ),
    )
    repo.conn.execute("INSERT INTO jobs_fts(jobs_fts) VALUES('rebuild')")
    repo.conn.commit()
    repo.close()

    stats = clean_jobs(str(db_path), bad_descriptions=True)
    con = sqlite3.connect(db_path)
    rows = con.execute("SELECT title FROM jobs ORDER BY title").fetchall()
    fts_rows = con.execute("SELECT COUNT(*) FROM jobs_fts").fetchone()[0]
    con.close()

    assert stats["bad_description_jobs"] == 1
    assert stats["deleted"] == 1
    assert stats["fts_rebuilt"] == 1
    assert rows == [("Frontend Developer",)]
    assert fts_rows == 1


def test_bad_description_cleanup_is_safe_on_legacy_schema(tmp_path: Path):
    db_path = tmp_path / "jobs.db"
    repo = SQLiteJobRepository(str(db_path), auto_add_optional_columns=False)
    repo.conn.execute(
        """INSERT INTO jobs(title,company,location,category,summary,apply_url)
           VALUES(?,?,?,?,?,?)""",
        (
            "Legacy Bad Row",
            "Example Ltd",
            "Remote",
            "Private Sector",
            "Please mention the word ADVOCATES and tag RMTUyLjU1LjE3Ny44Mw== when applying.",
            "https://example.com/legacy-bad",
        ),
    )
    repo.conn.execute(
        """INSERT INTO jobs(title,company,location,category,summary,apply_url)
           VALUES(?,?,?,?,?,?)""",
        (
            "Legacy Clean Row",
            "Example Ltd",
            "Harare",
            "Private Sector",
            "Normal job summary.",
            "https://example.com/legacy-clean",
        ),
    )
    repo.conn.commit()

    deleted = repo.delete_bad_description_jobs()
    remaining_titles = [r["title"] for r in repo.conn.execute("SELECT title FROM jobs ORDER BY title").fetchall()]
    repo.close()

    assert deleted == 1
    assert remaining_titles == ["Legacy Clean Row"]


def test_bad_description_cleanup_removes_remoteok_job_urls(tmp_path: Path):
    db_path = tmp_path / "jobs.db"
    repo = SQLiteJobRepository(str(db_path), auto_add_optional_columns=True)
    repo.conn.execute(
        """INSERT INTO jobs(title,company,location,category,summary,apply_url,source_url)
           VALUES(?,?,?,?,?,?,?)""",
        (
            "RemoteOK Role",
            "Example Ltd",
            "Remote",
            "Remote & International",
            "A normal looking summary from a source we no longer want.",
            "https://remoteok.com/remote-jobs/456-product-manager",
            "https://remoteok.com/remote-jobs/456-product-manager",
        ),
    )
    repo.conn.execute(
        """INSERT INTO jobs(title,company,location,category,summary,apply_url,source_url)
           VALUES(?,?,?,?,?,?,?)""",
        (
            "Non RemoteOK Role",
            "Example Ltd",
            "Remote",
            "Remote & International",
            "A normal looking summary from a retained source.",
            "https://example.com/remote-jobs/456-product-manager",
            "https://example.com/remote-jobs/456-product-manager",
        ),
    )
    repo.conn.commit()

    deleted = repo.delete_bad_description_jobs()
    remaining_urls = [r["apply_url"] for r in repo.conn.execute("SELECT apply_url FROM jobs ORDER BY title").fetchall()]
    repo.close()

    assert deleted == 1
    assert remaining_urls == ["https://example.com/remote-jobs/456-product-manager"]



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


def test_validator_rejects_task_fragment_job_titles():
    cfg = SourceConfig(name="vacancymail_zimbabwe", type="generic", start_urls=[], default_location="Zimbabwe", default_category="Private Sector")
    for title in [
        "Monitor surveillance equipment such as CCTV systems",
        "Examine structural drivers of learning disparities",
        "Registered General Nurse qualification with a minimum of two years work experience",
    ]:
        job = map_raw_job(
            RawJob(
                source_name="vacancymail_zimbabwe",
                source_url="https://example.com/jobs/1",
                title=title,
                company="Example Company",
                location="Zimbabwe",
                summary="This role supports operational delivery in Zimbabwe with clear responsibilities, reporting lines, and application instructions.",
                apply_url="https://example.com/jobs/1",
            ),
            cfg,
        )
        result = JobValidator(skip_expired=False).validate(job)
        assert not result.ok
        assert "title_not_real_role" in result.reasons


def test_mapper_preserves_common_zimbabwe_role_titles_over_body_matches():
    cfg = SourceConfig(name="vacancymail_zimbabwe", type="generic", start_urls=[], default_location="Zimbabwe", default_category="Private Sector")
    for title in ["Security Guard", "Artisan - Fitter & Turner", "Radiographer", "Salesperson - Kwekwe and Mutare"]:
        job = map_raw_job(
            RawJob(
                source_name="vacancymail_zimbabwe",
                source_url="https://vacancymail.co.zw/jobs/example-1/",
                title=title,
                company="Example Company",
                location="Zimbabwe",
                summary="""
                Assistant Regional Loss Control Officer
                This is related page content that should not replace the current detail page title.
                The current role is based in Zimbabwe with standard job responsibilities and application instructions.
                """,
                apply_url="https://vacancymail.co.zw/jobs/example-1/",
            ),
            cfg,
        )
        assert job.title == title
        assert JobValidator(skip_expired=False).validate(job).ok


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
