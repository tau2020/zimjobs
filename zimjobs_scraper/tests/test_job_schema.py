from __future__ import annotations

import importlib
import json
import sys
from datetime import date
from pathlib import Path
from xml.etree import ElementTree

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
    assert schema["directApply"] is False
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
    assert active_schema["directApply"] is False
    assert active_schema["baseSalary"]["value"]["value"] == 900
    assert listing_response.status_code == 200
    assert "JobPosting" not in listing_response.get_data(as_text=True)
    assert expired_response.status_code == 410
    assert "JobPosting" not in expired_response.get_data(as_text=True)


def test_sitemap_robots_and_canonical_rules(tmp_path, monkeypatch):
    web_app = import_web_app(tmp_path, monkeypatch)
    active_id = insert_job(
        web_app,
        title="Harare Finance Officer",
        company="Example NGO",
        location="Harare",
        category="NGO & Development",
        summary="Manage grants and programme finance.",
        expires_at="2099-12-31",
    )
    expired_id = insert_job(
        web_app,
        title="Expired Finance Officer",
        company="Example NGO",
        location="Harare",
        category="NGO & Development",
        summary="Closed role.",
        expires_at="2020-01-01",
    )
    utility_id = insert_job(
        web_app,
        title="Cookies Policy",
        company="Example Site",
        location="Harare",
        category="Private Sector",
        summary="A scraped utility page that is not a real job.",
        apply_url="https://example.org/cookies-policy",
    )

    client = web_app.app.test_client()
    sitemap_response = client.get("/sitemap.xml")
    robots_response = client.get("/robots.txt")
    home_response = client.get("/")
    search_response = client.get("/?q=finance")
    utility_response = client.get(f"/job/{utility_id}/cookies-policy")
    admin_response = client.get("/admin/")

    xml_root = ElementTree.fromstring(sitemap_response.get_data(as_text=True))
    ns = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}
    locs = [loc.text for loc in xml_root.findall(".//sm:loc", ns)]
    robots_text = robots_response.get_data(as_text=True)

    assert sitemap_response.status_code == 200
    assert sitemap_response.mimetype == "application/xml"
    assert f"https://zimjobs.example/job/{active_id}/harare-finance-officer" in locs
    assert f"https://zimjobs.example/job/{expired_id}/expired-finance-officer" not in locs
    assert f"https://zimjobs.example/job/{utility_id}/cookies-policy" not in locs
    assert "https://zimjobs.example/jobs/harare/" in locs
    assert "https://zimjobs.example/login" not in locs
    assert "https://zimjobs.example/admin/" not in locs
    assert robots_response.status_code == 200
    assert robots_response.mimetype == "text/plain"
    assert "Sitemap: https://zimjobs.example/sitemap.xml" in robots_text
    assert "Disallow: /admin/" in robots_text
    assert "Disallow: /api/" in robots_text
    assert "Disallow: /preview/" in robots_text
    assert "Disallow: /health" in robots_text
    assert "Disallow: /static/" not in robots_text
    assert canonical_href(home_response.get_data(as_text=True)) == "https://zimjobs.example/"
    assert robots_meta(search_response.get_data(as_text=True)) == "noindex,follow"
    assert canonical_href(search_response.get_data(as_text=True)) == "https://zimjobs.example/"
    assert utility_response.status_code == 404
    assert admin_response.status_code == 302


def test_landing_page_renders_filtered_jobs_alert_cta_and_tracking(tmp_path, monkeypatch):
    web_app = import_web_app(tmp_path, monkeypatch)
    harare_id = insert_job(
        web_app,
        title="Harare Finance Officer",
        company="Example NGO",
        location="Harare",
        category="NGO & Development",
        summary="Manage grants and programme finance.",
        expires_at="2099-12-31",
    )
    insert_job(
        web_app,
        title="Bulawayo Accountant",
        company="Example Retail",
        location="Bulawayo",
        category="Private Sector",
        summary="Manage store accounts.",
    )

    response = web_app.app.test_client().get("/jobs/harare/")
    html = response.get_data(as_text=True)
    soup = BeautifulSoup(html, "html.parser")

    assert response.status_code == 200
    assert "Harare Finance Officer" in html
    assert "Bulawayo Accountant" not in html
    assert canonical_href(html) == "https://zimjobs.example/jobs/harare/"
    assert robots_meta(html) is None
    assert soup.find("form", attrs={"data-track-event": "email_alert_signup"})
    apply_link = soup.find("a", attrs={"data-track-event": "apply_click_out"})
    assert apply_link["data-track-job-id"] == str(harare_id)


