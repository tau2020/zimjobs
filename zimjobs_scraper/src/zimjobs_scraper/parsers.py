from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Iterable
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup, Tag

from .models import RawJob
from .normalization import (
    clean_html_to_markdownish,
    clean_text,
    extract_company_from_text,
    extract_role_from_text,
    find_deadline,
    looks_like_real_role,
    normalize_url,
    parse_date,
)

log = logging.getLogger(__name__)


@dataclass(slots=True)
class SourceConfig:
    name: str
    type: str
    start_urls: list[str]
    enabled: bool = True
    max_pages: int = 1
    max_detail_pages: int = 40
    default_location: str = "Zimbabwe"
    default_category: str = "Other"
    allowed_locations: list[str] = field(default_factory=list)
    skip_expired: bool = True

    @classmethod
    def from_dict(cls, data: dict) -> "SourceConfig":
        return cls(
            name=data["name"],
            type=data.get("type", "generic"),
            enabled=bool(data.get("enabled", True)),
            start_urls=list(data.get("start_urls", [])),
            max_pages=int(data.get("max_pages", 1)),
            max_detail_pages=int(data.get("max_detail_pages", data.get("max_detail_pages", 40))),
            default_location=data.get("default_location", "Zimbabwe"),
            default_category=data.get("default_category", "Other"),
            allowed_locations=list(data.get("allowed_locations", [])),
            skip_expired=bool(data.get("skip_expired", True)),
        )


class BaseParser:
    def __init__(self, config: SourceConfig):
        self.config = config

    def list_job_urls(self, html: str, base_url: str) -> list[str]:
        raise NotImplementedError

    def parse_detail(self, html: str, url: str) -> RawJob | None:
        raise NotImplementedError

    def _soup(self, html: str) -> BeautifulSoup:
        return BeautifulSoup(html, "html.parser")

    def _meta(self, soup: BeautifulSoup, *names: str) -> str:
        for name in names:
            tag = soup.find("meta", attrs={"property": name}) or soup.find("meta", attrs={"name": name})
            if tag and tag.get("content"):
                return clean_text(tag["content"])
        return ""

    def _json_ld_jobs(self, soup: BeautifulSoup, url: str) -> list[RawJob]:
        jobs: list[RawJob] = []
        for script in soup.find_all("script", attrs={"type": "application/ld+json"}):
            raw = script.string or script.get_text(" ")
            if not raw.strip():
                continue
            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                continue
            nodes = data if isinstance(data, list) else [data]
            for node in nodes:
                if isinstance(node, dict) and node.get("@graph"):
                    nodes.extend([n for n in node["@graph"] if isinstance(n, dict)])
                if not isinstance(node, dict):
                    continue
                if str(node.get("@type", "")).lower() != "jobposting":
                    continue
                org = node.get("hiringOrganization") or {}
                if isinstance(org, str):
                    company = org
                elif isinstance(org, dict):
                    company = org.get("name")
                else:
                    company = None
                location = None
                job_loc = node.get("jobLocation")
                if isinstance(job_loc, dict):
                    address = job_loc.get("address") or {}
                    if isinstance(address, dict):
                        location = ", ".join([clean_text(address.get(k)) for k in ("addressLocality", "addressRegion", "addressCountry") if address.get(k)])
                jobs.append(
                    RawJob(
                        source_name=self.config.name,
                        source_url=url,
                        title=node.get("title"),
                        company=company,
                        location=location,
                        summary=clean_text(node.get("description")),
                        description_html=node.get("description"),
                        apply_url=normalize_url(node.get("url") or url, url),
                        posted_at=parse_date(node.get("datePosted")),
                        expires_at=parse_date(node.get("validThrough")),
                        employment_type=node.get("employmentType"),
                        extra={"json_ld": True},
                    )
                )
        return jobs


