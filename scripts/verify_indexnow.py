#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from urllib.parse import urljoin, urlsplit
from xml.etree import ElementTree

import requests

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRAPER_SRC = REPO_ROOT / "zimjobs_scraper" / "src"
if str(SCRAPER_SRC) not in sys.path:
    sys.path.insert(0, str(SCRAPER_SRC))

from zimjobs_scraper.indexnow import (  # noqa: E402
    base_url,
    indexnow_key,
    submit_url_to_indexnow,
    submit_urls_to_indexnow,
)


def fetch_first_job_url(site_url: str) -> str:
    response = requests.get(urljoin(site_url.rstrip("/") + "/", "sitemap.xml"), timeout=15)
    response.raise_for_status()
    root = ElementTree.fromstring(response.text)
    ns = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}
    for loc in root.findall(".//sm:loc", ns):
        url = (loc.text or "").strip()
        if "/job/" in urlsplit(url).path:
            return url
    raise RuntimeError("No public /job/ URL found in sitemap.xml. Pass --url with a real job URL.")


def verify_key_file(site_url: str, key: str) -> None:
    response = requests.get(f"{site_url.rstrip('/')}/{key}.txt", timeout=15)
    content_type = response.headers.get("Content-Type", "")
    if response.status_code != 200:
        raise RuntimeError(f"Key file returned HTTP {response.status_code}")
    if response.text != key:
        raise RuntimeError("Key file body did not exactly match INDEXNOW_KEY")
    if "text/plain" not in content_type.lower():
        raise RuntimeError(f"Key file Content-Type was not text/plain: {content_type}")
    print("key_file: ok status=200 path=/<INDEXNOW_KEY>.txt")


def verify_missing_key_skip(job_url: str) -> None:
    original = os.environ.pop("INDEXNOW_KEY", None)
    try:
        skipped = not submit_url_to_indexnow(job_url)
    finally:
        if original is not None:
            os.environ["INDEXNOW_KEY"] = original
    if not skipped:
        raise RuntimeError("Missing INDEXNOW_KEY did not skip safely")
    print("missing_key_skip: ok")


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify ZimJobs IndexNow configuration and submission.")
    parser.add_argument("--base-url", default=base_url(), help="Public site base URL")
    parser.add_argument("--url", default="", help="Real public job URL to submit; defaults to first /job/ URL in sitemap.xml")
    args = parser.parse_args()

    os.environ["BASE_URL"] = args.base_url.rstrip("/")
    key = indexnow_key()
    if not key:
        print("INDEXNOW_KEY: missing")
        verify_missing_key_skip(args.url or f"{args.base_url.rstrip('/')}/job/0/indexnow-test")
        return 1

    print("INDEXNOW_KEY: configured")
    verify_key_file(args.base_url, key)

    job_url = args.url.strip() or fetch_first_job_url(args.base_url)
    print(f"job_url: {job_url}")

    verify_missing_key_skip(job_url)

    if not submit_url_to_indexnow(job_url):
        raise RuntimeError("Single URL submission failed")
    print("single_submission: ok")

    if not submit_urls_to_indexnow([job_url, job_url]):
        raise RuntimeError("Bulk URL submission failed")
    print("bulk_submission: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
