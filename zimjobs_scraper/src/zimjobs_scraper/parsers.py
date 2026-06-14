from __future__ import annotations

import json
import logging
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from typing import Any, Iterable
from urllib.parse import urlparse

from bs4 import BeautifulSoup, Tag

from .models import RawJob
from .normalization import (
    clean_html_to_markdownish,
    clean_text,
    extract_company_from_text,
    extract_role_from_text,
    find_deadline,
    looks_like_good_company,
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
    attribution: str | None = None
    legal_status: str | None = None

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
            attribution=data.get("attribution"),
            legal_status=data.get("legal_status"),
        )


class BaseParser:
    def __init__(self, config: SourceConfig):
        self.config = config

    def list_job_urls(self, html: str, base_url: str) -> list[str]:
        raise NotImplementedError

    def parse_detail(self, html: str, url: str) -> RawJob | None:
        raise NotImplementedError

    def parse_listing_payload(self, payload: str, url: str) -> list[RawJob]:
        """Parse an API/RSS/listing response directly into RawJob records.

        HTML parsers normally return an empty list and let the pipeline fetch detail pages.
        API/RSS parsers override this so official feeds can be consumed without brittle
        detail-page scraping.
        """
        return []

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
            for node in list(nodes):
                if isinstance(node, dict) and node.get("@graph"):
                    nodes.extend([n for n in node["@graph"] if isinstance(n, dict)])
                if not isinstance(node, dict):
                    continue
                node_type = node.get("@type", "")
                types = node_type if isinstance(node_type, list) else [node_type]
                if "jobposting" not in {str(t).lower() for t in types}:
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
                if isinstance(job_loc, list):
                    job_loc = job_loc[0] if job_loc else None
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
                        location=location or self.config.default_location,
                        category=self.config.default_category,
                        summary=clean_html_to_markdownish(node.get("description")),
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

    JOB_URL_PATTERNS = ("/jobs/", "/job/", "/careers/", "/career/", "/vacanc", "/202", "/opportun")

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
            if any(pat in path for pat in self.JOB_URL_PATTERNS) or re.search(r"job|vacanc|hiring|apply|career|opportun", text, flags=re.I):
                seen.add(href)
                urls.append(href)
        if not urls and re.search(r"job|vacanc|hiring|apply|career|opportun", clean_text(soup.get_text(" ")), flags=re.I):
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
        for bad_selector in ["nav", "header", "footer", "script", "style", "form", "aside"]:
            for node in main.select(bad_selector):
                node.decompose()
        description = clean_html_to_markdownish(str(main))
        company = self._meta(soup, "article:author", "author") or extract_company_from_text(title, description)
        apply_url = self._find_apply_url(soup, url) or url
        location = self._extract_labeled(description, ["Location", "Opportunity Location", "Duty Station", "Work Location"]) or self.config.default_location
        return RawJob(
            source_name=self.config.name,
            source_url=url,
            title=title,
            company=company,
            location=location,
            category=self.config.default_category,
            summary=description,
            description_html=str(main),
            apply_url=apply_url,
            expires_at=find_deadline(description),
        )

    def _find_apply_url(self, soup: BeautifulSoup, base_url: str) -> str | None:
        for a in soup.find_all("a", href=True):
            text = clean_text(a.get_text(" "))
            if re.search(r"apply|view full|job call|application|official site", text, re.I):
                href = normalize_url(a["href"], base_url)
                if href:
                    return href
        return None

    def _extract_labeled(self, text: str, labels: Iterable[str]) -> str | None:
        for label in labels:
            match = re.search(rf"(?im)^\s*(?:[•\-*]\s*)?{re.escape(label)}\s*:?\s*([^\n]+)", text)
            if match:
                value = clean_text(match.group(1))
                if value and len(value) < 120:
                    return value
        return None