class GenericParser(BaseParser):
    """Fallback parser for simple HTML pages and screenshots copied as HTML/text."""

    JOB_URL_PATTERNS = ("/jobs/", "/job/", "/careers/", "/career/", "/vacanc", "/202")

    def list_job_urls(self, html: str, base_url: str) -> list[str]:
        soup = self._soup(html)
        urls: list[str] = []
        seen: set[str] = set()
        for a in soup.find_all("a", href=True):
            text = clean_text(a.get_text(" "))
            href = normalize_url(a["href"], base_url)
            if not href or href in seen:
                continue
            path = urlparse(href).path.lower()
            if any(pat in path for pat in self.JOB_URL_PATTERNS) or re.search(r"job|vacanc|hiring|apply", text, flags=re.I):
                seen.add(href)
                urls.append(href)
        if not urls and re.search(r"job|vacanc|hiring|apply", clean_text(soup.get_text(" ")), flags=re.I):
            urls.append(base_url)
        return urls

    def parse_detail(self, html: str, url: str) -> RawJob | None:
        soup = self._soup(html)
        json_jobs = self._json_ld_jobs(soup, url)
        if json_jobs:
            return json_jobs[0]
        title = clean_text((soup.find("h1") or soup.find("title") or Tag(name="")).get_text(" "))
        if not title:
            return None
        main = soup.find("main") or soup.find("article") or soup.find("body") or soup
        description = clean_html_to_markdownish(str(main))
        company = self._meta(soup, "article:author", "author") or None
        apply_url = self._find_apply_url(soup, url) or url
        return RawJob(
            source_name=self.config.name,
            source_url=url,
            title=title,
            company=company,
            location=self.config.default_location,
            category=self.config.default_category,
            summary=description,
            description_html=str(main),
            apply_url=apply_url,
            expires_at=find_deadline(description),
        )

    def _find_apply_url(self, soup: BeautifulSoup, base_url: str) -> str | None:
        for a in soup.find_all("a", href=True):
            text = clean_text(a.get_text(" "))
            if re.search(r"apply|view full|job call|application", text, re.I):
                href = normalize_url(a["href"], base_url)
                if href:
                    return href
        return None


class ApplyNowParser(GenericParser):
    def list_job_urls(self, html: str, base_url: str) -> list[str]:
        soup = self._soup(html)
        urls: list[str] = []
        seen: set[str] = set()
        selectors = ["h2 a[href]", "h3 a[href]", "h4 a[href]", "h5 a[href]", "article a[href]", ".p-url[href]", "a[href]"]
        for selector in selectors:
            for a in soup.select(selector):
                text = clean_text(a.get_text(" "))
                href = normalize_url(a.get("href"), base_url)
                if not href or href in seen:
                    continue
                if "applynow.co.zw" not in urlparse(href).netloc:
                    continue
                path = urlparse(href).path
                if re.match(r"/20\d{2}/\d{2}/\d{2}/", path) or re.search(r"\b(hiring|vacancy|job|internship|consultant|apply)\b", text, flags=re.I):
                    seen.add(href)
                    urls.append(href)
        return urls

    def parse_detail(self, html: str, url: str) -> RawJob | None:
        soup = self._soup(html)
        title = clean_text((soup.find("h1") or soup.find("title") or Tag(name="")).get_text(" "))
        if not title:
            return None
        article = soup.find("article") or soup.find("main") or soup.find("body") or soup
        for bad_selector in ["nav", "header", "footer", ".share", ".comments", "script", "style", "form"]:
            for node in article.select(bad_selector):
                node.decompose()
        description = clean_html_to_markdownish(str(article))
        text = clean_text(description, max_spaces=False)
        company = self._extract_field(text, ["Company", "Organisation", "Organization", "Employer"]) or extract_company_from_text(title, text) or None
        # If the page headline is generic, try the body for a real role. If none is found, the validator will skip it.
        role_from_body = extract_role_from_text(text)
        if role_from_body and not looks_like_real_role(title):
            title = role_from_body
        location = self._extract_field(text, ["Job Location", "Opportunity Location", "Location"])
        employment_type = self._extract_field(text, ["Contract", "Contract Type", "Employment Type", "Job Type"])
        salary = self._extract_field(text, ["Salary", "Compensation", "Pay"])
        posted = self._meta(soup, "article:published_time") or self._meta(soup, "article:modified_time")
        updated_node = soup.find(string=re.compile(r"Last updated", re.I))
        if updated_node:
            posted = clean_text(updated_node)
        apply_url = self._find_apply_url(soup, url) or url
        return RawJob(
            source_name=self.config.name,
            source_url=url,
            title=title,
            company=company,
            location=location or self.config.default_location,
            category=self.config.default_category,
            summary=description,
            description_html=str(article),
            apply_url=apply_url,
            posted_at=parse_date(posted),
            expires_at=find_deadline(f"{title}\n{text}"),
            employment_type=employment_type,
            salary_range=salary,
        )

    def _extract_field(self, text: str, labels: Iterable[str]) -> str | None:
        # Only accept labelled fields at the start of a line. This avoids treating prose like
        # "The organisation is implementing..." as the organization/company name.
        for label in labels:
            pattern = rf"(?im)^\s*(?:[•\-*]\s*)?{re.escape(label)}\s*:\s*([^\n]+)"
            match = re.search(pattern, text)
            if match:
                value = clean_text(match.group(1))
                if value and len(value) < 140:
                    return value
        return None


