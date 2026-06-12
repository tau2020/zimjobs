#!/usr/bin/env bash
# scrape_jobs.sh — Scrape public Zimbabwean job listings into an existing SQLite jobs table.
#
# Usage:
#   chmod +x scrape_jobs.sh
#   ./scrape_jobs.sh /path/to/jobs.db
#
# Cron example:
#   15 6 * * * /path/to/scrape_jobs.sh /path/to/jobs.db >> /var/log/zimjobs-scraper.log 2>&1
#
# Requirements:
#   bash, curl, python3, sqlite3
#
# Notes:
#   - Uses only public pages.
#   - Uses polite request headers, timeouts, and small delays.
#   - Uses inline Python standard library only.
#   - Deduplicates by apply_url before inserting.
#   - Exits 0 after run even if one source fails.
#   - Exits non-zero only for setup/configuration errors.

set -u
IFS=$'\n\t'

DB_PATH="${1:-}"

USER_AGENT="Mozilla/5.0 (compatible; ZimJobsBot/1.0; +https://zimjobs.online)"
CONNECT_TIMEOUT=12
MAX_TIME=35
SLEEP_BETWEEN_REQUESTS=2

log() {
  printf '[%s] %s\n' "$(date -u '+%Y-%m-%d %H:%M:%S UTC')" "$*"
}

die() {
  log "ERROR: $*"
  exit 1
}

require_cmd() {
  command -v "$1" >/dev/null 2>&1 || die "Required command not found: $1"
}

if [ -z "$DB_PATH" ]; then
  die "Missing database path. Usage: ./scrape_jobs.sh /path/to/jobs.db"
fi

require_cmd bash
require_cmd curl
require_cmd python3
require_cmd sqlite3
require_cmd mktemp

if [ ! -f "$DB_PATH" ]; then
  die "Database file does not exist: $DB_PATH"
fi

if [ ! -r "$DB_PATH" ] || [ ! -w "$DB_PATH" ]; then
  die "Database file must be readable and writable: $DB_PATH"
fi

sqlite3 "$DB_PATH" "SELECT name FROM sqlite_master WHERE type='table' AND name='jobs';" | grep -qx "jobs" \
  || die "Database does not contain required table: jobs"

MISSING_COLUMNS="$(
sqlite3 "$DB_PATH" <<'SQL'
.mode list
WITH required(name) AS (
  VALUES
  ('id'), ('title'), ('company'), ('location'), ('category'),
  ('summary'), ('apply_url'), ('featured'), ('created_at')
),
actual(name) AS (
  SELECT name FROM pragma_table_info('jobs')
)
SELECT group_concat(required.name, ', ')
FROM required
LEFT JOIN actual USING(name)
WHERE actual.name IS NULL;
SQL
)"

if [ -n "$MISSING_COLUMNS" ]; then
  die "jobs table is missing required column(s): $MISSING_COLUMNS"
fi

TMP_DIR="$(mktemp -d)"
trap 'rm -rf "$TMP_DIR"' EXIT

log "Starting Zimbabwe jobs scrape"
log "Database: $DB_PATH"

fetch_url() {
  local source_name="$1"
  local url="$2"
  local out_file="$3"

  log "Fetching ${source_name}: ${url}"

  local status
  status="$(
    curl \
      --silent \
      --show-error \
      --location \
      --compressed \
      --fail \
      --connect-timeout "$CONNECT_TIMEOUT" \
      --max-time "$MAX_TIME" \
      --retry 1 \
      --retry-delay 2 \
      --user-agent "$USER_AGENT" \
      --header "Accept: text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8" \
      --header "Accept-Language: en-US,en;q=0.9" \
      --output "$out_file" \
      --write-out "%{http_code}" \
      "$url" 2>"${out_file}.err"
  )"

  local rc=$?

  if [ "$rc" -ne 0 ]; then
    log "WARN: Failed to fetch ${source_name}. curl exit=${rc}. $(tr '\n' ' ' < "${out_file}.err" | sed 's/[[:space:]]\+/ /g')"
    return 1
  fi

  if [ ! -s "$out_file" ]; then
    log "WARN: Empty response from ${source_name}"
    return 1
  fi

  log "Fetched ${source_name} successfully. HTTP ${status}. Size $(wc -c < "$out_file" | tr -d ' ') bytes"
  return 0
}

