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

NOISE_LINE_RE = re.compile(
    r"^(?:"
    r"home|menu|jobs?|latest jobs?|all jobs?|browse jobs?|job search|search jobs?|search results|"
    r"job categories|categories|category\s*:?.*|"
    r"showing\s+\d+.*|(?:\d+\s+)?jobs?\s+found|sort by|filter by|filters?|"
    r"load more|read more|view details?|previous|next|back to jobs?|"
    r"login|register|sign in|candidate login|employer login|"
    r"privacy policy|terms(?: of use)?|cookie policy|all rights reserved|"
    r"subscribe.*|newsletter|share(?: on)?(?: facebook| twitter| linkedin| whatsapp)?|copy link|"
    r"save job|apply now|apply on official site|always apply on the official source.*"
    r")$",
    re.I,
)

INLINE_BULLET_RE = re.compile(r"(?<!^)\s+[•●▪◦‣]\s+")
BULLET_PREFIX_RE = re.compile(r"^\s*(?:[•●▪◦‣]+|[-*]+)\s+")


ROLE_KEYWORDS = (
    "officer", "coordinator", "manager", "assistant", "associate", "analyst", "specialist", "developer",
    "engineer", "designer", "accountant", "consultant", "advisor", "adviser", "lead", "director",
    "administrator", "clerk", "driver", "nurse", "teacher", "lecturer", "intern", "trainee", "technician",
    "supervisor", "representative", "executive", "researcher", "auditor", "cashier", "receptionist",
    "operator", "monitor", "enumerator", "facilitator", "controller", "agent", "architect", "scientist",
    "drivers", "operators", "agronomist", "mechanic", "mechanics", "apprentice", "apprenticeship",
    "learnership", "consultants", "guard", "guards", "salesperson", "sales", "artisan", "fitter",
    "turner", "radiographer", "sergeant",
)

GENERIC_TITLE_RE = re.compile(
    r"^(?:jobs?\s*\|\s*somewhere|jobs?|vacanc(?:y|ies)|multiple\s+vacanc(?:y|ies)|"
    r"\d+\s+(?:new\s+)?job\s+positions?|career\s+opportunities?|open\s+positions?|"
    r"various\s+positions?|latest\s+jobs?|remote\s+jobs?|talent\s+on[- ]demand|hire\s+remote\s+professionals)\b",
    re.I,
)

TASK_FRAGMENT_TITLE_RE = re.compile(
    r"^(?:"
    r"manage|monitor|examine|develop|conduct|provide|coordinate|ensure|maintain|prepare|"
    r"assist|work|report|oversee|implement|facilitate|perform|review|analy[sz]e|deliver|"
    r"carry\s+out"
    r")\b",
    re.I,
)

PROMO_TITLE_SUFFIX_PATTERNS = [
    r"\s*\|\s*(?:earn|salary|pay|compensation)\b.*$",
    r"\s*[-–—]\s*(?:earn|salary|pay|compensation)\b.*$",
    r"\s*\|\s*(?:remote|work from home|home based)\b.*$",
]

BAD_COMPANY_RE = re.compile(
    r"^(?:is|are|was|were|be|being|been|to|and|or|the\s+organisation|the\s+organization|the\s+job|"
    r"implementing|inviting|seeking|looking|hiring|operating|aimed|based|committed|dedicated)\b",
    re.I,
)


def looks_like_real_role(value: str | None) -> bool:
    title = clean_text(value)
    if not title or GENERIC_TITLE_RE.search(title):
        return False
    if len(title) < 5 or len(title) > 120:
        return False
    if TASK_FRAGMENT_TITLE_RE.search(title):
        return False
    if re.search(r"\b(?:qualification|qualifications|required|responsible for|duties include|minimum of|experience in|such as)\b", title, re.I):
        return False
    if re.search(r"\b(?:is|are)\s+hiring\b|\bapply\s+by\b|\bdeadline\b|\bclosing\s+date\b", title, re.I):
        return False
    if re.search(r"\b(?:jobs?|vacanc(?:y|ies)|positions?)\b", title, re.I) and not any(
        re.search(rf"\b{re.escape(k)}\b", title, re.I) for k in ROLE_KEYWORDS
    ):
        return False
    return any(re.search(rf"\b{re.escape(k)}\b", title, re.I) for k in ROLE_KEYWORDS)