class ImpactPoolParser(GenericParser):
    def list_job_urls(self, html: str, base_url: str) -> list[str]:
        soup = self._soup(html)
        urls: list[str] = []
        seen: set[str] = set()
        for a in soup.find_all("a", href=True):
            href = normalize_url(a["href"], base_url)
            if not href or href in seen:
                continue
            if "impactpool.org" not in urlparse(href).netloc:
                continue
            if re.search(r"/jobs/\d+", urlparse(href).path):
                seen.add(href)
                urls.append(href)
        return urls

    def parse_detail(self, html: str, url: str) -> RawJob | None:
        soup = self._soup(html)
        json_jobs = self._json_ld_jobs(soup, url)
        if json_jobs:
            return json_jobs[0]
        title = clean_text((soup.find("h1") or soup.find("title") or Tag(name="")).get_text(" "))
        if not title:
            return None
        main = soup.find("main") or soup.find("article") or soup.find("body") or soup
        for bad_selector in ["nav", "header", "footer", "script", "style", "form"]:
            for node in main.select(bad_selector):
                node.decompose()
        description = clean_html_to_markdownish(str(main))
        text = clean_text(description, max_spaces=False)
        company = None
        h1 = soup.find("h1")
        if h1:
            cursor = h1.find_next(string=True)
            if cursor:
                line = clean_text(cursor)
                if line and line != title and len(line) < 120:
                    company = line
        if not company:
            match = re.search(r"\n\s*([A-Z][A-Za-z0-9 &’'.,/-]{2,80})\s+(?:Harare|Remote|Zimbabwe|National|International)\b", text)
            company = clean_text(match.group(1)) if match else None
        apply_url = self._find_apply_url(soup, url) or self._find_external_apply_url(soup, url) or url
        location_match = re.search(r"\b(Remote\s*\|\s*)?(Harare|Zimbabwe|Home Based|Remote)(?:\s*\|\s*[A-Za-z ]+)?", text, re.I)
        return RawJob(
            source_name=self.config.name,
            source_url=url,
            title=title,
            company=company,
            location=clean_text(location_match.group(0)) if location_match else self.config.default_location,
            category=self.config.default_category,
            summary=description,
            description_html=str(main),
            apply_url=apply_url,
            expires_at=find_deadline(text),
        )

    def _find_external_apply_url(self, soup: BeautifulSoup, base_url: str) -> str | None:
        base_host = urlparse(base_url).netloc
        for a in soup.find_all("a", href=True):
            href = normalize_url(a["href"], base_url)
            if href and urlparse(href).netloc and urlparse(href).netloc != base_host:
                if not any(blocked in href for blocked in ["facebook.com", "linkedin.com", "instagram.com", "twitter.com"]):
                    return href
        return None


