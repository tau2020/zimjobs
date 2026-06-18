from __future__ import annotations

import json
import logging
import os
import re
from datetime import datetime, timezone
from typing import Iterable
from urllib.parse import urlsplit, urlunsplit

import requests

log = logging.getLogger(__name__)

INDEXNOW_ENDPOINT = "https://api.indexnow.org/indexnow"
INDEXNOW_MAX_BULK_URLS = 10000
INDEXNOW_SUCCESS_CODES = {200, 202}
DEFAULT_BASE_URL = "https://zimjobs.online"
REQUEST_TIMEOUT_SECONDS = 10

PRIVATE_PATH_PREFIXES = (
    "/admin",
    "/account",
    "/login",
    "/register",
    "/logout",
    "/dashboard",
    "/api/",
    "/internal/",
    "/preview/",
    "/staging/",
    "/affiliate/",
    "/alerts/",
    "/health",
    "/healthz/",
)


def indexnow_key() -> str:
    return os.getenv("INDEXNOW_KEY", "").strip()


def base_url() -> str:
    return os.getenv("BASE_URL", DEFAULT_BASE_URL).strip().rstrip("/") or DEFAULT_BASE_URL


def base_host() -> str:
    return urlsplit(base_url()).netloc.lower()


def slug(text: str | None) -> str:
    return re.sub(r"[^a-z0-9]+", "-", str(text or "").lower()).strip("-")


def canonical_job_path(job_id: int | str, title: str | None) -> str:
    return f"/job/{job_id}/{slug(title)}"


def canonical_job_url(job_id: int | str, title: str | None) -> str:
    return normalize_indexnow_url(canonical_job_path(job_id, title)) or ""


def _response_snippet(response: requests.Response, limit: int = 500) -> str:
    text = response.text or ""
    return text[:limit]


def _log(level: int, event: str, **fields) -> None:
    payload = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        **fields,
    }
    log.log(level, "%s %s", event, json.dumps(payload, sort_keys=True))


def _is_blocked_path(path: str) -> bool:
    normalized = "/" + path.lstrip("/")
    return any(
        normalized == prefix.rstrip("/") or normalized.startswith(prefix)
        for prefix in PRIVATE_PATH_PREFIXES
    )


def normalize_indexnow_url(url_or_path: str | None) -> str | None:
    raw = str(url_or_path or "").strip()
    if not raw:
        return None

    site = base_url()
    site_parts = urlsplit(site)
    if site_parts.scheme not in {"http", "https"} or not site_parts.netloc:
        _log(
            logging.WARNING,
            "indexnow_skipped",
            submission_type="normalize",
            error_message="BASE_URL must be an absolute http or https URL",
        )
        return None

    if raw.startswith(("http://", "https://")):
        parts = urlsplit(raw)
    else:
        path = raw if raw.startswith("/") else f"/{raw}"
        parts = urlsplit(f"{site}{path}")

    if parts.scheme not in {"http", "https"} or not parts.netloc:
        return None
    if parts.netloc.lower() != site_parts.netloc.lower():
        _log(
            logging.INFO,
            "indexnow_skipped",
            submission_type="normalize",
            submitted_url=raw,
            error_message="URL host does not match BASE_URL",
        )
        return None
    if parts.query or parts.fragment:
        _log(
            logging.INFO,
            "indexnow_skipped",
            submission_type="normalize",
            submitted_url=raw,
            error_message="URL is not canonical because it has a query string or fragment",
        )
        return None

    path = parts.path or "/"
    if _is_blocked_path(path):
        _log(
            logging.INFO,
            "indexnow_skipped",
            submission_type="normalize",
            submitted_url=raw,
            error_message="URL path is private, noindex, or blocked by robots.txt",
        )
        return None

    return urlunsplit((site_parts.scheme, site_parts.netloc, path, "", ""))


def unique_indexnow_urls(urls: Iterable[str | None]) -> list[str]:
    unique: list[str] = []
    seen: set[str] = set()
    for raw in urls:
        normalized = normalize_indexnow_url(raw)
        if normalized and normalized not in seen:
            seen.add(normalized)
            unique.append(normalized)
    return unique


def submit_url_to_indexnow(url_or_path: str | None) -> bool:
    url = normalize_indexnow_url(url_or_path)
    if not url:
        return False

    key = indexnow_key()
    if not key:
        _log(
            logging.INFO,
            "indexnow_skipped",
            submission_type="single",
            submitted_url=url,
            error_message="INDEXNOW_KEY is not configured",
        )
        return False

    try:
        response = requests.get(
            INDEXNOW_ENDPOINT,
            params={"url": url, "key": key},
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
    except requests.RequestException as exc:
        _log(
            logging.WARNING,
            "indexnow_failed",
            submission_type="single",
            submitted_url=url,
            error_message=str(exc),
        )
        return False

    success = response.status_code in INDEXNOW_SUCCESS_CODES
    _log(
        logging.INFO if success else logging.WARNING,
        "indexnow_submitted" if success else "indexnow_failed",
        submission_type="single",
        submitted_url=url,
        status_code=response.status_code,
        response_snippet=_response_snippet(response),
    )
    return success


def _chunks(urls: list[str], size: int) -> Iterable[list[str]]:
    for start in range(0, len(urls), size):
        yield urls[start:start + size]


def submit_urls_to_indexnow(urls: Iterable[str | None]) -> bool:
    unique_urls = unique_indexnow_urls(urls)
    if not unique_urls:
        _log(
            logging.INFO,
            "indexnow_skipped",
            submission_type="bulk",
            error_message="No changed canonical public URLs to submit",
        )
        return False

    key = indexnow_key()
    if not key:
        _log(
            logging.INFO,
            "indexnow_skipped",
            submission_type="bulk",
            url_count=len(unique_urls),
            error_message="INDEXNOW_KEY is not configured",
        )
        return False

    all_success = True
    for chunk in _chunks(unique_urls, INDEXNOW_MAX_BULK_URLS):
        payload = {
            "host": base_host(),
            "key": key,
            "urlList": chunk,
        }
        try:
            response = requests.post(
                INDEXNOW_ENDPOINT,
                json=payload,
                timeout=REQUEST_TIMEOUT_SECONDS,
            )
        except requests.RequestException as exc:
            all_success = False
            _log(
                logging.WARNING,
                "indexnow_failed",
                submission_type="bulk",
                submitted_urls=chunk[:20],
                url_count=len(chunk),
                error_message=str(exc),
            )
            continue

        success = response.status_code in INDEXNOW_SUCCESS_CODES
        all_success = all_success and success
        _log(
            logging.INFO if success else logging.WARNING,
            "indexnow_submitted" if success else "indexnow_failed",
            submission_type="bulk",
            submitted_urls=chunk[:20],
            url_count=len(chunk),
            status_code=response.status_code,
            response_snippet=_response_snippet(response),
        )

    return all_success


def submit_changed_urls_to_indexnow(urls: Iterable[str | None]) -> bool:
    unique_urls = unique_indexnow_urls(urls)
    if len(unique_urls) == 1:
        return submit_url_to_indexnow(unique_urls[0])
    return submit_urls_to_indexnow(unique_urls)
