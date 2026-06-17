from __future__ import annotations

import importlib
import sys
from pathlib import Path

from bs4 import BeautifulSoup

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


def import_web_app(tmp_path, monkeypatch):
    for module_name in ("admin", "auth", "app"):
        sys.modules.pop(module_name, None)
    monkeypatch.setenv("DB_PATH", str(tmp_path / "jobs.db"))
    monkeypatch.setenv("SITE_URL", "https://zimjobs.example")
    monkeypatch.setenv("ADMIN_TOKEN", "test-admin-token")
    module = importlib.import_module("app")
    module.app.config.update(TESTING=True)
    module.RATE_LIMIT_BUCKETS.clear()
    with module.app.app_context():
        module.get_db().execute("DELETE FROM jobs")
        module.get_db().commit()
    return module


def insert_job(web_app, **overrides):
    values = {
        "title": "Finance Officer",
        "company": "Example NGO",
        "location": "Harare",
        "category": "Private Sector",
        "summary": "Manage finance records, donor reports, compliance checks, and monthly reconciliations.",
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


def csrf_token(html):
    soup = BeautifulSoup(html, "html.parser")
    token = soup.find("input", attrs={"name": "_csrf"})
    assert token is not None
    return token["value"]


def test_security_headers_are_set(tmp_path, monkeypatch):
    web_app = import_web_app(tmp_path, monkeypatch)
    response = web_app.app.test_client().get("/")

    assert response.status_code == 200
    assert "default-src 'self'" in response.headers["Content-Security-Policy"]
    assert response.headers["X-Frame-Options"] == "DENY"
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert response.headers["Referrer-Policy"] == "strict-origin-when-cross-origin"
    assert "Strict-Transport-Security" in response.headers


def test_malformed_page_parameter_returns_400(tmp_path, monkeypatch):
    web_app = import_web_app(tmp_path, monkeypatch)

    response = web_app.app.test_client().get("/?page=not-a-number")

    assert response.status_code == 400
    assert "Server error" not in response.get_data(as_text=True)


def test_search_injection_payload_does_not_break_query(tmp_path, monkeypatch):
    web_app = import_web_app(tmp_path, monkeypatch)
    insert_job(web_app, title="Finance Officer")

    response = web_app.app.test_client().get("/?q=%27%20OR%201%3D1--")

    assert response.status_code == 200
    assert "Server error" not in response.get_data(as_text=True)


def test_scraped_xss_payloads_render_as_text(tmp_path, monkeypatch):
    web_app = import_web_app(tmp_path, monkeypatch)
    job_id = insert_job(
        web_app,
        title="Finance <script>alert(1)</script> Officer",
        company='Example <img src=x onerror="alert(1)"> NGO',
        summary="Review grants. <script>alert(2)</script>",
        job_description="Review grants. <script>alert(2)</script>",
    )

    response = web_app.app.test_client().get(f"/job/{job_id}/finance-officer")
    html = response.get_data(as_text=True)

    assert response.status_code == 200
    assert "<script>alert(1)</script>" not in html
    assert "<script>alert(2)</script>" not in html
    assert "<img src=x" not in html
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html or "\\u003cscript" in html


def test_unsafe_apply_url_is_not_rendered_as_link(tmp_path, monkeypatch):
    web_app = import_web_app(tmp_path, monkeypatch)
    job_id = insert_job(web_app, apply_url="javascript:alert(1)")

    response = web_app.app.test_client().get(f"/job/{job_id}/finance-officer")
    html = response.get_data(as_text=True)

    assert response.status_code == 200
    assert 'href="javascript:alert(1)"' not in html
    assert "invalid apply link" in html


def test_resend_sender_posts_expected_transactional_payload(tmp_path, monkeypatch):
    monkeypatch.setenv("RESEND_API_KEY", "re_test_key")
    monkeypatch.setenv("RESEND_FROM_EMAIL", "ZimJobs Hub <jobs@example.com>")
    monkeypatch.setenv("RESEND_REPLY_TO", "support@example.com")
    web_app = import_web_app(tmp_path, monkeypatch)
    calls = []

    class Response:
        status_code = 200
        text = '{"id":"email_123"}'

        def json(self):
            return {"id": "email_123"}

    def fake_post(url, json, headers, timeout):
        calls.append({
            "url": url,
            "json": json,
            "headers": headers,
            "timeout": timeout,
        })
        return Response()

    monkeypatch.setattr(web_app.requests, "post", fake_post)

    sent = web_app.send_transactional_email(
        "reader@example.com",
        "Test subject",
        "Plain text",
        "<p>HTML</p>",
        tags={"type": "test_email"},
        idempotency_key="test email/1",
    )

    assert sent is True
    assert calls[0]["url"] == "https://api.resend.com/emails"
    assert calls[0]["headers"]["Authorization"] == "Bearer re_test_key"
    assert calls[0]["headers"]["Idempotency-Key"] == "test_email_1"
    assert calls[0]["timeout"] == 10
    assert calls[0]["json"]["from"] == "ZimJobs Hub <jobs@example.com>"
    assert calls[0]["json"]["to"] == ["reader@example.com"]
    assert calls[0]["json"]["subject"] == "Test subject"
    assert calls[0]["json"]["text"] == "Plain text"
    assert calls[0]["json"]["html"] == "<p>HTML</p>"
    assert calls[0]["json"]["reply_to"] == "support@example.com"
    assert calls[0]["json"]["tags"] == [{"name": "type", "value": "test_email"}]


def test_email_alert_signup_stores_preferences_and_unsubscribe_token(tmp_path, monkeypatch):
    monkeypatch.setenv("TRANSACTIONAL_EMAILS_ENABLED", "0")
    web_app = import_web_app(tmp_path, monkeypatch)
    client = web_app.app.test_client()
    token = csrf_token(client.get("/").get_data(as_text=True))

    response = client.post(
        "/alerts/email",
        data={
            "_csrf": token,
            "email": "reader@example.com",
            "category": "Private Sector",
            "location": "Harare",
            "source": "test",
            "frequency": "weekly",
        },
    )

    assert response.status_code == 302
    with web_app.app.app_context():
        row = web_app.get_db().execute(
            "SELECT email, category, location, source, frequency, active, unsubscribe_token "
            "FROM email_alerts WHERE email=?",
            ("reader@example.com",),
        ).fetchone()

    assert row["category"] == "Private Sector"
    assert row["location"] == "Harare"
    assert row["source"] == "test"
    assert row["frequency"] == "weekly"
    assert row["active"] == 1
    assert len(row["unsubscribe_token"]) >= 20


def test_email_alert_unsubscribe_deactivates_alert(tmp_path, monkeypatch):
    monkeypatch.setenv("TRANSACTIONAL_EMAILS_ENABLED", "0")
    web_app = import_web_app(tmp_path, monkeypatch)
    client = web_app.app.test_client()
    csrf = csrf_token(client.get("/").get_data(as_text=True))
    client.post(
        "/alerts/email",
        data={"_csrf": csrf, "email": "reader@example.com"},
    )

    with web_app.app.app_context():
        token = web_app.get_db().execute(
            "SELECT unsubscribe_token FROM email_alerts WHERE email=?",
            ("reader@example.com",),
        ).fetchone()["unsubscribe_token"]

    response = client.get(f"/alerts/email/unsubscribe/{token}")

    assert response.status_code == 302
    with web_app.app.app_context():
        row = web_app.get_db().execute(
            "SELECT active, unsubscribed_at FROM email_alerts WHERE email=?",
            ("reader@example.com",),
        ).fetchone()

    assert row["active"] == 0
    assert row["unsubscribed_at"]


def test_search_falls_back_to_like_when_fts_query_fails(tmp_path, monkeypatch):
    web_app = import_web_app(tmp_path, monkeypatch)
    insert_job(web_app, title="Finance Officer")
    with web_app.app.app_context():
        web_app.get_db().execute("DROP TABLE jobs_fts")
        web_app.get_db().commit()

    response = web_app.app.test_client().get("/?q=Finance")
    html = response.get_data(as_text=True)

    assert response.status_code == 200
    assert "Finance Officer" in html
    assert "Server error" not in html


def test_post_requires_csrf_even_with_valid_form_token(tmp_path, monkeypatch):
    web_app = import_web_app(tmp_path, monkeypatch)
    client = web_app.app.test_client()
    payload = {
        "title": "Operations Assistant",
        "company": "Example Retail",
        "location": "Harare",
        "category": "Private Sector",
        "summary": "Support stock control, supplier follow-up, and store administration.",
        "apply_url": "https://example.org/apply",
        "token": "test-admin-token",
    }

    blocked = client.post("/post", data=payload)
    token = csrf_token(client.get("/post").get_data(as_text=True))
    allowed = client.post("/post", data={**payload, "_csrf": token})

    assert blocked.status_code == 400
    assert allowed.status_code == 302


def test_post_api_allows_admin_header_without_csrf(tmp_path, monkeypatch):
    web_app = import_web_app(tmp_path, monkeypatch)
    payload = {
        "title": "Operations Assistant",
        "company": "Example Retail",
        "location": "Harare",
        "category": "Private Sector",
        "summary": "Support stock control, supplier follow-up, and store administration.",
        "apply_url": "https://example.org/apply",
    }

    response = web_app.app.test_client().post(
        "/post",
        data=payload,
        headers={"X-Admin-Token": "test-admin-token"},
    )

    assert response.status_code == 302


def test_admin_routes_require_login(tmp_path, monkeypatch):
    web_app = import_web_app(tmp_path, monkeypatch)

    response = web_app.app.test_client().get("/admin/")

    assert response.status_code == 302
    assert "/login" in response.headers["Location"]


def test_login_rate_limit_blocks_repeated_attempts(tmp_path, monkeypatch):
    web_app = import_web_app(tmp_path, monkeypatch)
    client = web_app.app.test_client()

    for _ in range(10):
        assert client.post("/login", data={}).status_code == 400
    assert client.post("/login", data={}).status_code == 429
