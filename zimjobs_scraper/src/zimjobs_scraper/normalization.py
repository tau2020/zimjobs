from __future__ import annotations

import hashlib
import html
import os
import re
from datetime import datetime, timezone
from typing import Iterable
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup
from dateutil import parser as date_parser

# These match the current public ZimJobs Hub filters shown on the site.
SITE_CATEGORIES = {
    "NGO & Development",
    "Government",
    "Private Sector",
    "Remote & International",
    "Internships",
}

CATEGORY_RULES: dict[str, tuple[str, ...]] = {
    "Internships": (
        "intern", "internship", "graduate trainee", "attachment", "trainee", "student placement",
    ),
    "Remote & International": (
        "remote", "home based", "home-based", "work from home", "hybrid", "anywhere", "emea", "africa remote",
        "international", "global", "distributed", "timezone", "time zone", "sast", "utc", "contractor",
    ),
    "Government": (
        "ministry", "government", "parliament", "municipality", "city council", "local authority", "public service",
        "civil service", "zimbabwe republic", "zimra", "zesa", "nssa", "zrp", "council",
    ),
    "NGO & Development": (
        "ngo", "non-governmental", "non governmental", "unicef", "undp", "unops", "unfpa", "unhcr", "who ",
        "world vision", "care international", "oxfam", "plan international", "red cross", "save the children", "usaid",
        "fhi 360", "icap", "egpaf", "humanitarian", "development", "donor", "grant", "peacebuilding",
        "livelihoods", "wash", "gender", "climate", "community based", "impactpool", "relief", "foundation",
    ),
    "Private Sector": (
        "company", "sales", "marketing", "accountant", "finance", "software", "developer", "engineer", "business",
        "retail", "bank", "customer", "operations", "administration", "logistics", "procurement", "meraki labs",
    ),
}

# Old/general categories from previous versions. These are converted into the current site filters.
LEGACY_CATEGORY_MAP = {
    "remote": "Remote & International",
    "remote jobs": "Remote & International",
    "ngo": "NGO & Development",
    "ngo & international development": "NGO & Development",
    "ngo & development": "NGO & Development",
    "development": "NGO & Development",
    "government": "Government",
    "internship": "Internships",
    "internships": "Internships",
    "technology": "Private Sector",
    "finance": "Private Sector",
    "healthcare": "NGO & Development",
    "education": "Private Sector",
    "engineering": "Private Sector",
    "sales & marketing": "Private Sector",
    "administration": "Private Sector",
    "other": "",
}

VALID_EMPLOYMENT_TYPES = {
    "full time": "Full-time",
    "full-time": "Full-time",
    "permanent": "Full-time",
    "part time": "Part-time",
    "part-time": "Part-time",
    "contract": "Contract",
    "fixed term": "Contract",
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
    "fully remote": "Remote",
    "hybrid": "Hybrid",
    "on-site": "On-site",
    "onsite": "On-site",
}

TITLE_NOISE_PATTERNS = [
    r"\s*\|\s*(?:apply by|deadline|closing date|applications? close).*?$",
    r"\s*[-–—]\s*(?:apply by|deadline|closing date|applications? close).*?$",
    r"\s*\((?:apply by|deadline|closing date|applications? close)[^)]+\)\s*$",
    r"\s*\bapply\s+by\s+\d{1,2}\s+[A-Za-z]+\s+20\d{2}.*$",
    r"\s*\bdeadline\s*:?.*$",
    r"\s*\bclosing\s+date\s*:?.*$",
]

PURE_HEADING_RE = re.compile(
    r"^(contents|key responsibilities|responsibilities|required skills|qualifications and experience|working arrangements|"
    r"compensation and benefits|application process|important dates|selection process|about the role|about this role|"
    r"job description|duties and responsibilities|requirements|how to apply)$",
    re.I,
)


def clean_text(value: str | None, max_spaces: bool = True) -> str:
    if not value:
        return ""
    text = html.unescape(value).replace("\xa0", " ")
    text = re.sub(r"[\u200b\ufeff]", "", text)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
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
    text = re.sub(r"^(last updated|posted|date|closing date|deadline|apply by|applications? close)\s*:?\s*", "", text, flags=re.I)
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
        r"(?:apply by|closing date|deadline|applications? close|application deadline)\s*:?\s*([^\n\.;|]{4,80})",
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


def _split_hiring_title(raw_title: str) -> tuple[str | None, str | None]:
    title = clean_text(raw_title)
    for pattern in TITLE_NOISE_PATTERNS:
        title = re.sub(pattern, "", title, flags=re.I)
    title = clean_text(title)
    patterns = [
        r"^(?P<company>.+?)\s+(?:is|are)\s+hiring\s+(?:(?:for|to fill)\s+)?(?:a|an|the)?\s*(?P<title>.+)$",
        r"^(?P<company>.+?)\s+(?:seeks|is seeking|are seeking)\s+(?:a|an|the)?\s*(?P<title>.+)$",
        r"^(?P<title>.+?)\s+(?:at|with)\s+(?P<company>[A-Z][A-Za-z0-9 &.,'’/-]{2,80})$",
    ]
    for pattern in patterns:
        match = re.search(pattern, title, flags=re.I)
        if match:
            company = clean_text(match.groupdict().get("company"))
            role = clean_text(match.groupdict().get("title"))
            role = re.sub(r"^(?:for\s+)?(?:the\s+)?(?:position|role)\s+of\s+", "", role, flags=re.I)
            if company and role and 2 <= len(company) <= 100 and 5 <= len(role) <= 140:
                return company, role
    return None, title


