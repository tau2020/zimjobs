from __future__ import annotations

import hashlib
import html
import re
from datetime import datetime, timezone
from typing import Iterable
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup
from dateutil import parser as date_parser

CATEGORY_RULES: dict[str, tuple[str, ...]] = {
    "Technology": ("software", "developer", "engineer", "data", "cyber", "it ", "ict", "ai ", "machine learning", "systems", "network", "full-stack", "backend", "frontend", "analytics"),
    "Finance": ("finance", "account", "audit", "bookkeep", "payroll", "bank", "grant", "investment", "treasury", "tax", "budget"),
    "Healthcare": ("health", "nurse", "doctor", "clinical", "medical", "pharma", "hiv", "mnch", "mental health", "nutrition", "community health"),
    "Education": ("teacher", "education", "lecturer", "trainer", "school", "curriculum", "learning", "tutor", "youth"),
    "Engineering": ("civil engineer", "electrical", "mechanical", "construction", "technician", "maintenance", "plant", "quality assurance", "qa/qc"),
    "Sales & Marketing": ("sales", "marketing", "communications", "fundraising", "growth", "business development", "social media", "brand", "customer"),
    "Administration": ("admin", "operations", "coordinator", "assistant", "secretary", "office", "logistics", "procurement", "hr ", "people and culture"),
    "NGO & International Development": ("unicef", "undp", "unops", "world bank", "ngo", "humanitarian", "consultant", "development", "governance", "research", "field coordinator", "evaluation", "peacebuilding", "climate"),
}

VALID_EMPLOYMENT_TYPES = {
    "full time": "Full-time",
    "full-time": "Full-time",
    "permanent": "Full-time",
    "part time": "Part-time",
    "part-time": "Part-time",
    "contract": "Contract",
    "consultancy": "Consultancy",
    "consultant": "Consultancy",
    "internship": "Internship",
    "intern": "Internship",
    "temporary": "Temporary",
    "volunteer": "Volunteer",
    "roster": "Roster",
}

REMOTE_TERMS = {
    "remote": "Remote",
    "home based": "Remote",
    "home-based": "Remote",
    "work from home": "Remote",
    "hybrid": "Hybrid",
    "on-site": "On-site",
    "onsite": "On-site",
}


def clean_text(value: str | None, max_spaces: bool = True) -> str:
    if not value:
        return ""
    text = html.unescape(value).replace("\xa0", " ")
    text = re.sub(r"[\u200b\ufeff]", "", text)
    if max_spaces:
        text = re.sub(r"\s+", " ", text)
    return text.strip(" \t\r\n-|•")


def clean_html_to_markdownish(value: str | None) -> str:
    if not value:
        return ""
    soup = BeautifulSoup(value, "html.parser")
    for bad in soup(["script", "style", "noscript", "svg", "form", "iframe"]):
        bad.decompose()
    for br in soup.find_all("br"):
        br.replace_with("\n")
    for li in soup.find_all("li"):
        li.insert_before("\n• ")
    for heading in soup.find_all(re.compile("^h[1-6]$")):
        heading.insert_before("\n\n")
        heading.insert_after("\n")
    for p in soup.find_all("p"):
        p.insert_after("\n")
    text = soup.get_text(" ")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return clean_text(text, max_spaces=False)


def normalize_url(url: str | None, base_url: str | None = None) -> str:
    if not url:
        return ""
    absolute = urljoin(base_url or "", url.strip())
    parsed = urlparse(absolute)
    if parsed.scheme not in {"http", "https"}:
        return ""
    return absolute.split("#", 1)[0].strip()


def parse_date(value: str | None) -> str | None:
    text = clean_text(value)
    if not text:
        return None
    text = re.sub(r"^(last updated|posted|date|closing date|deadline|apply by)\s*:?\s*", "", text, flags=re.I)
    text = text.replace("at ", "")
    if re.search(r"rolling|ongoing|asap|until filled", text, flags=re.I):
        return None
    try:
        dt = date_parser.parse(text, fuzzy=True, dayfirst=False)
        if not dt.tzinfo:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.date().isoformat()
    except Exception:
        return None


def find_deadline(text: str) -> str | None:
    patterns = [
        r"(?:apply by|closing date|deadline|applications? close|application deadline)\s*:?\s*([^\n\.;]{4,80})",
        r"by\s+(\d{1,2}\s+[A-Za-z]+\s+20\d{2})",
        r"(\d{1,2}\s+[A-Za-z]+\s+20\d{2})",
        r"([A-Za-z]+\s+\d{1,2},\s*20\d{2})",
    ]
    for pattern in patterns:
        match = re.search(pattern, text, flags=re.I)
        if match:
            parsed = parse_date(match.group(1))
            if parsed:
                return parsed
    return None


