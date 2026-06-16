from __future__ import annotations

import importlib
import json
import sys
from datetime import date
from pathlib import Path

from bs4 import BeautifulSoup

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from job_schema import build_job_posting_json_ld


SITE_CONFIG = {
    "job_url": "https://zimjobs.example/job/1/finance-officer",
    "default_country_code": "ZW",
    "default_remote_applicant_country": "Zimbabwe",
}


def test_onsite_job_schema_includes_google_jobposting_fields():
    schema = build_job_posting_json_ld(
        {
            "id": 1,
            "title": "Finance Officer",
            "company": "Example NGO",
            "location": "Harare",
            "summary": "Manage programme finance and donor reporting.\n\nSource: feed | https://example.org/job",
            "created_at": "2026-06-01 09:00:00",
            "expires_at": "2099-12-31",
            "employment_type": "Full-time",
            "salary_range": "USD 900 - 1200 per month",
        },
        SITE_CONFIG,
        today=date(2026, 6, 16),
    )

    assert schema["@type"] == "JobPosting"
    assert schema["title"] == "Finance Officer"
    assert schema["description"] == "<p>Manage programme finance and donor reporting.</p>"
    assert schema["datePosted"] == "2026-06-01"
    assert schema["validThrough"] == "2099-12-31"
    assert schema["employmentType"] == "FULL_TIME"
    assert schema["identifier"]["value"] == "1"
    assert schema["jobLocation"]["address"]["addressCountry"] == "ZW"
    assert schema["jobLocation"]["address"]["addressLocality"] == "Harare"
    assert schema["baseSalary"]["currency"] == "USD"
    assert schema["baseSalary"]["value"]["minValue"] == 900
    assert schema["baseSalary"]["value"]["maxValue"] == 1200
    assert schema["baseSalary"]["value"]["unitText"] == "MONTH"
    assert "jobLocationType" not in schema


def test_remote_job_schema_uses_telecommute_and_applicant_location():
    schema = build_job_posting_json_ld(
        {
            "id": 2,
            "title": "Customer Support Specialist",
            "company": "Remote Co",
            "location": "Remote / Zimbabwe",
            "summary": "Provide email support for customers from a fully remote setup.",
            "created_at": "2026-06-01",
            "remote_status": "Remote",
            "employment_type": "Part-time",
        },
        SITE_CONFIG,
        today=date(2026, 6, 16),
    )

    assert schema["jobLocationType"] == "TELECOMMUTE"
    assert schema["applicantLocationRequirements"] == {
        "@type": "Country",
        "name": "Zimbabwe",
    }
    assert schema["employmentType"] == "PART_TIME"
    assert "jobLocation" not in schema


def test_schema_omits_optional_fields_that_cannot_be_structured():
    schema = build_job_posting_json_ld(
        {
            "id": 3,
            "title": "Operations Assistant",
            "company": "Example Retail",
            "location": "Bulawayo",
            "summary": "Support stock control, supplier follow-up, and store administration.",
            "created_at": "2026-06-01",
            "salary_range": "Competitive",
            "employment_type": "Shift work",
        },
        SITE_CONFIG,
        today=date(2026, 6, 16),
    )

    assert schema["jobLocation"]["address"]["addressCountry"] == "ZW"
    assert "baseSalary" not in schema
    assert "employmentType" not in schema
    assert "validThrough" not in schema


def test_expired_or_closed_jobs_do_not_emit_jobposting_schema():
    expired = build_job_posting_json_ld(
        {
            "id": 4,
            "title": "Expired Role",
            "company": "Example Co",
            "location": "Harare",
            "summary": "This role has closed.",
            "created_at": "2020-01-01",
            "expires_at": "2020-01-31",
        },
        SITE_CONFIG,
        today=date(2026, 6, 16),
    )
    closed = build_job_posting_json_ld(
        {
            "id": 5,
            "title": "Closed Role",
            "company": "Example Co",
            "location": "Harare",
            "summary": "This role has closed.",
            "created_at": "2026-06-01",
            "tags": "closed",
        },
        SITE_CONFIG,
        today=date(2026, 6, 16),
    )

    assert expired is None
    assert closed is None


def test_job_page_renders_schema_only_for_active_detail_pages(tmp_path, monkeypatch):
    web_app = import_web_app(tmp_path, monkeypatch)
    onsite_id = insert_job(
        web_app,
        title="Finance Officer",
        company="Example NGO",
        location="Harare",
        summary="Manage finance and donor reporting.",
        employment_type="Full-time",
        salary_range="USD 900 per month",
        remote_status="On-site",
        expires_at="2099-12-31",
        job_description="Manage finance and donor reporting.",
    )
    expired_id = insert_job(
        web_app,
        title="Expired Role",
        company="Example NGO",
        location="Harare",
        summary="Closed role.",
        expires_at="2020-01-01",
    )

    client = web_app.app.test_client()
    active_response = client.get(f"/job/{onsite_id}/finance-officer")
    active_schema = first_jobposting_schema(active_response.get_data(as_text=True))
    listing_response = client.get("/")
    expired_response = client.get(f"/job/{expired_id}/expired-role")

    assert active_response.status_code == 200
    assert active_schema["identifier"]["value"] == str(onsite_id)
    assert active_schema["baseSalary"]["value"]["value"] == 900
    assert listing_response.status_code == 200
    assert "JobPosting" not in listing_response.get_data(as_text=True)
    assert expired_response.status_code == 404
    assert "JobPosting" not in expired_response.get_data(as_text=True)


def test_homepage_filterbar_is_open_by_default(tmp_path, monkeypatch):
    web_app = import_web_app(tmp_path, monkeypatch)

    response = web_app.app.test_client().get("/")
    soup = BeautifulSoup(response.get_data(as_text=True), "html.parser")
    filterbar = soup.find("details", class_="filterbar")

    assert response.status_code == 200
    assert filterbar is not None
    assert filterbar.has_attr("open")


def import_web_app(tmp_path, monkeypatch):
    for module_name in ("admin", "auth", "app"):
        sys.modules.pop(module_name, None)
    monkeypatch.setenv("DB_PATH", str(tmp_path / "jobs.db"))
    monkeypatch.setenv("SITE_URL", "https://zimjobs.example")
    module = importlib.import_module("app")
    module.app.config.update(TESTING=True)
    with module.app.app_context():
        module.get_db().execute("DELETE FROM jobs")
        module.get_db().commit()
    return module


def insert_job(web_app, **overrides):
    values = {
        "title": "Example Role",
        "company": "Example Co",
        "location": "Harare",
        "category": "Private Sector",
        "summary": "A complete role description for a real open job.",
        "apply_url": "https://example.org/apply",
        "featured": 0,
        "created_at": "2026-06-01 09:00:00",
        "posted_at": "2026-06-01",
        "expires_at": None,
        "employment_type": None,
        "salary_range": None,
        "remote_status": None,
        "job_description": None,
        "requirements": None,
    }
    values.update(overrides)
    cols = list(values)
    placeholders = ",".join("?" for _ in cols)
    with web_app.app.app_context():
        cur = web_app.get_db().execute(
            f"INSERT INTO jobs({','.join(cols)}) VALUES({placeholders})",
            [values[col] for col in cols],
        )
        web_app.get_db().commit()
        return cur.lastrowid


def first_jobposting_schema(page_html: str):
    soup = BeautifulSoup(page_html, "html.parser")
    for script in soup.find_all("script", attrs={"type": "application/ld+json"}):
        payload = json.loads(script.string)
        if payload.get("@type") == "JobPosting":
            return payload
    raise AssertionError("No JobPosting JSON-LD found")