def looks_like_good_company(value: str | None) -> bool:
    company = clean_text(value)
    if not company or company.lower() in {"confidential", "n/a", "unknown"}:
        return False
    if len(company) < 2 or len(company) > 90:
        return False
    if BAD_COMPANY_RE.search(company):
        return False
    if re.search(r"\b(?:is|are|was|were|aimed|inviting|seeking|implementing|operating|supporting)\b", company, re.I):
        # Good company names can contain small words, but not obvious sentence verbs.
        return False
    if company.count(" ") >= 8:
        return False
    if re.search(r"[.!?]", company):
        return False
    return bool(re.search(r"[A-Za-z0-9]", company))


def _strip_title_noise(title: str) -> str:
    title = clean_text(title)
    for pattern in TITLE_NOISE_PATTERNS + PROMO_TITLE_SUFFIX_PATTERNS:
        title = re.sub(pattern, "", title, flags=re.I)
    title = re.sub(r"\s*\((?:remote|hybrid|onsite|on-site|home[- ]based|work from home)\)\s*", " ", title, flags=re.I)
    title = re.sub(r"\s+[-–—|:]\s*$", "", title)
    return clean_text(title)


def extract_role_from_text(text: str | None) -> str | None:
    body = clean_text(text or "", max_spaces=False)
    if not body:
        return None
    labels = (
        "Job Title", "Position Title", "Position", "Role Title", "Role", "Vacancy", "Post", "Title", "Job Opportunity"
    )
    for label in labels:
        # Only accept proper labelled fields at the start of a line, not inline prose.
        pattern = rf"(?im)^\s*(?:[•\-*]\s*)?{re.escape(label)}\s*:\s*([^\n|•]+)"
        for match in re.finditer(pattern, body):
            candidate = _strip_title_noise(match.group(1))
            if looks_like_real_role(candidate):
                return candidate
    lines = [clean_text(re.sub(r"^[•\-*]\s*", "", line)) for line in body.splitlines()]
    for index, line in enumerate(lines):
        if not any(re.fullmatch(rf"{re.escape(label)}\s*:?", line, flags=re.I) for label in labels):
            continue
        for next_line in lines[index + 1 : index + 4]:
            candidate = _strip_title_noise(next_line)
            if looks_like_real_role(candidate):
                return candidate
    # Some ApplyNOW pages have a TOC line like "Operations Coordinator (Remote) – Company".
    for line in lines:
        if not line or len(line) > 130:
            continue
        for label in labels:
            inline = re.search(rf"{re.escape(label)}\s*:\s*(.+)$", line, flags=re.I)
            if inline:
                candidate = _strip_title_noise(inline.group(1))
                if looks_like_real_role(candidate):
                    return candidate
        if re.match(r"^(?:how to apply|about|contents|the role)$", line, re.I):
            continue
        line = re.sub(r"\s+[–—-]\s+[A-Z][A-Za-z0-9 &.'’/-]{2,80}$", "", line)
        candidate = _strip_title_noise(line)
        if looks_like_real_role(candidate):
            return candidate
    return None