declare -a SOURCE_ARGS=()

add_source_file() {
  local source_name="$1"
  local source_url="$2"
  local file_path="$3"

  if [ -s "$file_path" ]; then
    SOURCE_ARGS+=("${source_name}|${source_url}|${file_path}")
  fi
}

KUBATANA_FILE="$TMP_DIR/kubatana_jobs.html"
CLASSIFIEDS_FILE="$TMP_DIR/classifieds_jobs.html"
VACANCYMAIL_FILE="$TMP_DIR/vacancymail_jobs.html"
CAREERS_FILE="$TMP_DIR/careers_jobs.html"

if fetch_url "kubatana" "https://kubatana.net/jobs" "$KUBATANA_FILE"; then
  add_source_file "kubatana" "https://kubatana.net/jobs" "$KUBATANA_FILE"
fi

sleep "$SLEEP_BETWEEN_REQUESTS"

if fetch_url "classifieds" "https://www.classifieds.co.zw/zimbabwe-jobs" "$CLASSIFIEDS_FILE"; then
  add_source_file "classifieds" "https://www.classifieds.co.zw/zimbabwe-jobs" "$CLASSIFIEDS_FILE"
fi

sleep "$SLEEP_BETWEEN_REQUESTS"

if fetch_url "vacancymail" "https://vacancymail.co.zw/jobs/" "$VACANCYMAIL_FILE"; then
  add_source_file "vacancymail" "https://vacancymail.co.zw/jobs/" "$VACANCYMAIL_FILE"
fi

sleep "$SLEEP_BETWEEN_REQUESTS"

if fetch_url "careers" "https://www.careers.co.zw/" "$CAREERS_FILE"; then
  add_source_file "careers" "https://www.careers.co.zw/" "$CAREERS_FILE"
fi

if [ "${#SOURCE_ARGS[@]}" -eq 0 ]; then
  log "WARN: No sources downloaded successfully. Nothing to parse."
  log "Done. total_found=0 inserted=0 skipped_duplicates=0 failed=0"
  exit 0
fi

python3 - "$DB_PATH" "${SOURCE_ARGS[@]}" <<'PY'
import sys
import os
import re
import json
import sqlite3
from html import unescape
from html.parser import HTMLParser
from urllib.parse import urljoin, urlparse
from datetime import datetime, timezone

DB_PATH = sys.argv[1]
SOURCE_ARGS = sys.argv[2:]

ALLOWED_CATEGORIES = [
    "Technology",
    "Finance",
    "Healthcare",
    "Education",
    "Engineering",
    "Sales & Marketing",
    "Administration",
    "Other",
]

SOURCE_LIMITS = {
    "kubatana": 40,
    "classifieds": 60,
    "vacancymail": 60,
    "careers": 40,
}

SOURCE_HINTS = {
    "kubatana": {
        "include": [
            "/jobs",
            "/job/",
            "/opportunity",
            "/vacancy",
            "/consultancy",
            "/internship",
        ],
        "exclude": [
            "#",
            "mailto:",
            "tel:",
            "javascript:",
            "/category/",
            "/tag/",
            "/author/",
            "/wp-content/",
            "/wp-json/",
            "/feed",
            "/login",
            "/register",
            "/privacy",
            "/terms",
            "/about",
            "/contact",
        ],
    },
    "classifieds": {
        "include": [
            "/listings/",
            "/zimbabwe-",
            "/jobs",
            "/job",
        ],
        "exclude": [
            "#",
            "mailto:",
            "tel:",
            "javascript:",
            "/login",
            "/register",
            "/sell",
            "/post",
            "/about",
            "/contact",
            "/help",
            "/terms",
            "/privacy",
            "/vehicles",
            "/property",
            "/real-estate",
            "/electronics",
            "/furniture",
        ],
    },
    "vacancymail": {
        "include": [
            "/jobs/",
            "/job/",
            "/vacancy",
            "/categories/",
        ],
        "exclude": [
            "#",
            "mailto:",
            "tel:",
            "javascript:",
            "/login",
            "/register",
            "/candidate",
            "/employer",
            "/resume",
            "/cv",
            "/premium",
            "/testimonials",
            "/terms",
            "/privacy",
            "/contact",
        ],
    },
    "careers": {
        "include": [
            "/jobs",
            "/job",
            "/vacanc",
            "/career",
        ],
        "exclude": [
            "#",
            "mailto:",
            "tel:",
            "javascript:",
            "/login",
            "/register",
            "/candidate",
            "/employer",
            "/resume",
            "/cv",
            "/terms",
            "/privacy",
            "/contact",
        ],
    },
}