def clean_job_title(raw_title: str | None, company: str | None = None, text: str | None = None) -> str:
    """Remove source marketing/deadline wording from a scraped title.

    Example:
    "Meraki Labs is hiring a Communications and Reporting Officer (Remote) | Apply by 22 June 2026"
    becomes "Communications and Reporting Officer (Remote)".
    """
    title = clean_text(raw_title)
    if not title:
        return "Untitled role"
    _, role = _split_hiring_title(title)
    title = role or title
    title = re.sub(r"^(job title|position|role|vacancy)\s*:\s*", "", title, flags=re.I)
    for pattern in TITLE_NOISE_PATTERNS:
        title = re.sub(pattern, "", title, flags=re.I)
    if company:
        title = re.sub(rf"^\s*{re.escape(clean_text(company))}\s*[-–—:|]\s*", "", title, flags=re.I)
    title = re.sub(r"\s*\|\s*.*$", "", title) if re.search(r"\|\s*(apply|deadline|closing|job)", title, re.I) else title
    title = re.sub(r"\s+", " ", title).strip(" -–—:|")
    # Keep useful workplace tags like (Remote), but remove pure deadline/date tags.
    title = re.sub(r"\s*\((?:deadline|closing|apply)[^)]+\)", "", title, flags=re.I).strip()
    max_len = int(os.getenv("MAX_TITLE_CHARS", "95"))
    if len(title) > max_len:
        title = title[:max_len].rsplit(" ", 1)[0].rstrip(".,;:-")
    return title or clean_text(raw_title)[:max_len]


def infer_company(title: str, text: str | None = None) -> str:
    company, _ = _split_hiring_title(title)
    if company:
        return company
    joined = f"{title}\n{text or ''}"
    rules = [
        r"(?:job|vacancy|opening)\s+at\s+([^\|\n\-–—:]+)",
        r"at\s+([^\|\n\-–—:]{2,80})\s*(?:\||-|–|—|$)",
        r"(?:Company|Organisation|Organization|Employer)\s*:?\s*([^\n]{2,80})",
        r"(?:About|Company)\s+([A-Z][A-Za-z0-9 &.,'’/-]{2,80})",
    ]
    for pattern in rules:
        match = re.search(pattern, joined, flags=re.I | re.M)
        if match:
            company = clean_text(match.group(1))
            company = re.sub(r"\s+(?:job|vacancy|role|position)$", "", company, flags=re.I)
            company = re.sub(r"\s*\|.*$", "", company).strip()
            if 2 <= len(company) <= 100:
                return company
    return "Confidential"


def normalize_location(value: str | None, title: str = "", text: str = "", default: str = "Zimbabwe") -> str:
    combined = clean_text(" ".join([value or "", title, text[:900]]))
    if re.search(r"remote|home[- ]based|work from home|fully remote", combined, flags=re.I):
        if re.search(r"zimbabwe|harare|bulawayo", combined, flags=re.I):
            return "Remote / Zimbabwe"
        if re.search(r"africa|emea|south african standard time|sast|global south", combined, flags=re.I):
            return "Remote / Africa"
        return "Remote"
    if re.search(r"hybrid", combined, flags=re.I):
        for place in ["Harare", "Bulawayo", "Zimbabwe", "South Africa"]:
            if re.search(rf"\b{re.escape(place)}\b", combined, flags=re.I):
                return f"Hybrid / {place}"
        return "Hybrid"
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


def normalize_category(raw_category: str | None, title: str, location: str, text: str, default: str = "Private Sector") -> str:
    raw = clean_text(raw_category)
    mapped = LEGACY_CATEGORY_MAP.get(raw.lower(), raw if raw in SITE_CATEGORIES else "")
    inferred = infer_category(title, f"{location}\n{text}", default=default)
    # Remote/internship signals should override vague source categories like Other/Private Sector.
    if inferred in {"Remote & International", "Internships"}:
        return inferred
    if mapped:
        return mapped
    return inferred


def _contains_term(text: str, term: str) -> bool:
    term = term.lower().strip()
    if not term:
        return False
    # Use word-ish boundaries so "intern" does not match "internal" or "international".
    if re.fullmatch(r"[a-z0-9 &/+-]+", term):
        return re.search(rf"(?<![a-z0-9]){re.escape(term)}(?![a-z0-9])", text) is not None
    return term in text