def extract_company_from_text(title: str | None, text: str | None) -> str | None:
    heading = clean_text(title)
    body = clean_text(text or "", max_spaces=False)
    # ApplyNOW pattern: "Chewore Conservation Trust Zimbabwe Jobs June 2026 – Multiple Vacancies"
    company_jobs_pattern = r"^([A-Z0-9][A-Za-z0-9 &.'’/-]{2,90}?)\s+(?:Zimbabwe\s+)?Jobs?\s+(?:[A-Za-z]+\s+20\d{2}|20\d{2})\s+[–—-]\s+(?:Multiple\s+Vacanc(?:y|ies)|\d+\s+(?:new\s+)?job\s+positions?)"
    for candidate_text in [heading, *body.splitlines()[:8]]:
        match = re.search(company_jobs_pattern, clean_text(candidate_text), flags=re.I)
        if match and looks_like_good_company(match.group(1)):
            return clean_text(match.group(1))
    # Headline pattern: "The Self-Investigation is hiring: Operations Coordinator ..."
    company, _ = _split_hiring_title(heading)
    if looks_like_good_company(company):
        return company
    rules = [
        r"(?im)^\s*(?:Company|Organisation|Organization|Employer)\s*:\s*([^\n]{2,90})",
        r"(?im)^\s*About\s+([A-Z0-9][A-Za-z0-9 &.'’/-]{2,90}?)(?:\s+The\s+(?:organisation|organization|company)\b|\n|$)",
        r"(?im)^\s*([A-Z0-9][A-Za-z0-9 &.'’/-]{2,90}?)\s+is\s+(?:inviting|seeking|looking|hiring|recruiting)\b",
        r"(?im)^\s*([A-Z0-9][A-Za-z0-9 &.'’/-]{2,90}?)\s*,\s+(?:operating|based|a\s+registered|an?\s+international)",
    ]
    for pattern in rules:
        for match in re.finditer(pattern, body):
            candidate = clean_text(match.group(1))
            candidate = re.sub(r"\s+(?:Zimbabwe\s+)?Jobs?$", "", candidate, flags=re.I)
            if looks_like_good_company(candidate):
                return candidate
    return None


def clean_text(value: str | None, max_spaces: bool = True) -> str:
    if not value:
        return ""
    text = html.unescape(str(value)).replace("\xa0", " ")
    text = re.sub(r"[\u200b\ufeff]", "", text)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    if max_spaces:
        text = re.sub(r"\s+", " ", text)
    else:
        text = re.sub(r"[ \t\f\v]+", " ", text)
        text = "\n".join(line.strip() for line in text.split("\n"))
        text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip(" \t\r\n-|•")


def _clean_job_text_line(line: str) -> str:
    line = html.unescape(line).replace("\xa0", " ")
    line = re.sub(r"[\u200b\ufeff]", "", line)
    line = re.sub(r"[ \t\f\v]+", " ", line).strip()
    if not line:
        return ""
    return BULLET_PREFIX_RE.sub("• ", line)


def _is_unrelated_line(line: str) -> bool:
    cleaned = clean_text(re.sub(r"^[•\-*]\s*", "", line))
    if not cleaned:
        return True
    if cleaned in SITE_CATEGORIES:
        return True
    if NOISE_LINE_RE.match(cleaned):
        return True
    if re.match(r"^(?:NGO|Government|Private Sector|Remote|Internship)s?\s+Jobs?$", cleaned, re.I):
        return True
    return False


def normalize_job_text(
    value: str | None,
    *,
    max_chars: int | None = None,
    remove_duplicate_lines: bool = True,
    remove_noise: bool = True,
) -> str:
    """Normalize scraped multiline job text without preserving source-page spacing.

    This keeps one blank line at most, normalizes bullets, removes common listing
    page chrome, and drops repeated lines caused by cards/headers being scraped
    more than once.
    """
    if not value:
        return ""
    text = html.unescape(str(value)).replace("\xa0", " ")
    text = re.sub(r"[\u200b\ufeff]", "", text)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = INLINE_BULLET_RE.sub("\n• ", text)
    text = re.sub(r"[ \t\f\v]+", " ", text)

    lines: list[str] = []
    seen: set[str] = set()
    pending_blank = False
    for raw_line in text.split("\n"):
        line = _clean_job_text_line(raw_line)
        if not line:
            if lines:
                pending_blank = True
            continue
        if remove_noise and _is_unrelated_line(line):
            continue
        key = re.sub(r"\W+", " ", line.lower()).strip()
        if remove_duplicate_lines and key and key in seen:
            continue
        seen.add(key)
        if pending_blank and lines and lines[-1] != "":
            lines.append("")
        lines.append(line)
        pending_blank = False

    text = "\n".join(lines).strip()
    text = re.sub(r"\n{3,}", "\n\n", text)
    if max_chars and len(text) > max_chars:
        text = text[:max_chars].rsplit(" ", 1)[0].rstrip(".,;:-")
    return text