JOB_WORDS = re.compile(
    r"\b("
    r"job|jobs|vacancy|vacancies|career|careers|officer|manager|assistant|"
    r"developer|engineer|technician|accountant|finance|nurse|doctor|teacher|"
    r"lecturer|driver|clerk|attachee|intern|graduate|consultant|specialist|"
    r"administrator|receptionist|sales|marketing|auditor|data|ict|it|hr|"
    r"procurement|logistics|mechanic|fitter|electrician|operator|coordinator"
    r")\b",
    re.I,
)

NON_JOB_WORDS = re.compile(
    r"\b("
    r"login|register|sign in|sign up|privacy|terms|cookie|contact|about|"
    r"advertise|post a job|submit cv|upload cv|browse categories|read more|"
    r"home|faq|help|newsletter|whatsapp|facebook|twitter|linkedin|instagram|"
    r"previous|next|search|filter|premium|testimonial|candidate|employer"
    r")\b",
    re.I,
)

ZIM_LOCATIONS = [
    "Harare", "Bulawayo", "Mutare", "Gweru", "Masvingo", "Kwekwe",
    "Kadoma", "Chitungwiza", "Chinhoyi", "Marondera", "Bindura",
    "Victoria Falls", "Zvishavane", "Chegutu", "Norton", "Rusape",
    "Hwange", "Kariba", "Beitbridge", "Gwanda", "Lupane", "Binga",
    "Chipinge", "Chiredzi", "Mvurwi", "Ruwa", "Zimbabwe", "Remote",
    "Mashonaland East", "Mashonaland West", "Mashonaland Central",
    "Matabeleland North", "Matabeleland South", "Manicaland",
    "Midlands", "Masvingo Province",
]

DATE_PATTERNS = [
    re.compile(r"\b(20\d{2})[-/](0?[1-9]|1[0-2])[-/](0?[1-9]|[12]\d|3[01])\b"),
    re.compile(r"\b(0?[1-9]|[12]\d|3[01])[-/](0?[1-9]|1[0-2])[-/](20\d{2})\b"),
]

MONTHS = {
    "jan": 1, "january": 1,
    "feb": 2, "february": 2,
    "mar": 3, "march": 3,
    "apr": 4, "april": 4,
    "may": 5,
    "jun": 6, "june": 6,
    "jul": 7, "july": 7,
    "aug": 8, "august": 8,
    "sep": 9, "sept": 9, "september": 9,
    "oct": 10, "october": 10,
    "nov": 11, "november": 11,
    "dec": 12, "december": 12,
}

MONTH_DATE_PATTERNS = [
    re.compile(r"\b([A-Za-z]{3,9})\s+([0-3]?\d),?\s+(20\d{2})\b", re.I),
    re.compile(r"\b([0-3]?\d)\s+([A-Za-z]{3,9})\s+(20\d{2})\b", re.I),
]


def now_utc():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def clean_text(value):
    if not value:
        return ""
    value = unescape(str(value))
    value = re.sub(r"<[^>]+>", " ", value)
    value = re.sub(r"\s+", " ", value)
    return value.strip(" \t\r\n-–—|•")


def normalize_url(url):
    url = (url or "").strip()
    if not url:
        return ""
    url = url.split("#", 1)[0].strip()
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        return ""
    if not parsed.netloc:
        return ""
    return url


def is_valid_job_url(url):
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        return False
    if not parsed.netloc:
        return False

    lowered = url.lower()
    bad_ext = (
        ".jpg", ".jpeg", ".png", ".gif", ".svg", ".webp", ".pdf",
        ".zip", ".rar", ".css", ".js", ".ico", ".xml",
    )
    if lowered.endswith(bad_ext):
        return False
    if any(x in lowered for x in ["mailto:", "tel:", "javascript:"]):
        return False
    return True


