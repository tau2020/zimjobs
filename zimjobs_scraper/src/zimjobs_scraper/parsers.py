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
    content_hash,
    extract_company_from_text,
    extract_labeled_value,
    extract_role_from_text,
    extract_section,
    find_deadline,
    is_probable_merged_job_text,
    looks_like_good_company,
    looks_like_real_role,
    normalize_job_text,
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
    default_company: str | None = None
    allowed_locations: list[str] = field(default_factory=list)
    skip_expired: bool = True
    attribution: str | None = None
    legal_status: str | None = None
    site_type: str | None = None
    ats_type: str | None = None
    careers_url: str | None = None
    pagination_strategy: str = "auto"
    selectors: dict[str, str] = field(default_factory=dict)
    api_endpoint: str | None = None
    notes: str | None = None
    include_url_patterns: list[str] = field(default_factory=list)
    exclude_url_patterns: list[str] = field(default_factory=list)
    allow_external_detail_urls: bool = False

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
            default_company=data.get("default_company"),
            allowed_locations=list(data.get("allowed_locations", [])),
            skip_expired=bool(data.get("skip_expired", True)),
            attribution=data.get("attribution"),
            legal_status=data.get("legal_status"),
            site_type=data.get("site_type"),
            ats_type=data.get("ats_type"),
            careers_url=data.get("careers_url"),
            pagination_strategy=data.get("pagination_strategy", "auto"),
            selectors=dict(data.get("selectors", {})),
            api_endpoint=data.get("api_endpoint"),
            notes=data.get("notes"),
            include_url_patterns=list(data.get("include_url_patterns", [])),
            exclude_url_patterns=list(data.get("exclude_url_patterns", [])),
            allow_external_detail_urls=bool(data.get("allow_external_detail_urls", False)),
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

    def list_pagination_urls(self, html: str, base_url: str) -> list[str]:
        if self.config.pagination_strategy in {"none", "disabled", "off"}:
            return []
        soup = self._soup(html)
        urls: list[str] = []
        seen: set[str] = set()
        selectors = [
            'a[rel~="next"]',
            "a.next",
            "a.nextpostslink",
            "a.page-numbers",
            'a[aria-label*="Next" i]',
            'a[title*="Next" i]',
        ]
        custom_selector = self.config.selectors.get("pagination_links")
        if custom_selector:
            selectors.insert(0, custom_selector)
        for selector in selectors:
            for a in soup.select(selector):
                if not isinstance(a, Tag) or not a.get("href"):
                    continue
                href = normalize_url(a.get("href"), base_url)
                if href and href not in seen and self._looks_like_listing_url(href, base_url):
                    seen.add(href)
                    urls.append(href)
        for a in soup.find_all("a", href=True):
            text = clean_text(a.get_text(" "))
            href = normalize_url(a["href"], base_url)
            if not href or href in seen:
                continue
            if re.fullmatch(r"(next|older|more|load more|>|>>|»|next page)", text, re.I) and self._looks_like_listing_url(href, base_url):
                seen.add(href)
                urls.append(href)
        return urls

    def _same_origin(self, url: str, base_url: str) -> bool:
        parsed = urlparse(url)
        base = urlparse(base_url)
        return parsed.netloc.lower() == base.netloc.lower()

    def _matches_any(self, value: str, patterns: Iterable[str]) -> bool:
        for pattern in patterns:
            if not pattern:
                continue
            try:
                if re.search(pattern, value, flags=re.I):
                    return True
            except re.error:
                if pattern.lower() in value.lower():
                    return True
        return False

    def _is_excluded_url(self, url: str) -> bool:
        defaults = [
            r"#",
            r"mailto:",
            r"tel:",
            r"javascript:",
            r"/login\b",
            r"/register\b",
            r"/privacy",
            r"/terms",
            r"/contact",
            r"/wp-login",
            r"/feed/?$",
            r"/author/",
            r"/tag/",
        ]
        return self._matches_any(url, [*defaults, *self.config.exclude_url_patterns])

    def _allowed_by_config_patterns(self, url: str) -> bool:
        if not self.config.include_url_patterns:
            return True
        return self._matches_any(url, self.config.include_url_patterns)

    def _looks_like_listing_url(self, url: str, base_url: str) -> bool:
        if not self._same_origin(url, base_url):
            return False
        if self._is_excluded_url(url):
            return False
        path_query = f"{urlparse(url).path}?{urlparse(url).query}".lower()
        return bool(re.search(r"(/page/\d+/?|\bpage=\d+\b|\bpaged=\d+\b|\boffset=\d+\b)", path_query))

    def _soup(self, html: str) -> BeautifulSoup:
        return BeautifulSoup(html, "html.parser")

    def _remove_unrelated_nodes(self, root: Tag | BeautifulSoup) -> None:
        for bad_selector in [
            "nav",
            "header",
            "footer",
            "script",
            "style",
            "form",
            "aside",
            "noscript",
            "iframe",
            "[hidden]",
            "[aria-hidden='true']",
        ]:
            for node in root.select(bad_selector):
                node.decompose()
        noisy_token_re = re.compile(
            r"(?:^|[-_\s])(?:"
            r"breadcrumb|pagination|pager|sidebar|widget|menu|nav|footer|header|"
            r"related|similar|recommended|latest|more[-_\s]?jobs|job[-_\s]?list|jobs[-_\s]?list|"
            r"search|filter|sort|share|social|comment|newsletter|cookie|advert|ads?"
            r")(?:$|[-_\s])",
            re.I,
        )
        for node in list(root.find_all(True)):
            style = node.get("style", "")
            if style and re.search(r"display\s*:\s*none|visibility\s*:\s*hidden", style, re.I):
                node.decompose()
                continue
            token = " ".join(
                [
                    str(node.get("id", "")),
                    " ".join(str(c) for c in node.get("class", [])),
                    str(node.get("role", "")),
                ]
            )
            if noisy_token_re.search(token):
                node.decompose()

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
                        department=node.get("industry") if isinstance(node.get("industry"), str) else None,
                        requirements=clean_html_to_markdownish(node.get("qualifications") or node.get("skills")),
                        extra={"json_ld": True},
                    )
                )
        return jobs