def clean_html_to_markdownish(value: str | None) -> str:
    if not value:
        return ""
    soup = BeautifulSoup(value, "html.parser")
    for bad in soup(["script", "style", "noscript", "svg", "form", "iframe"]):
        bad.decompose()
    for hidden in soup.find_all(attrs={"hidden": True}):
        hidden.decompose()
    for node in soup.find_all(attrs={"aria-hidden": "true"}):
        node.decompose()
    for node in soup.find_all(style=True):
        if re.search(r"display\s*:\s*none|visibility\s*:\s*hidden", node.get("style", ""), re.I):
            node.decompose()
    for br in soup.find_all("br"):
        br.replace_with("\n")
    for li in soup.find_all("li"):
        li.insert_before("\n• ")
        li.insert_after("\n")
    for heading in soup.find_all(re.compile("^h[1-6]$")):
        heading.insert_before("\n\n")
        heading.insert_after("\n")
    for block in soup.find_all(["p", "div", "section", "article", "tr"]):
        block.insert_after("\n")
    return normalize_job_text(soup.get_text("\n"))


def normalize_url(url: str | None, base_url: str | None = None) -> str:
    if not url:
        return ""
    cleaned = re.sub(r"[\x00-\x20\x7f]+", "", str(url).strip())
    if not cleaned or len(cleaned) > 2048:
        return ""
    absolute = urljoin(base_url or "", cleaned)
    parsed = urlparse(absolute)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
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


def extract_labeled_value(text: str | None, labels: Iterable[str], max_chars: int = 160) -> str | None:
    body = clean_text(text or "", max_spaces=False)
    if not body:
        return None
    for label in labels:
        pattern = rf"(?im)^\s*(?:[•\-*]\s*)?{re.escape(label)}\s*:?\s*([^\n]+)"
        match = re.search(pattern, body)
        if match:
            value = clean_text(match.group(1))
            if value and len(value) <= max_chars:
                return value
    return None


def extract_section(text: str | None, headings: Iterable[str], max_chars: int = 1600) -> str | None:
    """Extract a compact section body after one of the supplied line headings."""
    body = clean_text(text or "", max_spaces=False)
    if not body:
        return None
    heading_alt = "|".join(re.escape(h) for h in headings)
    next_heading = (
        r"responsibilities|duties|requirements|qualifications|skills|experience|education|"
        r"how to apply|application process|deadline|closing date|about|benefits|salary|location|"
        r"job description|key competencies|selection process"
    )
    pattern = rf"(?ims)^\s*(?:{heading_alt})\s*:?\s*(.+?)(?=^\s*(?:{next_heading})\s*:?\s*$|\Z)"
    match = re.search(pattern, body)
    if not match:
        return None
    section = clean_text(match.group(1), max_spaces=False)
    lines = []
    for raw_line in section.splitlines():
        line = clean_text(raw_line)
        if not line:
            continue
        if PURE_HEADING_RE.match(line):
            break
        lines.append(line)
    value = "\n".join(lines).strip()
    if not value:
        return None
    return value[:max_chars].rsplit(" ", 1)[0].rstrip(".,; ") if len(value) > max_chars else value


def is_expired(expires_at: str | None) -> bool:
    if not expires_at:
        return False
    try:
        return datetime.fromisoformat(expires_at).date() < datetime.now(timezone.utc).date()
    except ValueError:
        return False