class RssFeedParser(BaseParser):
    """Parser for officially published job RSS feeds such as We Work Remotely."""

    def list_job_urls(self, html: str, base_url: str) -> list[str]:
        return []

    def parse_detail(self, html: str, url: str) -> RawJob | None:
        return None

    def parse_listing_payload(self, payload: str, url: str) -> list[RawJob]:
        try:
            root = ET.fromstring(payload.encode("utf-8"))
        except ET.ParseError:
            return []
        jobs: list[RawJob] = []
        for item in root.findall(".//item"):
            title = self._node_text(item, "title")
            link = self._node_text(item, "link") or url
            description_html = self._node_text(item, "description") or self._node_text(item, "{http://purl.org/rss/1.0/modules/content/}encoded")
            pub_date = self._node_text(item, "pubDate") or self._node_text(item, "{http://purl.org/dc/elements/1.1/}date")
            categories = [clean_text(c.text) for c in item.findall("category") if clean_text(c.text)]
            company, role = self._split_rss_title(title)
            description = clean_html_to_markdownish(description_html)
            jobs.append(
                RawJob(
                    source_name=self.config.name,
                    source_url=normalize_url(link, url) or url,
                    title=role or title,
                    company=company,
                    location=self._infer_remote_location(description, categories),
                    category=self.config.default_category,
                    summary=description or title,
                    description_html=description_html,
                    apply_url=normalize_url(link, url) or url,
                    posted_at=parse_date(pub_date),
                    remote_status="Remote",
                    extra={"rss_categories": categories, "attribution": self.config.attribution},
                )
            )
        return jobs

    def _node_text(self, item: ET.Element, name: str) -> str:
        node = item.find(name)
        return clean_text(node.text if node is not None else "", max_spaces=False)

    def _split_rss_title(self, title: str) -> tuple[str | None, str]:
        cleaned = clean_text(title)
        # WWR commonly uses "Company: Role". Keep the fallback conservative.
        if ":" in cleaned:
            left, right = [clean_text(p) for p in cleaned.split(":", 1)]
            if looks_like_good_company(left) and (looks_like_real_role(right) or len(right) >= 5):
                return left, right
        match = re.search(r"^(?P<role>.+?)\s+(?:at|with)\s+(?P<company>[A-Z0-9][A-Za-z0-9 &.'’/-]{2,80})$", cleaned, re.I)
        if match:
            return clean_text(match.group("company")), clean_text(match.group("role"))
        return None, cleaned

    def _infer_remote_location(self, text: str, categories: list[str]) -> str:
        combined = " ".join(categories + [text]).lower()
        if re.search(r"anywhere in the world|worldwide|global", combined):
            return "Remote / Worldwide"
        if re.search(r"africa|emea|sast|south african standard time", combined):
            return "Remote / Africa"
        return self.config.default_location or "Remote"


class JobicyApiParser(BaseParser):
    def list_job_urls(self, html: str, base_url: str) -> list[str]:
        return []

    def parse_detail(self, html: str, url: str) -> RawJob | None:
        return None

    def parse_listing_payload(self, payload: str, url: str) -> list[RawJob]:
        try:
            data = json.loads(payload)
        except json.JSONDecodeError:
            return []
        items = data.get("jobs", []) if isinstance(data, dict) else []
        jobs: list[RawJob] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            description_html = item.get("jobDescription") or item.get("description") or item.get("jobExcerpt") or ""
            location = clean_text(item.get("jobGeo") or item.get("jobRegion") or self.config.default_location)
            title = item.get("jobTitle") or item.get("title")
            company = item.get("companyName") or item.get("company")
            link = item.get("url") or item.get("jobUrl") or item.get("jobSlug") or url
            jobs.append(
                RawJob(
                    source_name=self.config.name,
                    source_url=normalize_url(link, url) or url,
                    title=title,
                    company=company,
                    location=location or self.config.default_location,
                    category=self.config.default_category,
                    summary=clean_html_to_markdownish(description_html) or clean_text(item.get("jobExcerpt")),
                    description_html=description_html,
                    apply_url=normalize_url(link, url) or url,
                    posted_at=parse_date(item.get("pubDate") or item.get("publishedAt")),
                    employment_type=item.get("jobType"),
                    remote_status="Remote",
                    external_id=str(item.get("id") or item.get("jobSlug") or "") or None,
                    extra={"attribution": self.config.attribution, "api": "jobicy"},
                )
            )
        return jobs