def infer_category(title: str, text: str, default: str = "Private Sector") -> str:
    combined = f"{title} {text}".lower()
    # Priority matters because one job can be an NGO remote job; the site filter should still catch it as remote.
    priority = ["Internships", "Remote & International", "Government", "NGO & Development", "Private Sector"]
    for category in priority:
        terms = CATEGORY_RULES[category]
        if any(_contains_term(combined, term) for term in terms):
            return category
    default = LEGACY_CATEGORY_MAP.get(clean_text(default).lower(), default)
    return default if default in SITE_CATEGORIES else "Private Sector"


def extract_salary(text: str) -> str | None:
    patterns = [
        r"(?:salary|compensation|pay)\s*:?\s*([^\n]{0,20}(?:USD|US\$|\$|EUR|£)[^\n]{2,80})",
        r"((?:USD|US\$|\$|EUR|£)\s?[\d,]+(?:\s?[–\-]\s?(?:USD|US\$|\$|EUR|£)?\s?[\d,]+)?\s*(?:per annum|per year|/year|/month|per month|/hour|per hour)?)",
    ]
    for pattern in patterns:
        match = re.search(pattern, text, flags=re.I)
        if match:
            return clean_text(match.group(1))[:120]
    return None


def _line_is_toc_or_heading(line: str) -> bool:
    bare = clean_text(re.sub(r"^[•\-*]\s*", "", line))
    if not bare:
        return True
    if PURE_HEADING_RE.match(bare):
        return True
    # TOC-generated lines are often short title-cased headings without sentence punctuation.
    if len(bare) <= 72 and not re.search(r"[:.!?]", bare) and re.search(r"[A-Za-z]", bare):
        words = bare.split()
        if 2 <= len(words) <= 8 and sum(1 for w in words if w[:1].isupper()) >= max(1, len(words) - 2):
            return True
    return False


def _clean_summary_lines(text: str, title: str = "") -> list[str]:
    title_norm = clean_text(title).lower()
    cleaned_title_norm = clean_job_title(title).lower() if title else ""
    lines = [clean_text(line) for line in text.splitlines()]
    out: list[str] = []
    seen: set[str] = set()
    in_toc = False
    toc_skipped = 0
    for raw_line in lines:
        line = clean_text(raw_line)
        if not line:
            continue
        line = re.sub(r"^Selection Process\s+(Job Title\s*:)", r"\1", line, flags=re.I)
        lower = line.lower()
        if lower in {title_norm, cleaned_title_norm}:
            continue
        if re.search(r"^(contents)$", line, re.I):
            in_toc = True
            toc_skipped = 0
            continue
        if in_toc:
            # Drop bullet/heading blocks after a generated table of contents until real content starts.
            if _line_is_toc_or_heading(line) and toc_skipped < 30:
                toc_skipped += 1
                continue
            in_toc = False
        if _line_is_toc_or_heading(line):
            continue
        if re.search(r"^(share on|copy link|save job|apply on official site|always apply on the official source)", line, re.I):
            continue
        if re.search(r"^(source|official source)\s*:", line, re.I):
            continue
        # Remove redundant breadcrumbs and source/site noise.
        if re.search(r"applynow|zimjobs hub|privacy policy|terms of use|cookie", line, re.I) and len(line) < 120:
            continue
        line = re.sub(r"\s+", " ", line).strip()
        key = re.sub(r"\W+", " ", line.lower()).strip()
        if key in seen:
            continue
        seen.add(key)
        out.append(line)
    return out


def make_summary(title: str, description: str, max_chars: int = 700) -> str:
    text = clean_text(description, max_spaces=False)
    if not text:
        return title
    boilerplate_patterns = [
        r"By using this site, you agree to the Privacy Policy and Terms of Use\.?.*?$",
        r"Disclaimer: ApplyNOW is not the hiring organization.*?end\.",
        r"Master the recruitment process of the impact sector!.*?Get started",
        r"Subscribe to .*? job alerts.*?$",
    ]
    for pattern in boilerplate_patterns:
        text = re.sub(pattern, "", text, flags=re.I | re.S)
    lines = _clean_summary_lines(text, title=title)
    if not lines:
        return clean_job_title(title)

    # Put proper descriptive sentences first, then compact useful labelled facts.
    descriptive = [line for line in lines if len(line) >= 70 and not re.match(r"^[A-Za-z /]+\s*:", line)]
    facts = [line for line in lines if re.match(r"^(Job Title|Location|Closing Date|Deadline|Contract Type|Employment Type|Salary|Expected Start Date)\s*:", line, re.I)]
    bullets = [line for line in lines if line.startswith("•") and len(line) >= 45]

    selected: list[str] = []
    for group in (descriptive[:3], facts[:6], bullets[:4]):
        for line in group:
            if line not in selected:
                selected.append(line)

    if not selected:
        selected = lines[:8]

    summary = "\n".join(selected).strip()
    summary = re.sub(r"\n{3,}", "\n\n", summary)
    if len(summary) <= max_chars:
        return summary
    return summary[:max_chars].rsplit(" ", 1)[0].rstrip(".,; ") + "…"


def content_hash(values: Iterable[str | None]) -> str:
    joined = "|".join(clean_text(v).lower() for v in values if v)
    joined = re.sub(r"\W+", " ", joined)
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()