class SomewhereParser(GenericParser):
    """Parser for Somewhere candidate pages and RecruitCRM-backed job pages.

    The public Somewhere landing page is mostly marketing HTML, while open jobs are delegated
    to jobs.somewhere.com/recruitcrm. This parser extracts visible job-card links when present
    and falls back to detail-page parsing when a specific public job URL is supplied.
    """

    def list_job_urls(self, html: str, base_url: str) -> list[str]:
        soup = self._soup(html)
        urls: list[str] = []
        seen: set[str] = set()
        for a in soup.find_all("a", href=True):
            text = clean_text(a.get_text(" "))
            href = normalize_url(a["href"], base_url)
            if not href or href in seen:
                continue
            netloc = urlparse(href).netloc
            path = urlparse(href).path.lower()
            if any(host in netloc for host in ["recruitcrm.io", "jobs.somewhere.com", "somewhere.com"]):
                if re.search(r"job|apply|opening|view", f"{path} {text}", re.I):
                    seen.add(href)
                    urls.append(href)
        # Do not fall back to the generic Somewhere landing page. It contains marketing
        # copy, not actual job records, and previously created fake jobs.
        return urls

    def parse_detail(self, html: str, url: str) -> RawJob | None:
        # Try JSON blobs first because RecruitCRM/SPA pages often render data into script tags.
        soup = self._soup(html)
        text = clean_text(soup.get_text("\n"), max_spaces=False)
        title = clean_text((soup.find("h1") or soup.find("title") or Tag(name="")).get_text(" "))
        if title.lower() in {"jobs", "job page", "somewhere", "jobs | somewhere"} or not title:
            title = self._find_title_in_text(text) or ""
        if (
            not looks_like_real_role(title)
            or "are you looking for a remote job" in text.lower()
            or re.search(r"talent on[- ]demand|hire remote professionals on demand|somewhere browser", text, re.I)
        ):
            # This is the generic candidate/marketing page, not an individual listing.
            return None
        company = self._extract_labeled(text, ["Company", "Client", "Employer", "Hiring Organization"])
        location = self._extract_labeled(text, ["Location", "Work Location"]) or "Remote"
        salary = self._extract_labeled(text, ["Compensation", "Salary"])
        employment_type = self._extract_labeled(text, ["Job Type", "Employment Type"])
        description = clean_html_to_markdownish(str(soup.find("main") or soup.find("body") or soup))
        apply_url = self._find_apply_url(soup, url) or url
        return RawJob(
            source_name=self.config.name,
            source_url=url,
            title=title,
            company=company,
            location=location,
            category="Remote",
            summary=description or text,
            description_html=str(soup.find("body") or soup),
            apply_url=apply_url,
            salary_range=salary,
            employment_type=employment_type,
        )

    def _find_title_in_text(self, text: str) -> str | None:
        for line in text.splitlines():
            line = clean_text(line)
            if 8 <= len(line) <= 120 and re.search(r"specialist|manager|assistant|developer|engineer|analyst|coordinator|support", line, re.I):
                return line
        return None

    def _extract_labeled(self, text: str, labels: Iterable[str]) -> str | None:
        for label in labels:
            match = re.search(rf"{re.escape(label)}\s*:?\s*([^\n]+)", text, flags=re.I)
            if match:
                return clean_text(match.group(1))[:160]
        return None


PARSER_REGISTRY = {
    "generic": GenericParser,
    "applynow": ApplyNowParser,
    "impactpool": ImpactPoolParser,
    "somewhere": SomewhereParser,
}


def make_parser(config: SourceConfig) -> BaseParser:
    klass = PARSER_REGISTRY.get(config.type, GenericParser)
    return klass(config)
