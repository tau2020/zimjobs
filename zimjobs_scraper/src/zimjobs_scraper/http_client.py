from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass
from functools import lru_cache
from typing import Iterable
from urllib.parse import urlparse
from urllib.robotparser import RobotFileParser

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

log = logging.getLogger(__name__)


@dataclass(slots=True)
class HttpSettings:
    timeout_seconds: int = int(os.getenv("HTTP_TIMEOUT_SECONDS", "20"))
    delay_seconds: float = float(os.getenv("REQUEST_DELAY_SECONDS", "2"))
    user_agent: str = os.getenv(
        "USER_AGENT",
        "ZimJobsBot/2.0 (+https://zimjobs.online; contact: makombemt@gmail.com)",
    )
    robots_strict: bool = os.getenv("ROBOTS_STRICT", "0") == "1"


def _origin(url: str) -> str:
    parsed = urlparse(url)
    return f"{parsed.scheme}://{parsed.netloc}"


@lru_cache(maxsize=64)
def _robot_parser(origin: str, user_agent: str) -> RobotFileParser:
    robots_url = origin.rstrip("/") + "/robots.txt"
    parser = RobotFileParser()
    parser.set_url(robots_url)
    try:
        parser.read()
        log.info("robots_checked", extra={"url": robots_url, "status": "ok"})
    except Exception as exc:  # network and malformed robots should not crash the scraper
        log.warning("robots_unavailable", extra={"url": robots_url, "status": str(exc)})
    return parser


class HttpClient:
    def __init__(self, settings: HttpSettings | None = None):
        self.settings = settings or HttpSettings()
        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": self.settings.user_agent,
                "Accept": "text/html,application/xhtml+xml,application/xml,application/rss+xml,application/json;q=0.9,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.9",
            }
        )
        retry = Retry(
            total=3,
            connect=3,
            read=3,
            status=3,
            backoff_factor=1,
            status_forcelist=(429, 500, 502, 503, 504),
            allowed_methods=("GET", "HEAD"),
            raise_on_status=False,
        )
        adapter = HTTPAdapter(max_retries=retry)
        self.session.mount("http://", adapter)
        self.session.mount("https://", adapter)
        self._last_request_at = 0.0

    def allowed_by_robots(self, url: str) -> bool:
        parser = _robot_parser(_origin(url), self.settings.user_agent)
        try:
            allowed = parser.can_fetch(self.settings.user_agent, url)
        except Exception as exc:
            log.warning("robots_check_failed", extra={"url": url, "status": str(exc)})
            return not self.settings.robots_strict
        if not allowed:
            log.warning("robots_disallow", extra={"url": url, "status": "disallowed"})
        return allowed

    def get(self, url: str) -> str | None:
        if not self.allowed_by_robots(url):
            return None
        elapsed = time.monotonic() - self._last_request_at
        if elapsed < self.settings.delay_seconds:
            time.sleep(self.settings.delay_seconds - elapsed)
        try:
            response = self.session.get(url, timeout=self.settings.timeout_seconds)
            self._last_request_at = time.monotonic()
            log.info("http_get", extra={"url": url, "status": response.status_code})
            if response.status_code >= 400:
                return None
            return response.text
        except requests.RequestException as exc:
            log.warning("http_error", extra={"url": url, "status": str(exc)})
            return None

    def get_many(self, urls: Iterable[str]) -> dict[str, str]:
        output: dict[str, str] = {}
        for url in urls:
            html = self.get(url)
            if html:
                output[url] = html
        return output