def trim_summary(text, max_len=420):
    text = clean_text(text)
    if not text:
        return ""
    text = re.sub(r"\b(apply now|view job|read more|more details)\b", "", text, flags=re.I)
    text = clean_text(text)
    if len(text) <= max_len:
        return text
    cut = text[:max_len].rsplit(" ", 1)[0]
    return clean_text(cut) + "..."


def title_case_if_needed(title):
    title = clean_text(title)
    if not title:
        return ""
    if len(title) > 120:
        title = title[:120].rsplit(" ", 1)[0]
    if title.isupper() and len(title) > 8:
        return title.title()
    return title


def map_category(title, summary=""):
    text = f"{title} {summary}".lower()

    rules = [
        ("Technology", [
            "software", "developer", "programmer", "frontend", "front-end",
            "backend", "back-end", "fullstack", "full-stack", " it ", "ict",
            "information technology", "data", "database", "systems", "sysadmin",
            "network", "cyber", "security analyst", "web", "digital", " ai ",
            "machine learning", "devops", "cloud", "support engineer",
        ]),
        ("Finance", [
            "accountant", "accounting", "finance", "financial", "audit",
            "auditor", "bank", "banking", "bookkeeper", "bookkeeping",
            "payroll", "tax", "treasury", "credit", "accounts clerk",
        ]),
        ("Healthcare", [
            "nurse", "doctor", "clinic", "clinical", "health", "hospital",
            "pharmacy", "pharmacist", "medical", "midwife", "laboratory",
            "lab scientist", "dentist", "radiographer", "physiotherapist",
        ]),
        ("Education", [
            "teacher", "lecturer", "school", "training", "trainer", "tutor",
            "education", "academic", "instructor", "principal", "headmaster",
            "headmistress", "curriculum", "teaching",
        ]),
        ("Engineering", [
            "engineer", "engineering", "technician", "construction", "civil",
            "mechanical", "electrical", "fitter", "artisan", "builder",
            "surveyor", "architect", "electrician", "maintenance", "scada",
            "solar", "quantity surveyor",
        ]),
        ("Sales & Marketing", [
            "sales", "marketing", "customer", "business development",
            "brand", "digital marketing", "market", "call centre",
            "call center", "client relations", "commercial", "key accounts",
        ]),
        ("Administration", [
            "admin", "administrator", "administration", "receptionist",
            "office", "hr", "human resources", "assistant", "secretary",
            "personal assistant", "pa ", "clerk", "data entry", "operations",
            "procurement", "logistics", "stores", "driver",
        ]),
    ]

    padded = f" {text} "
    for category, keywords in rules:
        for kw in keywords:
            if kw in padded or kw.strip() in text:
                return category
    return "Other"


def guess_location(text):
    text = clean_text(text)
    low = text.lower()
    for loc in ZIM_LOCATIONS:
        if loc.lower() in low:
            if loc.lower() == "remote":
                return "Remote"
            if loc.lower() == "zimbabwe":
                continue
            return loc
    if "remote" in low:
        return "Remote"
    return "Zimbabwe"


def guess_company(title, context):
    context = clean_text(context)

    patterns = [
        r"\bat\s+([A-Z][A-Za-z0-9&.,'’()\/ -]{2,80})",
        r"\bcompany\s*[:\-]\s*([A-Za-z0-9&.,'’()\/ -]{2,80})",
        r"\bemployer\s*[:\-]\s*([A-Za-z0-9&.,'’()\/ -]{2,80})",
        r"\borganisation\s*[:\-]\s*([A-Za-z0-9&.,'’()\/ -]{2,80})",
        r"\borganization\s*[:\-]\s*([A-Za-z0-9&.,'’()\/ -]{2,80})",
    ]

    for pat in patterns:
        m = re.search(pat, context, re.I)
        if m:
            company = clean_text(m.group(1))
            company = re.split(r"\s{2,}| Location | Full Time | Expires | Deadline | Apply ", company, flags=re.I)[0]
            company = clean_text(company)
            if 2 <= len(company) <= 80 and not NON_JOB_WORDS.search(company):
                return company

    pipe_parts = [clean_text(p) for p in re.split(r"\s+[|–—-]\s+", title) if clean_text(p)]
    if len(pipe_parts) >= 2:
        possible = pipe_parts[-1]
        if 2 <= len(possible) <= 80 and not JOB_WORDS.search(possible):
            return possible

    return "Unknown"