def _split_hiring_title(raw_title: str) -> tuple[str | None, str | None]:
    title = _strip_title_noise(raw_title)
    patterns = [
        r"^(?P<company>.+?)\s+(?:is|are)\s+hiring\s*:?\s*(?:(?:for|to fill)\s+)?(?:a|an|the)?\s*(?P<title>.+)$",
        r"^(?P<company>.+?)\s+hiring\s*:?\s*(?:(?:for|to fill)\s+)?(?:a|an|the)?\s*(?P<title>.+)$",
        r"^(?P<company>.+?)\s+(?:seeks|is seeking|are seeking|is recruiting|are recruiting)\s*:?\s*(?:a|an|the)?\s*(?P<title>.+)$",
        r"^(?P<title>.+?)\s+(?:at|with)\s+(?P<company>[A-Z0-9][A-Za-z0-9 &.,'’/-]{2,80})$",
    ]
    # The \u000e placeholder is removed below; it makes this raw patch easier to read.
    patterns = [p.replace("\u000e", "") for p in patterns]
    for pattern in patterns:
        match = re.search(pattern, title, flags=re.I)
        if match:
            company = clean_text(match.groupdict().get("company"))
            role = clean_text(match.groupdict().get("title"))
            role = re.sub(r"^(?:for\s+)?(?:the\s+)?(?:position|role|post)\s+of\s+", "", role, flags=re.I)
            role = _strip_title_noise(role)
            if looks_like_good_company(company) and looks_like_real_role(role):
                return company, role
    return None, title

def clean_job_title(raw_title: str | None, company: str | None = None, text: str | None = None) -> str:
    """Return a concise role title only, with company, deadline, salary and remote tags removed."""
    original = clean_text(raw_title)
    body = clean_text(text or "", max_spaces=False)
    if not original:
        candidate = extract_role_from_text(body)
        return candidate or "Untitled role"

    company_from_title, role = _split_hiring_title(original)
    title = role or original
    title = re.sub(r"^(job title|position title|position|role title|role|vacancy|post)\s*:\s*", "", title, flags=re.I)
    title = _strip_title_noise(title)

    good_company = clean_text(company) or company_from_title or ""
    if good_company:
        title = re.sub(rf"^\s*{re.escape(clean_text(good_company))}\s*[-–—:|]\s*", "", title, flags=re.I)
        title = _strip_title_noise(title)

    title = re.sub(r"\s+vacanc(?:y|ies)\s*(?:20\d{2})?\b.*$", "", title, flags=re.I)

    extracted = extract_role_from_text(body)
    if extracted and (
        not looks_like_real_role(title)
        or re.search(r"\b(?:hiring|recruiting|paying|earn|salary|compensation|usd|eur|gbp)\b", original, re.I)
    ):
        title = extracted

    max_len = int(os.getenv("MAX_TITLE_CHARS", "80"))
    if len(title) > max_len:
        title = title[:max_len].rsplit(" ", 1)[0].rstrip(".,;:-")
    return clean_text(title) or clean_text(raw_title)[:max_len]

def infer_company(title: str, text: str | None = None) -> str:
    extracted = extract_company_from_text(title, text)
    if extracted:
        return extracted
    company, _ = _split_hiring_title(title)
    if looks_like_good_company(company):
        return company
    joined = f"{title}\n{text or ''}"
    rules = [
        r"(?im)^\s*(?:job|vacancy|opening)\s+at\s+([^\|\n\-–—:]{2,80})",
        r"(?im)^\s*(?:Company|Organisation|Organization|Employer)\s*:\s*([^\n]{2,80})",
        r"(?im)^\s*About\s+([A-Z0-9][A-Za-z0-9 &.,'’/-]{2,80}?)(?:\s+The\s+(?:organisation|organization|company)\b|\n|$)",
    ]
    for pattern in rules:
        match = re.search(pattern, joined, flags=re.I | re.M)
        if match:
            company = clean_text(match.group(1))
            company = re.sub(r"\s+(?:job|vacancy|role|position)$", "", company, flags=re.I)
            company = re.sub(r"\s*\|.*$", "", company).strip()
            if looks_like_good_company(company):
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
    text = normalize_job_text(description)
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
    lines = _clean_summary_lines(normalize_job_text(text), title=title)
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

    summary = normalize_job_text("\n".join(selected).strip())
    if len(summary) <= max_chars:
        return summary
    return summary[:max_chars].rsplit(" ", 1)[0].rstrip(".,; ") + "…"