class RemoteOkApiParser(BaseParser):
    def list_job_urls(self, html: str, base_url: str) -> list[str]:
        return []

    def parse_detail(self, html: str, url: str) -> RawJob | None:
        return None

    def parse_listing_payload(self, payload: str, url: str) -> list[RawJob]:
        try:
            data = json.loads(payload)
        except json.JSONDecodeError:
            return []
        items = data if isinstance(data, list) else data.get("jobs", []) if isinstance(data, dict) else []
        jobs: list[RawJob] = []
        for item in items:
            if not isinstance(item, dict) or not (item.get("position") or item.get("title")):
                continue
            tags = item.get("tags") or []
            if isinstance(tags, str):
                tags = [tags]
            salary = self._salary(item)
            description_html = item.get("description") or item.get("description_html") or ""
            location = clean_text(item.get("location") or self.config.default_location)
            source_url = normalize_url(item.get("url") or item.get("apply_url") or item.get("slug"), url) or url
            jobs.append(
                RawJob(
                    source_name=self.config.name,
                    source_url=source_url,
                    title=item.get("position") or item.get("title"),
                    company=item.get("company"),
                    location=location or "Remote",
                    category=self.config.default_category,
                    summary=clean_html_to_markdownish(description_html) or " ".join(tags),
                    description_html=description_html,
                    apply_url=source_url,
                    posted_at=parse_date(item.get("date") or item.get("epoch")),
                    employment_type=item.get("type"),
                    salary_range=salary,
                    remote_status="Remote",
                    external_id=str(item.get("id") or "") or None,
                    extra={"tags": tags, "attribution": self.config.attribution, "api": "remoteok"},
                )
            )
        return jobs

    def _salary(self, item: dict[str, Any]) -> str | None:
        salary = item.get("salary")
        if salary:
            return clean_text(str(salary))
        min_salary = item.get("salary_min")
        max_salary = item.get("salary_max")
        if min_salary and max_salary:
            return f"USD {min_salary} - {max_salary}"
        return None