class GenericParser(BaseParser):
    """Fallback parser for simple HTML pages and screenshots copied as HTML/text."""

    JOB_URL_PATTERNS = ("/jobs/", "/job/", "/careers/", "/career/", "/vacanc", "/position", "/202", "/opportun")
    CARD_SELECTORS = (
        "[data-job-id]",
        "[data-jobid]",
        "[data-testid*='job-card' i]",
        ".job-card",
        ".job_card",
        ".job-listing",
        ".job_listing",
        ".job-item",
        ".job_item",
        ".vacancy-card",
        ".vacancy-item",
        "article[class*='job' i]",
        "li[class*='job' i]",
        "tr[class*='job' i]",
    )

    def parse_listing_payload(self, payload: str, url: str) -> list[RawJob]:
        # If normal job-detail links are available, let the pipeline fetch the
        # detail page. Card parsing is a fallback for source pages that expose
        # independent listing cards but no usable detail URL.
        if self.list_job_urls(payload, url):
            return []
        soup = self._soup(payload)
        if self._looks_like_single_detail_page(soup, url):
            return []
        jobs: list[RawJob] = []
        for index, card in enumerate(self._job_card_nodes(soup), start=1):
            raw = self._raw_from_card(card, url, index)
            if raw:
                jobs.append(raw)
        if jobs:
            log.info("listing_cards_parsed", extra={"source": self.config.name, "url": url, "status": len(jobs)})
        return jobs

    def list_job_urls(self, html: str, base_url: str) -> list[str]:
        soup = self._soup(html)
        urls: list[str] = []
        seen: set[str] = set()
        anchors: list[Tag] = []
        selector = self.config.selectors.get("job_links")
        if selector:
            for node in soup.select(selector):
                if isinstance(node, Tag):
                    if node.name == "a" and node.get("href"):
                        anchors.append(node)
                    anchors.extend([a for a in node.find_all("a", href=True) if isinstance(a, Tag)])
        anchors.extend([a for a in soup.find_all("a", href=True) if isinstance(a, Tag)])
        for a in anchors:
            text = clean_text(a.get_text(" "))
            href = normalize_url(a["href"], base_url)
            if not href or href in seen:
                continue
            if href.rstrip("/") == base_url.rstrip("/") and re.search(r"jobs?|vacanc|careers?", text, re.I):
                continue
            if not self.config.allow_external_detail_urls and not self._same_origin(href, base_url):
                continue
            if self._is_excluded_url(href):
                continue
            if not self._allowed_by_config_patterns(href):
                continue
            path = urlparse(href).path.lower()
            if self._looks_like_listing_url(href, base_url):
                continue
            if any(pat in path for pat in self.JOB_URL_PATTERNS) or re.search(r"job|vacanc|hiring|apply|career|opportun|opening|position", text, flags=re.I):
                seen.add(href)
                urls.append(href)
        if not urls and not self.config.include_url_patterns and self._looks_like_single_detail_page(soup, base_url):
            urls.append(base_url)
        elif not urls and self._looks_like_listing_page(soup, base_url):
            log.info("listing_page_without_detail_links", extra={"source": self.config.name, "url": base_url, "status": "skipped"})
        return urls

    def parse_detail(self, html: str, url: str) -> RawJob | None:
        soup = self._soup(html)
        json_jobs = self._json_ld_jobs(soup, url)
        if json_jobs:
            return json_jobs[0]
        if self._looks_like_listing_page(soup, url):
            log.info("parse_detail_skipped_listing_page", extra={"source": self.config.name, "url": url, "status": "listing_page"})
            return None
        title = clean_text((soup.find("h1") or Tag(name="")).get_text(" ")) or self._meta(soup, "og:title", "twitter:title") or clean_text((soup.find("title") or Tag(name="")).get_text(" "))
        if not title:
            return None
        main = self._detail_container(soup)
        self._remove_unrelated_nodes(main)
        description = clean_html_to_markdownish(str(main))
        if is_probable_merged_job_text(title, description):
            log.info("parse_detail_skipped_merged_text", extra={"source": self.config.name, "url": url, "job_title": title, "status": "merged_text"})
            return None
        company = (
            extract_labeled_value(description, ["Company", "Organisation", "Organization", "Employer", "Hiring Organization"])
            or self._meta(soup, "article:author", "author")
            or extract_company_from_text(title, description)
        )
        apply_url = self._find_apply_url(soup, url) or url
        location = self._extract_labeled(description, ["Location", "Opportunity Location", "Duty Station", "Work Location"]) or self.config.default_location
        posted = self._meta(soup, "article:published_time", "datePublished", "date") or self._extract_labeled(description, ["Posted", "Date Posted", "Publication Date"])
        employment_type = self._extract_labeled(description, ["Employment Type", "Job Type", "Contract", "Contract Type", "Opportunity Type"])
        department = self._extract_labeled(description, ["Department", "Team", "Unit", "Programme", "Program"])
        salary = self._extract_labeled(description, ["Salary", "Compensation", "Pay"])
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
            posted_at=parse_date(posted),
            expires_at=find_deadline(description),
            department=department,
            employment_type=employment_type,
            salary_range=salary,
            requirements=extract_section(description, ["Requirements", "Qualifications", "Qualifications and Experience", "Required Skills"]),
        )

    def _find_apply_url(self, soup: BeautifulSoup, base_url: str) -> str | None:
        for a in soup.find_all("a", href=True):
            text = clean_text(a.get_text(" "))
            if re.search(r"apply|view full|job call|application|official site", text, re.I):
                href = normalize_url(a["href"], base_url)
                if href and not self._is_excluded_url(href):
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

    def _detail_container(self, soup: BeautifulSoup) -> Tag | BeautifulSoup:
        custom_selector = self.config.selectors.get("detail_container") or self.config.selectors.get("description")
        selectors = [
            custom_selector,
            "[itemtype*='JobPosting' i]",
            ".job-detail",
            ".job-details",
            ".job-description",
            ".job_description",
            ".single_job_listing",
            "article",
            "main",
        ]
        for selector in selectors:
            if not selector:
                continue
            node = soup.select_one(selector)
            if isinstance(node, Tag):
                return node
        return soup.find("body") or soup

    def _job_card_nodes(self, soup: BeautifulSoup) -> list[Tag]:
        selectors = [self.config.selectors.get("job_cards"), *self.CARD_SELECTORS]
        nodes: list[Tag] = []
        seen: set[int] = set()
        container_re = re.compile(r"(?:list|grid|container|wrapper|search|filter|header|sidebar|nav|menu)", re.I)
        for selector in selectors:
            if not selector:
                continue
            for node in soup.select(selector):
                if not isinstance(node, Tag) or id(node) in seen:
                    continue
                token = " ".join([str(node.get("id", "")), " ".join(str(c) for c in node.get("class", []))])
                if node.name in {"html", "body", "main"} or container_re.search(token):
                    continue
                text = normalize_job_text(node.get_text("\n"), max_chars=5000)
                if len(text) < 40 or len(text) > 5000:
                    continue
                seen.add(id(node))
                nodes.append(node)
        # Drop outer duplicate wrappers while keeping the most specific cards.
        node_ids = {id(node) for node in nodes}
        return [node for node in nodes if not any(id(child) in node_ids for child in node.find_all(True))]

    def _raw_from_card(self, card: Tag, base_url: str, index: int) -> RawJob | None:
        title = self._card_title(card)
        if not title:
            log.info("listing_card_skipped", extra={"source": self.config.name, "url": base_url, "status": "missing_title"})
            return None
        detail_url = self._card_url(card, base_url)
        source_url = detail_url or base_url
        description = normalize_job_text(card.get_text("\n"), max_chars=3000)
        if is_probable_merged_job_text(title, description):
            log.info("listing_card_skipped", extra={"source": self.config.name, "url": base_url, "job_title": title, "status": "merged_card"})
            return None
        company = self._card_field(card, ["company", "organisation", "organization", "employer"]) or extract_company_from_text(title, description)
        location = self._card_field(card, ["location", "city", "country", "duty-station", "work-location"]) or self.config.default_location
        employment_type = self._card_field(card, ["type", "job-type", "employment", "contract"])
        salary = self._card_field(card, ["salary", "compensation", "pay"])
        external_id = (
            clean_text(card.get("data-job-id") or card.get("data-jobid") or card.get("data-id"))
            or content_hash([self.config.name, base_url, title, company, location, str(index)])[:16]
        )
        return RawJob(
            source_name=self.config.name,
            source_url=source_url,
            title=title,
            company=company,
            location=location,
            category=self.config.default_category,
            summary=description,
            description_html=str(card),
            apply_url=detail_url or base_url,
            expires_at=find_deadline(description),
            employment_type=employment_type,
            salary_range=salary,
            external_id=external_id,
            requirements=extract_section(description, ["Requirements", "Qualifications", "Qualifications and Experience", "Required Skills"]),
            extra={"listing_card": True, "listing_url": base_url},
        )

    def _card_title(self, card: Tag) -> str | None:
        selectors = [
            self.config.selectors.get("title"),
            "[class*='title' i]",
            "h1",
            "h2",
            "h3",
            "a",
        ]
        for selector in selectors:
            if not selector:
                continue
            for node in card.select(selector):
                text = clean_text(node.get_text(" "))
                text = re.sub(r"\s*\|\s*(?:apply|deadline|closing date).*$", "", text, flags=re.I)
                if 5 <= len(text) <= 140 and (looks_like_real_role(text) or re.search(r"\b(?:officer|manager|assistant|engineer|developer|coordinator|analyst|accountant|consultant|intern)\b", text, re.I)):
                    return text
        for raw_line in normalize_job_text(card.get_text("\n")).splitlines()[:6]:
            line = clean_text(raw_line)
            if 5 <= len(line) <= 140 and looks_like_real_role(line):
                return line
        return None

    def _card_url(self, card: Tag, base_url: str) -> str | None:
        for selector in [self.config.selectors.get("job_links"), "a[href]"]:
            if not selector:
                continue
            for a in card.select(selector):
                if not isinstance(a, Tag) or not a.get("href"):
                    continue
                href = normalize_url(a.get("href"), base_url)
                if not href or self._is_excluded_url(href):
                    continue
                if not self.config.allow_external_detail_urls and not self._same_origin(href, base_url):
                    continue
                if self._allowed_by_config_patterns(href):
                    return href
        return None

    def _card_field(self, card: Tag, field_names: Iterable[str]) -> str | None:
        names = list(field_names)
        selector_parts = []
        for name in names:
            selector_parts.extend([f"[class*='{name}' i]", f"[data-testid*='{name}' i]"])
        for selector in selector_parts:
            node = card.select_one(selector)
            if node:
                value = clean_text(node.get_text(" "))
                if 2 <= len(value) <= 160:
                    return value
        text = normalize_job_text(card.get_text("\n"))
        labels = [name.replace("-", " ").replace("_", " ").title() for name in names]
        return extract_labeled_value(text, labels)

    def _looks_like_listing_page(self, soup: BeautifulSoup, base_url: str) -> bool:
        h1 = clean_text((soup.find("h1") or Tag(name="")).get_text(" "))
        if looks_like_real_role(h1):
            return False
        if len(self._job_card_nodes(soup)) > 1:
            return True
        page_title = " ".join([h1, clean_text((soup.find("title") or Tag(name="")).get_text(" "))])
        if re.search(r"\b(?:jobs?|vacancies|careers|opportunities|search results|category)\b", page_title, re.I):
            return True
        text = clean_text(soup.get_text(" "))
        roleish_links = 0
        for a in soup.find_all("a", href=True):
            link_text = clean_text(a.get_text(" "))
            if looks_like_real_role(link_text):
                roleish_links += 1
        return roleish_links >= 4 or bool(re.search(r"\b(?:showing\s+\d+|jobs found|filter by|sort by|load more)\b", text, re.I))

    def _looks_like_single_detail_page(self, soup: BeautifulSoup, base_url: str) -> bool:
        if self._json_ld_jobs(soup, base_url):
            return True
        if self._looks_like_listing_page(soup, base_url):
            return False
        path = urlparse(base_url).path.lower().rstrip("/")
        if any(pat.strip("/") in path for pat in self.JOB_URL_PATTERNS) and not re.search(r"/(?:jobs?|careers?|vacancies)$", path):
            return True
        h1 = clean_text((soup.find("h1") or Tag(name="")).get_text(" "))
        return looks_like_real_role(h1)


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
            tags = item.get("jobIndustry") or item.get("jobLevel") or item.get("jobType")
            department = ", ".join(clean_text(str(t)) for t in tags if clean_text(str(t))) if isinstance(tags, list) else clean_text(str(tags)) if tags else None
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
                    department=department,
                    employment_type=item.get("jobType"),
                    remote_status="Remote",
                    external_id=str(item.get("id") or item.get("jobSlug") or "") or None,
                    requirements=extract_section(clean_html_to_markdownish(description_html), ["Requirements", "Qualifications", "Required Skills"]),
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
                    department=", ".join(clean_text(str(tag)) for tag in tags[:5] if clean_text(str(tag))) if tags else None,
                    employment_type=item.get("type"),
                    salary_range=salary,
                    remote_status="Remote",
                    external_id=str(item.get("id") or "") or None,
                    requirements=extract_section(clean_html_to_markdownish(description_html), ["Requirements", "Qualifications", "Required Skills"]),
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
            department = self._join_names(fields.get("career_categories")) or self._join_names(fields.get("theme"))
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
                    department=department or None,
                    employment_type=fields.get("career_categories", [{}])[0].get("name") if isinstance(fields.get("career_categories"), list) and fields.get("career_categories") else None,
                    external_id=str(item.get("id") or "") or None,
                    requirements=extract_section(clean_html_to_markdownish(description), ["Requirements", "Qualifications", "Required Skills"]),
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
            departments = item.get("departments") or []
            department = None
            if isinstance(departments, list) and departments:
                names = [clean_text(d.get("name")) for d in departments if isinstance(d, dict) and d.get("name")]
                department = ", ".join([name for name in names if name]) or None
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
                    department=department,
                    external_id=str(item.get("id") or "") or None,
                    remote_status="Remote" if re.search(r"remote|global|worldwide|emea|africa", location, re.I) else None,
                    requirements=extract_section(clean_html_to_markdownish(description_html), ["Requirements", "Qualifications", "Required Skills"]),
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
                    department=categories.get("team") if isinstance(categories, dict) else None,
                    employment_type=categories.get("commitment") if isinstance(categories, dict) else None,
                    external_id=str(item.get("id") or "") or None,
                    remote_status="Remote" if re.search(r"remote|global|worldwide|emea|africa", location + description_html, re.I) else None,
                    requirements=extract_section(description_html, ["Requirements", "Qualifications", "Required Skills"]),
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
        self._remove_unrelated_nodes(article)
        title = clean_text((soup.find("h1") or soup.find("title") or Tag(name="")).get_text(" "))
        description = clean_html_to_markdownish(str(article))
        if is_probable_merged_job_text(title, description):
            log.info("parse_detail_skipped_merged_text", extra={"source": self.config.name, "url": url, "job_title": title, "status": "merged_text"})
            return None
        text = clean_text(description, max_spaces=False)
        role = extract_role_from_text(text)
        if role:
            title = role if not looks_like_real_role(title) else title
        company = self._extract_field(text, ["Company", "Organisation", "Organization", "Employer"])
        if not company:
            company = extract_company_from_text(title, text)
        location = self._extract_field(text, ["Location", "Opportunity Location", "Duty Station", "Work Location"]) or self.config.default_location
        department = self._extract_field(text, ["Department", "Team", "Unit", "Programme", "Program"])
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
            department=department,
            employment_type=employment_type,
            salary_range=salary,
            requirements=extract_section(text, ["Requirements", "Qualifications", "Qualifications and Experience", "Required Skills"]),
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
        self._remove_unrelated_nodes(main)
        description = clean_html_to_markdownish(str(main))
        if is_probable_merged_job_text(title, description):
            log.info("parse_detail_skipped_merged_text", extra={"source": self.config.name, "url": url, "job_title": title, "status": "merged_text"})
            return None
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