def test_job_detail_has_growth_tracking_hooks_and_sticky_apply(tmp_path, monkeypatch):
    web_app = import_web_app(tmp_path, monkeypatch)
    job_id = insert_job(
        web_app,
        title="Finance Officer",
        company="Example NGO",
        location="Harare",
        category="NGO & Development",
        summary="Manage programme finance.",
    )

    response = web_app.app.test_client().get(f"/job/{job_id}/finance-officer")
    html = response.get_data(as_text=True)
    soup = BeautifulSoup(html, "html.parser")
    schema = first_jobposting_schema(html)

    assert response.status_code == 200
    assert soup.title.string == "Finance Officer - Example NGO | ZimJobs Hub"
    assert meta_content(html, "description") == (
        "Finance Officer at Example NGO - Harare. Manage programme finance."
    )
    assert canonical_href(html) == f"https://zimjobs.example/job/{job_id}/finance-officer"
    assert meta_property(html, "og:type") == "article"
    assert meta_property(html, "og:url") == f"https://zimjobs.example/job/{job_id}/finance-officer"
    assert meta_property(html, "og:title") == "Finance Officer - Example NGO"
    assert meta_content(html, "twitter:card") == "summary"
    assert schema["@type"] == "JobPosting"
    assert schema["url"] == f"https://zimjobs.example/job/{job_id}/finance-officer"
    assert soup.find(attrs={"data-track-view": "job_view"})
    assert soup.find("a", attrs={"data-track-event": "apply_click_out", "data-track-source": "job_detail"})
    assert soup.find("a", attrs={"data-track-event": "apply_click_out", "data-track-source": "sticky_apply"})
    assert soup.find("a", attrs={"data-track-event": "whatsapp_channel_join_click"})


def test_job_detail_redirects_duplicate_slug_urls_to_canonical(tmp_path, monkeypatch):
    web_app = import_web_app(tmp_path, monkeypatch)
    job_id = insert_job(
        web_app,
        title="Finance Officer",
        company="Example NGO",
        location="Harare",
        summary="Manage programme finance.",
    )

    client = web_app.app.test_client()
    no_slug_response = client.get(f"/job/{job_id}")
    wrong_slug_response = client.get(f"/job/{job_id}/old-title")

    assert no_slug_response.status_code == 301
    assert no_slug_response.headers["Location"] == f"https://zimjobs.example/job/{job_id}/finance-officer"
    assert wrong_slug_response.status_code == 301
    assert wrong_slug_response.headers["Location"] == f"https://zimjobs.example/job/{job_id}/finance-officer"


def test_email_alert_signup_persists_subscription(tmp_path, monkeypatch):
    web_app = import_web_app(tmp_path, monkeypatch)
    sent = []
    monkeypatch.setattr(
        web_app,
        "send_email_alert_confirmation",
        lambda email, category="", location="": sent.append((email, category, location)) or True,
    )
    client = web_app.app.test_client()
    page = client.get("/jobs/harare/")
    soup = BeautifulSoup(page.get_data(as_text=True), "html.parser")
    token = soup.find("input", attrs={"name": "_csrf"})["value"]

    response = client.post("/alerts/email", data={
        "_csrf": token,
        "email": "reader@example.com",
        "category": "NGO & Development",
        "location": "Harare",
        "source": "landing_harare",
        "next": "/jobs/harare/",
    })

    with web_app.app.app_context():
        row = web_app.get_db().execute(
            "SELECT * FROM email_alerts WHERE email=?",
            ("reader@example.com",),
        ).fetchone()

    assert response.status_code == 302
    assert response.headers["Location"].endswith("/jobs/harare/?email_alert=success")
    assert row["category"] == "NGO & Development"
    assert row["location"] == "Harare"
    assert row["source"] == "landing_harare"
    assert sent == [("reader@example.com", "NGO & Development", "Harare")]


def test_homepage_filterbar_is_not_rendered(tmp_path, monkeypatch):
    web_app = import_web_app(tmp_path, monkeypatch)

    response = web_app.app.test_client().get("/")
    soup = BeautifulSoup(response.get_data(as_text=True), "html.parser")
    filterbar = soup.find("details", class_="filterbar")

    assert response.status_code == 200
    assert filterbar is None


def test_probable_merged_scraped_row_is_not_rendered(tmp_path, monkeypatch):
    web_app = import_web_app(tmp_path, monkeypatch)
    merged_id = insert_job(
        web_app,
        title="Finance Officer",
        company="Example NGO",
        location="Harare",
        summary=(
            "Job Title: Finance Officer\nManage donor finance and grant reports.\n"
            "Job Title: Operations Coordinator\nCoordinate procurement and operations.\n"
            "Job Title: HR Assistant\nMaintain recruitment records and onboarding files."
        ),
        job_description=(
            "Job Title: Finance Officer\nManage donor finance and grant reports.\n"
            "Job Title: Operations Coordinator\nCoordinate procurement and operations.\n"
            "Job Title: HR Assistant\nMaintain recruitment records and onboarding files."
        ),
    )

    client = web_app.app.test_client()
    detail_response = client.get(f"/job/{merged_id}/finance-officer")
    listing_response = client.get("/")

    assert detail_response.status_code == 404
    assert "Operations Coordinator" not in listing_response.get_data(as_text=True)


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


def canonical_href(page_html: str):
    soup = BeautifulSoup(page_html, "html.parser")
    tag = soup.find("link", rel="canonical")
    return tag["href"] if tag else None


def robots_meta(page_html: str):
    soup = BeautifulSoup(page_html, "html.parser")
    tag = soup.find("meta", attrs={"name": "robots"})
    return tag["content"] if tag else None


def meta_content(page_html: str, name: str):
    soup = BeautifulSoup(page_html, "html.parser")
    tag = soup.find("meta", attrs={"name": name})
    return tag["content"] if tag else None


def meta_property(page_html: str, property_name: str):
    soup = BeautifulSoup(page_html, "html.parser")
    tag = soup.find("meta", attrs={"property": property_name})
    return tag["content"] if tag else None