class ReliefWebApiParser(BaseParser):
    def list_job_urls(self, html: str, base_url: str) -> list[str]:
        return []

    def parse_detail(self, html: str, url: str) -> RawJob | None:
        return None

    def parse_listing_payload(self, payload: str, url: str) -> list[RawJob]:
        try:
            data = json.loads(payload)
        except json.JSONDecodeError:
            return []
        jobs: list[RawJob] = []
        for item in data.get("data", []) if isinstance(data, dict) else []:
            fields = item.get("fields", {}) if isinstance(item, dict) else {}
            if not isinstance(fields, dict):
                continue
            source = fields.get("source") or []
            company = self._first_name(source) or self._first_name(fields.get("organization"))
            country = self._join_names(fields.get("country"))
            city = self._join_names(fields.get("city"))
            location = ", ".join([v for v in [city, country] if v]) or self.config.default_location
            title = fields.get("title")
            description = fields.get("body-html") or fields.get("body") or fields.get("description") or ""
            url_alias = fields.get("url_alias") or item.get("href") or url
            jobs.append(
                RawJob(
                    source_name=self.config.name,
                    source_url=normalize_url(url_alias, "https://reliefweb.int") or url,
                    title=title,
                    company=company,
                    location=location,
                    category=self.config.default_category,
                    summary=clean_html_to_markdownish(description),
                    description_html=description,
                    apply_url=normalize_url(fields.get("how_to_apply") or url_alias, "https://reliefweb.int") or normalize_url(url_alias, "https://reliefweb.int") or url,
                    posted_at=parse_date(fields.get("date", {}).get("created") if isinstance(fields.get("date"), dict) else fields.get("date")),
                    expires_at=parse_date(fields.get("date", {}).get("closing") if isinstance(fields.get("date"), dict) else fields.get("date.closing")),
                    employment_type=fields.get("career_categories", [{}])[0].get("name") if isinstance(fields.get("career_categories"), list) and fields.get("career_categories") else None,
                    external_id=str(item.get("id") or "") or None,
                    extra={"attribution": self.config.attribution, "api": "reliefweb"},
                )
            )
        return jobs

    def _first_name(self, value: Any) -> str | None:
        if isinstance(value, dict):
            return clean_text(value.get("name")) or None
        if isinstance(value, list) and value:
            first = value[0]
            if isinstance(first, dict):
                return clean_text(first.get("name")) or None
            if isinstance(first, str):
                return clean_text(first)
        if isinstance(value, str):
            return clean_text(value)
        return None

    def _join_names(self, value: Any) -> str:
        if isinstance(value, dict):
            return clean_text(value.get("name"))
        if isinstance(value, list):
            names: list[str] = []
            for item in value:
                if isinstance(item, dict) and item.get("name"):
                    names.append(clean_text(item.get("name")))
                elif isinstance(item, str):
                    names.append(clean_text(item))
            return ", ".join([n for n in names if n])
        if isinstance(value, str):
            return clean_text(value)
        return ""


class GreenhouseApiParser(BaseParser):
    def list_job_urls(self, html: str, base_url: str) -> list[str]:
        return []

    def parse_detail(self, html: str, url: str) -> RawJob | None:
        return None

    def parse_listing_payload(self, payload: str, url: str) -> list[RawJob]:
        try:
            data = json.loads(payload)
        except json.JSONDecodeError:
            return []
        items = data.get("jobs", []) if isinstance(data, dict) else []
        jobs: list[RawJob] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            location = clean_text((item.get("location") or {}).get("name") if isinstance(item.get("location"), dict) else item.get("location")) or self.config.default_location
            description_html = item.get("content") or ""
            absolute_url = item.get("absolute_url") or url
            company = self._company_from_url(absolute_url) or self.config.name.replace("_", " ").title()
            jobs.append(
                RawJob(
                    source_name=self.config.name,
                    source_url=normalize_url(absolute_url, url) or url,
                    title=item.get("title"),
                    company=company,
                    location=location,
                    category=self.config.default_category,
                    summary=clean_html_to_markdownish(description_html),
                    description_html=description_html,
                    apply_url=normalize_url(absolute_url, url) or url,
                    posted_at=parse_date(item.get("updated_at")),
                    external_id=str(item.get("id") or "") or None,
                    remote_status="Remote" if re.search(r"remote|global|worldwide|emea|africa", location, re.I) else None,
                    extra={"api": "greenhouse"},
                )
            )
        return jobs

    def _company_from_url(self, url: str) -> str | None:
        path = urlparse(url).path.strip("/").split("/")
        if path:
            token = path[0].replace("job-boards", "").replace("greenhouse", "")
            token = re.sub(r"[^A-Za-z0-9]+", " ", token).strip()
            if token and looks_like_good_company(token):
                return token.title()
        return None