def repeated_labeled_values(text: str | None, labels: Iterable[str]) -> dict[str, set[str]]:
    body = normalize_job_text(text)
    repeated: dict[str, set[str]] = {}
    for label in labels:
        pattern = rf"(?im)^\s*(?:[•\-*]\s*)?{re.escape(label)}\s*:?\s*([^\n]{{2,140}})"
        values = {
            re.sub(r"\W+", " ", clean_text(match.group(1)).lower()).strip()
            for match in re.finditer(pattern, body)
            if clean_text(match.group(1))
        }
        values.discard("")
        if len(values) > 1:
            repeated[label] = values
    return repeated


def is_probable_merged_job_text(title: str | None, text: str | None) -> bool:
    body = normalize_job_text(text)
    if not body:
        return False
    if len(body) > 12000:
        return True
    if repeated_labeled_values(
        body,
        ["Job Title", "Position Title", "Position", "Role Title", "Role", "Vacancy", "Post"],
    ):
        return True

    title_key = re.sub(r"\W+", " ", clean_text(title).lower()).strip()
    role_lines: set[str] = set()
    category_or_listing_lines = 0
    for raw_line in body.splitlines():
        line = clean_text(re.sub(r"^[•\-*]\s*", "", raw_line))
        if not line:
            continue
        if _is_unrelated_line(line) or re.search(r"\b(?:jobs found|showing|filter by|sort by|latest jobs)\b", line, re.I):
            category_or_listing_lines += 1
            continue
        if len(line) > 140 or re.search(r"\b(?:deadline|closing date|apply by|salary|source)\b", line, re.I):
            continue
        if looks_like_real_role(line):
            key = re.sub(r"\W+", " ", line.lower()).strip()
            if key and key != title_key:
                role_lines.add(key)

    if len(role_lines) >= 3:
        return True
    return category_or_listing_lines >= 3 and len(role_lines) >= 2


def content_hash(values: Iterable[str | None]) -> str:
    joined = "|".join(clean_text(v).lower() for v in values if v)
    joined = re.sub(r"\W+", " ", joined)
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()
BAD_SCRAPED_EXACT_MARKERS = (
    "RMTUyLjU1LjE3Ny44Mw==",
)

BAD_SCRAPED_PHRASES = (
    "please mention the word",
    "show you read the job post completely",
    "beta feature to avoid spam applicants",
    "companies can search these words",
    "see this and similar jobs on linkedin",
)

BAD_SCRAPED_URL_PREFIXES = (
    "https://remoteok.com/remote-jobs",
    "http://remoteok.com/remote-jobs",
    "https://www.remoteok.com/remote-jobs",
    "http://www.remoteok.com/remote-jobs",
)

BAD_SCRAPED_URL_NETLOCS = {
    "remoteok.com",
    "www.remoteok.com",
}


def has_bad_scraped_content(*values: str | None) -> bool:
    text = "\n".join(str(value or "") for value in values)
    text_lower = text.lower()
    if any(marker in text for marker in BAD_SCRAPED_EXACT_MARKERS):
        return True
    if any(phrase in text_lower for phrase in BAD_SCRAPED_PHRASES):
        return True
    if re.search(r"please mention the word\b.{0,240}\btag\s+[a-z0-9+/=]{12,}", text_lower, re.I | re.S):
        return True

    for raw_value in values:
        value = str(raw_value or "").strip()
        if not value:
            continue
        lowered = value.lower()
        if any(lowered.startswith(prefix) for prefix in BAD_SCRAPED_URL_PREFIXES):
            return True
        parsed = urlparse(value)
        if parsed.netloc.lower() in BAD_SCRAPED_URL_NETLOCS and parsed.path.lower().startswith("/remote-jobs"):
            return True
    return False