def is_expired(expires_at: str | None) -> bool:
    if not expires_at:
        return False
    try:
        return datetime.fromisoformat(expires_at).date() < datetime.now(timezone.utc).date()
    except ValueError:
        return False


def infer_company(title: str, text: str | None = None) -> str:
    joined = f"{title}\n{text or ''}"
    rules = [
        r"^(.+?)\s+(?:is|are)\s+hiring\b",
        r"(?:job|vacancy|opening)\s+at\s+([^\|\n\-–—:]+)",
        r"at\s+([^\|\n\-–—:]{2,80})\s*(?:\||-|–|—|$)",
        r"(?:About|Company)\s+([A-Z][A-Za-z0-9 &.,'’/-]{2,80})",
    ]
    for pattern in rules:
        match = re.search(pattern, joined, flags=re.I | re.M)
        if match:
            company = clean_text(match.group(1))
            company = re.sub(r"\s+(?:job|vacancy|role|position)$", "", company, flags=re.I)
            if 2 <= len(company) <= 100:
                return company
    return "Confidential"


def normalize_location(value: str | None, title: str = "", text: str = "", default: str = "Zimbabwe") -> str:
    combined = clean_text(" ".join([value or "", title, text[:500]]))
    if re.search(r"remote|home[- ]based|work from home|fully remote", combined, flags=re.I):
        if re.search(r"zimbabwe|harare|bulawayo", combined, flags=re.I):
            return "Remote / Zimbabwe"
        if re.search(r"africa|emea|south african standard time|sast|global south", combined, flags=re.I):
            return "Remote / Africa"
        return "Remote"
    for place in ["Harare", "Bulawayo", "Mutare", "Gweru", "Masvingo", "Chitungwiza", "Zimbabwe", "South Africa", "Africa", "EMEA"]:
        if re.search(rf"\b{re.escape(place)}\b", combined, flags=re.I):
            return place
    return clean_text(value) or default


def normalize_remote_status(location: str, text: str) -> str:
    combined = f"{location} {text}".lower()
    for term, normalized in REMOTE_TERMS.items():
        if term in combined:
            return normalized
    return "On-site"


def normalize_employment_type(text: str) -> str | None:
    lowered = text.lower()
    for term, normalized in VALID_EMPLOYMENT_TYPES.items():
        if re.search(rf"\b{re.escape(term)}\b", lowered):
            return normalized
    return None


def infer_category(title: str, text: str, default: str = "Other") -> str:
    combined = f"{title} {text}".lower()
    scores: list[tuple[int, str]] = []
    for category, terms in CATEGORY_RULES.items():
        score = sum(1 for term in terms if term.lower() in combined)
        if score:
            scores.append((score, category))
    if not scores:
        return default or "Other"
    return sorted(scores, reverse=True)[0][1]


def extract_salary(text: str) -> str | None:
    patterns = [
        r"(?:salary|compensation|pay|earn)\s*:?\s*([^\n]{0,20}(?:USD|US\$|\$|EUR|£)[^\n]{2,80})",
        r"((?:USD|US\$|\$|EUR|£)\s?[\d,]+(?:\s?[–\-]\s?(?:USD|US\$|\$|EUR|£)?\s?[\d,]+)?\s*(?:per annum|per year|/year|/month|per month|/hour|per hour)?)",
    ]
    for pattern in patterns:
        match = re.search(pattern, text, flags=re.I)
        if match:
            return clean_text(match.group(1))[:120]
    return None


def make_summary(title: str, description: str, max_chars: int = 900) -> str:
    text = clean_text(description, max_spaces=False)
    if not text:
        return title
    boilerplate_patterns = [
        r"By using this site, you agree to the Privacy Policy and Terms of Use\.?",
        r"Disclaimer: ApplyNOW is not the hiring organization.*?end\.",
        r"Master the recruitment process of the impact sector!.*?Get started",
    ]
    for pattern in boilerplate_patterns:
        text = re.sub(pattern, "", text, flags=re.I | re.S)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    if len(text) <= max_chars:
        return text
    return text[:max_chars].rsplit(" ", 1)[0].rstrip(".,; ") + "…"


def content_hash(values: Iterable[str | None]) -> str:
    joined = "|".join(clean_text(v).lower() for v in values if v)
    joined = re.sub(r"\W+", " ", joined)
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()