class LeverApiParser(BaseParser):
    def list_job_urls(self, html: str, base_url: str) -> list[str]:
        return []

    def parse_detail(self, html: str, url: str) -> RawJob | None:
        return None

    def parse_listing_payload(self, payload: str, url: str) -> list[RawJob]:
        try:
            data = json.loads(payload)
        except json.JSONDecodeError:
            return []
        items = data if isinstance(data, list) else data.get("postings", []) if isinstance(data, dict) else []
        company = self._company_from_url(url) or self.config.name.replace("_", " ").title()
        jobs: list[RawJob] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            categories = item.get("categories") or {}
            location = clean_text(categories.get("location") if isinstance(categories, dict) else "") or self.config.default_location
            description_html = "\n".join([clean_text(item.get("description")), clean_text(item.get("descriptionPlain"))])
            hosted_url = item.get("hostedUrl") or item.get("applyUrl") or url
            jobs.append(
                RawJob(
                    source_name=self.config.name,
                    source_url=normalize_url(hosted_url, url) or url,
                    title=item.get("text"),
                    company=company,
                    location=location,
                    category=self.config.default_category,
                    summary=clean_html_to_markdownish(description_html),
                    description_html=description_html,
                    apply_url=normalize_url(hosted_url, url) or url,
                    employment_type=categories.get("commitment") if isinstance(categories, dict) else None,
                    external_id=str(item.get("id") or "") or None,
                    remote_status="Remote" if re.search(r"remote|global|worldwide|emea|africa", location + description_html, re.I) else None,
                    extra={"api": "lever"},
                )
            )
        return jobs

    def _company_from_url(self, url: str) -> str | None:
        path = urlparse(url).path.strip("/").split("/")
        if path:
            token = path[-1]
            token = re.sub(r"[^A-Za-z0-9]+", " ", token).strip()
            if token and looks_like_good_company(token):
                return token.title()
        return None


class ApplyNowParser(GenericParser):
    def list_job_urls(self, html: str, base_url: str) -> list[str]:
        soup = self._soup(html)
        urls: list[str] = []
        seen: set[str] = set()
        for a in soup.find_all("a", href=True):
            href = normalize_url(a["href"], base_url)
            if not href or href in seen:
                continue
            text = clean_text(a.get_text(" "))
            parsed = urlparse(href)
            if "applynow.co.zw" in parsed.netloc and re.search(r"/20\d{2}/\d{2}/\d{2}/", parsed.path):
                if not re.search(r"privacy|terms|category|tag|author", href, re.I):
                    seen.add(href)
                    urls.append(href)
            elif re.search(r"apply|job|vacanc|hiring", text, re.I) and "applynow.co.zw" in parsed.netloc:
                seen.add(href)
                urls.append(href)
        return urls

    def parse_detail(self, html: str, url: str) -> RawJob | None:
        soup = self._soup(html)
        article = soup.find("article") or soup.find("main") or soup.find("body") or soup
        for bad_selector in ["nav", "header", "footer", "script", "style", "form", "aside"]:
            for node in article.select(bad_selector):
                node.decompose()
        title = clean_text((soup.find("h1") or soup.find("title") or Tag(name="")).get_text(" "))
        description = clean_html_to_markdownish(str(article))
        text = clean_text(description, max_spaces=False)
        role = extract_role_from_text(text)
        if role:
            title = role if not looks_like_real_role(title) else title
        company = self._extract_field(text, ["Company", "Organisation", "Organization", "Employer"])
        if not company:
            company = extract_company_from_text(title, text)
        location = self._extract_field(text, ["Location", "Opportunity Location", "Duty Station", "Work Location"]) or self.config.default_location
        employment_type = self._extract_field(text, ["Contract", "Contract Type", "Opportunity Type", "Employment Type", "Job Type"])
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
        return urls

    def parse_detail(self, html: str, url: str) -> RawJob | None:
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
    "rss": RssFeedParser,
    "rss_feed": RssFeedParser,
    "jobicy_api": JobicyApiParser,
    "remoteok_api": RemoteOkApiParser,
    "reliefweb_api": ReliefWebApiParser,
    "greenhouse_api": GreenhouseApiParser,
    "lever_api": LeverApiParser,
    "applynow": ApplyNowParser,
    "impactpool": ImpactPoolParser,
    "somewhere": SomewhereParser,
}


def make_parser(config: SourceConfig) -> BaseParser:
    klass = PARSER_REGISTRY.get(config.type, GenericParser)
    return klass(config)