class PscERecruitmentParser(GenericParser):
    def list_job_urls(self, html: str, base_url: str) -> list[str]:
        soup = self._soup(html)
        urls: list[str] = []
        seen: set[str] = set()
        for a in soup.find_all("a", href=True):
            href = normalize_url(a["href"], base_url)
            if not href or href in seen:
                continue
            if re.search(r"^https://erecruitment\.psc\.gov\.zw/jobs/\d+/?$", href, re.I):
                seen.add(href)
                urls.append(href)
        return urls

    def parse_detail(self, html: str, url: str) -> RawJob | None:
        soup = self._soup(html)
        title = clean_text((soup.find("h1") or Tag(name="")).get_text(" "))
        if not title:
            return None
        main = soup.find("main") or soup.find("body") or soup
        self._remove_unrelated_nodes(main)
        description = clean_html_to_markdownish(str(main))
        if is_probable_merged_job_text(title, description):
            log.info("parse_detail_skipped_merged_text", extra={"source": self.config.name, "url": url, "job_title": title, "status": "merged_text"})
            return None
        text = clean_text(description, max_spaces=False)
        location = self._extract_location(text) or self.config.default_location
        reference = self._extract_labeled(text, ["Ref", "Reference"])
        vacancy_no = self._extract_labeled(text, ["Vacancy No", "Vacancy Number"])
        return RawJob(
            source_name=self.config.name,
            source_url=url,
            title=title,
            company="Public Service Commission Zimbabwe",
            location=location,
            category=self.config.default_category,
            summary=description,
            description_html=str(main),
            apply_url=url,
            expires_at=find_deadline(text) or self._extract_deadline(text),
            department=self._extract_department(text),
            employment_type=self._extract_labeled(text, ["Employment Type", "Job Type"]) or self._first_line_after_title(text, title),
            external_id=reference or vacancy_no or urlparse(url).path.rstrip("/").split("/")[-1],
            requirements=extract_section(text, ["Requirements & Qualifications", "Requirements", "Qualifications"]),
            extra={"reference": reference, "vacancy_no": vacancy_no},
        )

    def _extract_location(self, text: str) -> str | None:
        for line in text.splitlines():
            value = clean_text(line)
            if re.search(r"\b(?:Harare|Bulawayo|Zimbabwe|Gweru|Mutare|Masvingo)\b", value, re.I):
                return value
        return None

    def _extract_department(self, text: str) -> str | None:
        for line in text.splitlines():
            value = clean_text(line)
            if re.search(r"\b(?:Ministry|Department|Commission|Office of)\b", value, re.I) and len(value) < 180:
                return value
        return None

    def _first_line_after_title(self, text: str, title: str) -> str | None:
        lines = [clean_text(line) for line in text.splitlines() if clean_text(line)]
        for index, line in enumerate(lines):
            if line == title and index + 1 < len(lines):
                candidate = lines[index + 1]
                if re.search(r"full[- ]time|part[- ]time|contract|temporary|intern", candidate, re.I):
                    return candidate
        return None

    def _extract_deadline(self, text: str) -> str | None:
        lines = [clean_text(line) for line in text.splitlines() if clean_text(line)]
        for index, line in enumerate(lines):
            if re.fullmatch(r"application deadline|deadline|closing date", line, re.I) and index + 1 < len(lines):
                return parse_date(lines[index + 1])
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
        main = soup.find("main") or soup.find("body") or soup
        self._remove_unrelated_nodes(main)
        description = clean_html_to_markdownish(str(main))
        if is_probable_merged_job_text(title, description):
            log.info("parse_detail_skipped_merged_text", extra={"source": self.config.name, "url": url, "job_title": title, "status": "merged_text"})
            return None
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
    "psc_erecruitment": PscERecruitmentParser,
    "somewhere": SomewhereParser,
}


def make_parser(config: SourceConfig) -> BaseParser:
    klass = PARSER_REGISTRY.get(config.type, GenericParser)
    return klass(config)