def parse_date(text):
    text = clean_text(text)
    if not text:
        return None

    for pat in DATE_PATTERNS:
        m = pat.search(text)
        if m:
            parts = m.groups()
            try:
                if len(parts[0]) == 4:
                    y, mo, d = int(parts[0]), int(parts[1]), int(parts[2])
                else:
                    d, mo, y = int(parts[0]), int(parts[1]), int(parts[2])
                return datetime(y, mo, d, tzinfo=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
            except ValueError:
                pass

    for pat in MONTH_DATE_PATTERNS:
        m = pat.search(text)
        if m:
            try:
                if m.group(1).lower() in MONTHS:
                    mo = MONTHS[m.group(1).lower()]
                    d = int(m.group(2))
                    y = int(m.group(3))
                else:
                    d = int(m.group(1))
                    mo = MONTHS[m.group(2).lower()]
                    y = int(m.group(3))
                return datetime(y, mo, d, tzinfo=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
            except Exception:
                pass

    return None


class LinkCollector(HTMLParser):
    def __init__(self, base_url):
        super().__init__(convert_charrefs=True)
        self.base_url = base_url
        self.links = []
        self._current = None
        self._jsonld = []
        self._in_jsonld = False
        self._jsonld_buf = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs or {})
        tag = tag.lower()

        if tag == "script" and "ld+json" in attrs.get("type", "").lower():
            self._in_jsonld = True
            self._jsonld_buf = []

        if tag == "a":
            href = attrs.get("href") or ""
            url = urljoin(self.base_url, href)
            self._current = {
                "url": normalize_url(url),
                "text": "",
                "title": attrs.get("title", ""),
                "aria": attrs.get("aria-label", ""),
                "class": attrs.get("class", ""),
            }

    def handle_endtag(self, tag):
        tag = tag.lower()

        if tag == "script" and self._in_jsonld:
            raw = "".join(self._jsonld_buf).strip()
            if raw:
                self._jsonld.append(raw)
            self._in_jsonld = False
            self._jsonld_buf = []

        if tag == "a" and self._current:
            text = clean_text(" ".join([
                self._current.get("text", ""),
                self._current.get("title", ""),
                self._current.get("aria", ""),
            ]))
            url = self._current.get("url", "")
            if text and url:
                self.links.append({
                    "url": url,
                    "text": text,
                    "class": self._current.get("class", ""),
                })
            self._current = None

    def handle_data(self, data):
        if self._in_jsonld:
            self._jsonld_buf.append(data)
        if self._current is not None:
            self._current["text"] += " " + data

    @property
    def jsonld(self):
        return self._jsonld


def extract_jsonld_jobs(jsonld_blocks, base_url):
    jobs = []

    def walk(obj):
        if isinstance(obj, list):
            for item in obj:
                yield from walk(item)
        elif isinstance(obj, dict):
            yield obj
            for value in obj.values():
                if isinstance(value, (dict, list)):
                    yield from walk(value)

    for raw in jsonld_blocks:
        raw = raw.strip()
        if not raw:
            continue
        try:
            data = json.loads(raw)
        except Exception:
            fixed = re.sub(r"[\x00-\x1f]+", " ", raw)
            try:
                data = json.loads(fixed)
            except Exception:
                continue

        for obj in walk(data):
            obj_type = obj.get("@type") or obj.get("type") or ""
            if isinstance(obj_type, list):
                obj_type = " ".join(str(x) for x in obj_type)
            if "JobPosting" not in str(obj_type):
                continue

            title = title_case_if_needed(obj.get("title") or "")
            if not title:
                continue

            org = obj.get("hiringOrganization") or {}
            if isinstance(org, dict):
                company = clean_text(org.get("name") or "Unknown")
            else:
                company = clean_text(str(org)) or "Unknown"

            location = "Zimbabwe"
            loc_obj = obj.get("jobLocation") or obj.get("applicantLocationRequirements") or {}
            if isinstance(loc_obj, list) and loc_obj:
                loc_obj = loc_obj[0]
            if isinstance(loc_obj, dict):
                address = loc_obj.get("address") or loc_obj
                if isinstance(address, dict):
                    location = clean_text(" ".join(
                        str(address.get(k, ""))
                        for k in ("addressLocality", "addressRegion", "addressCountry")
                        if address.get(k)
                    )) or "Zimbabwe"
                else:
                    location = clean_text(str(address)) or "Zimbabwe"
            elif loc_obj:
                location = clean_text(str(loc_obj)) or "Zimbabwe"

            summary = trim_summary(obj.get("description") or obj.get("summary") or "")
            url = normalize_url(urljoin(base_url, obj.get("url") or obj.get("sameAs") or ""))

            if not url:
                ident = obj.get("identifier")
                if isinstance(ident, dict):
                    url = normalize_url(urljoin(base_url, ident.get("value") or ""))

            if not url:
                continue

            created_at = None
            date_posted = obj.get("datePosted") or ""
            if date_posted:
                created_at = parse_date(str(date_posted))

            jobs.append({
                "title": title,
                "company": company or "Unknown",
                "location": location or "Zimbabwe",
                "summary": summary or title,
                "apply_url": url,
                "created_at": created_at or now_utc(),
            })

    return jobs


def should_keep_link(source_name, url, text):
    if not is_valid_job_url(url):
        return False

    text = clean_text(text)
    if not text:
        return False

    lowered_url = url.lower()
    lowered_text = text.lower()

    hints = SOURCE_HINTS.get(source_name, {})
    includes = hints.get("include", [])
    excludes = hints.get("exclude", [])

    if any(ex in lowered_url for ex in excludes) or any(ex in lowered_text for ex in excludes):
        return False

    if len(text) < 4 or len(text) > 180:
        return False

    include_url = any(inc in lowered_url for inc in includes)
    include_text = bool(JOB_WORDS.search(text))

    if source_name == "classifieds":
        if "/listings/" in lowered_url and include_text:
            return True
        if "job" in lowered_url and include_text:
            return True
        return False

    if source_name == "kubatana":
        if "/jobs" in lowered_url and include_text:
            return True
        if include_url and include_text:
            return True
        return False

    if source_name in ("vacancymail", "careers"):
        if include_url and not NON_JOB_WORDS.search(text):
            return True
        if include_text and ("job" in lowered_url or "vacanc" in lowered_url):
            return True
        return False

    return include_url or include_text


def build_jobs_from_links(source_name, base_url, links, page_text):
    jobs = []
    seen = set()

    for link in links:
        url = normalize_url(link.get("url", ""))
        text = title_case_if_needed(link.get("text", ""))

        if not should_keep_link(source_name, url, text):
            continue

        if url in seen:
            continue
        seen.add(url)

        context = get_context_near_url(page_text, url, text)

        title = infer_title(source_name, text, url, context)
        if not title or len(title) < 4:
            continue
        if NON_JOB_WORDS.fullmatch(title):
            continue

        summary = infer_summary(source_name, title, context)
        location = guess_location(f"{title} {context}")
        company = guess_company(title, context)
        created_at = parse_date(context) or now_utc()

        jobs.append({
            "title": title,
            "company": company or "Unknown",
            "location": location or "Zimbabwe",
            "summary": summary or title,
            "apply_url": url,
            "created_at": created_at,
        })

    return jobs


def get_context_near_url(page_text, url, link_text):
    if not page_text:
        return link_text

    candidates = []

    m = re.search(re.escape(url), page_text, re.I)
    if m:
        start = max(m.start() - 900, 0)
        end = min(m.end() + 1400, len(page_text))
        candidates.append(page_text[start:end])

    path = urlparse(url).path
    if path:
        m = re.search(re.escape(path), page_text, re.I)
        if m:
            start = max(m.start() - 900, 0)
            end = min(m.end() + 1400, len(page_text))
            candidates.append(page_text[start:end])

    if link_text:
        m = re.search(re.escape(link_text[:80]), page_text, re.I)
        if m:
            start = max(m.start() - 700, 0)
            end = min(m.end() + 1200, len(page_text))
            candidates.append(page_text[start:end])

    if not candidates:
        return link_text

    context = " ".join(clean_text(x) for x in candidates)
    return clean_text(context)


def infer_title(source_name, text, url, context):
    text = clean_text(text)

    if source_name == "classifieds":
        text = re.sub(r"\b(Job Offer|On Offer|Wanted|Image|Swipe right|Swipe left)\b", "", text, flags=re.I)
        text = clean_text(text)

    if source_name == "vacancymail":
        text = re.sub(r"\b(View|Full Time|Part Time|Contract|Expires?:.*)$", "", text, flags=re.I)
        text = clean_text(text)

    title = title_case_if_needed(text)

    if title.lower() in ("view", "apply", "apply now", "read more", "details"):
        m = re.search(
            r"([A-Z][A-Za-z0-9&'’.,()/ -]{5,100})\s+(?:Harare|Bulawayo|Mutare|Gweru|Zimbabwe|Remote|Full Time|Part Time|Expires)",
            context,
        )
        if m:
            title = title_case_if_needed(m.group(1))

    title = re.sub(r"\s+\|\s+.*$", "", title)
    title = re.sub(r"\s+[-–—]\s+Apply.*$", "", title, flags=re.I)
    title = re.sub(r"\b\s*Apply\s*$", "", title, flags=re.I)
    title = clean_text(title)

    if len(title) < 4:
        slug = os.path.basename(urlparse(url).path).replace("-", " ")
        title = title_case_if_needed(slug)

    return title_case_if_needed(title)


def infer_summary(source_name, title, context):
    context = clean_text(context)
    if not context:
        return title

    context = re.sub(r"\s+", " ", context)
    context = re.sub(re.escape(title), "", context, count=1, flags=re.I)

    parts = re.split(r"(?<=[.!?])\s+", context)
    useful = []

    for part in parts:
        part = clean_text(part)
        if len(part) < 25:
            continue
        if NON_JOB_WORDS.search(part):
            continue
        useful.append(part)
        if len(" ".join(useful)) > 260:
            break

    if useful:
        return trim_summary(" ".join(useful))

    words = context.split()
    if len(words) > 18:
        return trim_summary(" ".join(words[:70]))

    return title


def parse_source(source_name, base_url, file_path):
    result = {
        "source": source_name,
        "found": 0,
        "jobs": [],
        "error": None,
    }

    try:
        with open(file_path, "rb") as f:
            raw = f.read()
    except Exception as exc:
        result["error"] = f"could not read file: {exc}"
        return result

    if not raw:
        result["error"] = "empty source file"
        return result

    html = raw.decode("utf-8", errors="replace")
    parser = LinkCollector(base_url)

    try:
        parser.feed(html)
    except Exception:
        pass

    jsonld_jobs = extract_jsonld_jobs(parser.jsonld, base_url)
    link_jobs = build_jobs_from_links(source_name, base_url, parser.links, html)

    combined = []
    seen_urls = set()

    for job in jsonld_jobs + link_jobs:
        url = normalize_url(job.get("apply_url", ""))
        title = title_case_if_needed(job.get("title", ""))
        if not url or not title:
            continue
        if not is_valid_job_url(url):
            continue
        if url in seen_urls:
            continue

        summary = trim_summary(job.get("summary") or title)
        company = clean_text(job.get("company")) or "Unknown"
        location = clean_text(job.get("location")) or "Zimbabwe"
        created_at = job.get("created_at") or now_utc()

        category = map_category(title, summary)

        combined.append({
            "source": source_name,
            "title": title,
            "company": company,
            "location": location,
            "category": category,
            "summary": summary,
            "apply_url": url,
            "featured": 0,
            "created_at": created_at,
        })
        seen_urls.add(url)

    limit = SOURCE_LIMITS.get(source_name, 50)
    result["jobs"] = combined[:limit]
    result["found"] = len(result["jobs"])
    return result


def ensure_index(conn):
    try:
        conn.execute("CREATE INDEX IF NOT EXISTS idx_jobs_apply_url ON jobs(apply_url)")
        conn.commit()
    except Exception:
        pass


def insert_jobs(conn, source_results):
    totals = {
        "total_found": 0,
        "inserted": 0,
        "skipped_duplicates": 0,
        "failed": 0,
        "per_source": {},
    }

    for src in source_results:
        source = src["source"]
        totals["per_source"].setdefault(source, {
            "found": 0,
            "inserted": 0,
            "skipped_duplicates": 0,
            "failed": 0,
            "error": src.get("error"),
        })

        if src.get("error"):
            totals["per_source"][source]["failed"] += 1
            totals["failed"] += 1
            continue

        jobs = src.get("jobs") or []
        totals["per_source"][source]["found"] = len(jobs)
        totals["total_found"] += len(jobs)

        for job in jobs:
            try:
                title = title_case_if_needed(job.get("title"))
                apply_url = normalize_url(job.get("apply_url"))

                if not title or not apply_url or not is_valid_job_url(apply_url):
                    totals["failed"] += 1
                    totals["per_source"][source]["failed"] += 1
                    continue

                exists = conn.execute(
                    "SELECT 1 FROM jobs WHERE apply_url = ? LIMIT 1",
                    (apply_url,),
                ).fetchone()

                if exists:
                    totals["skipped_duplicates"] += 1
                    totals["per_source"][source]["skipped_duplicates"] += 1
                    continue

                company = clean_text(job.get("company")) or "Unknown"
                location = clean_text(job.get("location")) or "Zimbabwe"
                summary = trim_summary(job.get("summary") or title)
                category = job.get("category") if job.get("category") in ALLOWED_CATEGORIES else map_category(title, summary)
                featured = int(job.get("featured") or 0)
                created_at = clean_text(job.get("created_at")) or now_utc()

                conn.execute(
                    """
                    INSERT INTO jobs
                    (title, company, location, category, summary, apply_url, featured, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        title,
                        company,
                        location,
                        category,
                        summary,
                        apply_url,
                        featured,
                        created_at,
                    ),
                )

                totals["inserted"] += 1
                totals["per_source"][source]["inserted"] += 1

            except Exception:
                totals["failed"] += 1
                totals["per_source"][source]["failed"] += 1

        try:
            conn.commit()
        except Exception:
            conn.rollback()
            totals["failed"] += len(jobs)
            totals["per_source"][source]["failed"] += len(jobs)

    return totals


def main():
    source_results = []

    for arg in SOURCE_ARGS:
        try:
            source_name, base_url, file_path = arg.split("|", 2)
        except ValueError:
            continue

        print(f"[{now_utc()} UTC] Parsing source: {source_name}", flush=True)
        result = parse_source(source_name, base_url, file_path)
        source_results.append(result)

        if result.get("error"):
            print(f"[{now_utc()} UTC] WARN: {source_name} parse failed: {result['error']}", flush=True)
        else:
            print(f"[{now_utc()} UTC] {source_name}: found {result['found']} candidate job(s)", flush=True)

    conn = sqlite3.connect(DB_PATH)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
    except Exception:
        pass

    ensure_index(conn)

    try:
        totals = insert_jobs(conn, source_results)
    finally:
        conn.close()

    for source, stats in totals["per_source"].items():
        error_msg = f" error={stats['error']}" if stats.get("error") else ""
        print(
            f"[{now_utc()} UTC] Source summary: {source} "
            f"found={stats['found']} inserted={stats['inserted']} "
            f"skipped_duplicates={stats['skipped_duplicates']} failed={stats['failed']}{error_msg}",
            flush=True,
        )

    print(
        f"[{now_utc()} UTC] Done. "
        f"total_found={totals['total_found']} "
        f"inserted={totals['inserted']} "
        f"skipped_duplicates={totals['skipped_duplicates']} "
        f"failed={totals['failed']}",
        flush=True,
    )


if __name__ == "__main__":
    main()
PY

PY_RC=$?

if [ "$PY_RC" -ne 0 ]; then
  log "WARN: Parser/insert step exited with code ${PY_RC}. Treating as non-fatal runtime failure."
fi

log "Scrape run finished"
exit 0
