from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRAPER_SRC = REPO_ROOT / "zimjobs_scraper" / "src"
if str(SCRAPER_SRC) not in sys.path:
    sys.path.insert(0, str(SCRAPER_SRC))

from zimjobs_scraper import indexnow


class FakeResponse:
    def __init__(self, status_code=200, text="ok"):
        self.status_code = status_code
        self.text = text


def test_single_submission_normalizes_relative_public_url(monkeypatch):
    calls = []
    monkeypatch.setenv("BASE_URL", "https://zimjobs.online")
    monkeypatch.setenv("INDEXNOW_KEY", "test-key-123")

    def fake_get(url, params, timeout):
        calls.append({"url": url, "params": params, "timeout": timeout})
        return FakeResponse(status_code=202, text="accepted")

    monkeypatch.setattr(indexnow.requests, "get", fake_get)

    assert indexnow.submit_url_to_indexnow("/job/12/finance-officer") is True
    assert calls == [{
        "url": "https://api.indexnow.org/indexnow",
        "params": {
            "url": "https://zimjobs.online/job/12/finance-officer",
            "key": "test-key-123",
        },
        "timeout": 10,
    }]


def test_bulk_submission_deduplicates_filters_and_chunks(monkeypatch):
    calls = []
    monkeypatch.setenv("BASE_URL", "https://zimjobs.online")
    monkeypatch.setenv("INDEXNOW_KEY", "test-key-123")
    monkeypatch.setattr(indexnow, "INDEXNOW_MAX_BULK_URLS", 2)

    def fake_post(url, json, timeout):
        calls.append({"url": url, "json": json, "timeout": timeout})
        return FakeResponse(status_code=200, text="ok")

    monkeypatch.setattr(indexnow.requests, "post", fake_post)

    assert indexnow.submit_urls_to_indexnow([
        "/job/1/one",
        "/job/1/one",
        "https://zimjobs.online/job/2/two",
        "/job/3/three",
        "/admin/jobs",
        "/job/4/four?utm_source=test",
        "https://other.example/job/5/five",
    ]) is True

    assert len(calls) == 2
    assert calls[0]["json"] == {
        "host": "zimjobs.online",
        "key": "test-key-123",
        "urlList": [
            "https://zimjobs.online/job/1/one",
            "https://zimjobs.online/job/2/two",
        ],
    }
    assert calls[1]["json"]["urlList"] == ["https://zimjobs.online/job/3/three"]


def test_missing_indexnow_key_skips_without_network(monkeypatch):
    monkeypatch.setenv("BASE_URL", "https://zimjobs.online")
    monkeypatch.delenv("INDEXNOW_KEY", raising=False)

    def fail_get(*_args, **_kwargs):
        raise AssertionError("network should not be called without INDEXNOW_KEY")

    monkeypatch.setattr(indexnow.requests, "get", fail_get)

    assert indexnow.submit_url_to_indexnow("/job/12/finance-officer") is False
