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
# Requirements: bash, curl, python3, sqlite3
#
# Captured fields per job:
#   title, company (employer), location, category, job_type, posted_date,
#   expiry_date (application deadline), apply_url (application link),
#   summary (description), source_url (authentic source page).
#
# Notes:
#   - Public pages only. Polite headers, timeouts, small delays.
#   - Inline Python standard library only.
#   - Deduplicates by apply_url before inserting.
#   - Missing columns are auto-added to the jobs table (non-destructive).
#   - Exits 0 after run even if a source fails. Non-zero only for setup errors.
#   - PSC eRecruitment / UNICEF / UN.org are JS-rendered SPAs; curl may return
#     little usable HTML. Those sources will log a warning and be skipped if empty.

set -u
IFS=$'\n\t'

DB_PATH="${1:-}"

USER_AGENT="Mozilla/5.0 (compatible; ZimJobsBot/1.0; +https://zimjobs.online)"
CONNECT_TIMEOUT=12
MAX_TIME=40
SLEEP_BETWEEN_REQUESTS=2

log()  { printf '[%s] %s\n' "$(date -u '+%Y-%m-%d %H:%M:%S UTC')" "$*"; }
die()  { log "ERROR: $*"; exit 1; }
require_cmd() { command -v "$1" >/dev/null 2>&1 || die "Required command not found: $1"; }

if [ -z "$DB_PATH" ]; then
  die "Missing database path. Usage: ./scrape_jobs.sh /path/to/jobs.db"
fi

require_cmd bash
require_cmd curl
require_cmd python3
require_cmd sqlite3
require_cmd mktemp

[ -f "$DB_PATH" ] || die "Database file does not exist: $DB_PATH"
{ [ -r "$DB_PATH" ] && [ -w "$DB_PATH" ]; } || die "Database file must be readable and writable: $DB_PATH"

sqlite3 "$DB_PATH" "SELECT name FROM sqlite_master WHERE type='table' AND name='jobs';" | grep -qx "jobs" \
  || die "Database does not contain required table: jobs"

# ---- Core columns that MUST already exist ----
CORE_MISSING="$(
sqlite3 "$DB_PATH" <<'SQL'
.mode list
WITH required(name) AS (
  VALUES ('id'),('title'),('company'),('location'),('category'),
         ('summary'),('apply_url'),('featured'),('created_at')
),
actual(name) AS ( SELECT name FROM pragma_table_info('jobs') )
SELECT group_concat(required.name, ', ')
FROM required LEFT JOIN actual USING(name)
WHERE actual.name IS NULL;
SQL
)"
[ -z "$CORE_MISSING" ] || die "jobs table is missing required core column(s): $CORE_MISSING"

# ---- Auto-migrate: add new optional columns if absent (non-destructive) ----
column_exists() {
  sqlite3 "$DB_PATH" "SELECT 1 FROM pragma_table_info('jobs') WHERE name='$1' LIMIT 1;" | grep -qx 1
}
add_column() {
  local name="$1" decl="$2"
  if column_exists "$name"; then
    log "Column present: $name"
  else
    log "Adding column: $name ($decl)"
    sqlite3 "$DB_PATH" "ALTER TABLE jobs ADD COLUMN $name $decl;" \
      || die "Failed to add column $name"
  fi
}
add_column "job_type"    "TEXT"
add_column "posted_date" "TEXT"
add_column "expiry_date" "TEXT"
add_column "source_url"  "TEXT"

TMP_DIR="$(mktemp -d)"
trap 'rm -rf "$TMP_DIR"' EXIT

log "Starting Zimbabwe jobs scrape"
log "Database: $DB_PATH"

fetch_url() {
  local source_name="$1" url="$2" out_file="$3"
  log "Fetching ${source_name}: ${url}"
  local status
  status="$(
    curl --silent --show-error --location --compressed --fail \
      --connect-timeout "$CONNECT_TIMEOUT" --max-time "$MAX_TIME" \
      --retry 1 --retry-delay 2 \
      --user-agent "$USER_AGENT" \
      --header "Accept: text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8" \
      --header "Accept-Language: en-US,en;q=0.9" \
      --output "$out_file" --write-out "%{http_code}" \
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
  log "Fetched ${source_name} OK. HTTP ${status}. Size $(wc -c < "$out_file" | tr -d ' ') bytes"
  return 0
}

declare -a SOURCE_ARGS=()
add_source_file() {
  local source_name="$1" source_url="$2" file_path="$3"
  [ -s "$file_path" ] && SOURCE_ARGS+=("${source_name}|${source_url}|${file_path}")
}

# ---- Source list (name | url | tmp file) ----
declare -a SOURCES=(
  "vacancymail|https://vacancymail.co.zw/jobs/|$TMP_DIR/vacancymail.html"

  "unicef|https://jobs.unicef.org/cw/en-us/filter/?location=zimbabwe&search-keyword=|$TMP_DIR/unicef.html"


)

for entry in "${SOURCES[@]}"; do
  IFS='|' read -r s_name s_url s_file <<< "$entry"
  if fetch_url "$s_name" "$s_url" "$s_file"; then
    add_source_file "$s_name" "$s_url" "$s_file"
  fi
  sleep "$SLEEP_BETWEEN_REQUESTS"
done

if [ "${#SOURCE_ARGS[@]}" -eq 0 ]; then
  log "WARN: No sources downloaded successfully. Nothing to parse."
  log "Done. total_found=0 inserted=0 skipped_duplicates=0 failed=0"
  exit 0
fi

python3 - "$DB_PATH" "${SOURCE_ARGS[@]}" <<'PY'
import sys, os, re, json, sqlite3
from html import unescape
from html.parser import HTMLParser
from urllib.parse import urljoin, urlparse
from datetime import datetime, timezone

DB_PATH = sys.argv[1]
SOURCE_ARGS = sys.argv[2:]

ALLOWED_CATEGORIES = [
    "Technology","Finance","Healthcare","Education","Engineering",
    "Sales & Marketing","Administration","NGO & Development","Government","Other",
]

SOURCE_LIMITS = {
    "vacancymail": 60, "psc": 60, "unicef": 60,
    "un_zimbabwe": 60, "ihararejobs": 60, "carezimbabwe": 40,
}

# Some sources default to a known category / location.
SOURCE_DEFAULT_CATEGORY = {
    "psc": "Government",
    "unicef": "NGO & Development",
    "un_zimbabwe": "NGO & Development",
    "carezimbabwe": "NGO & Development",
}

SOURCE_HINTS = {
    "vacancymail": {
        "include": ["/jobs/","/job/","/vacancy","/categories/"],
        "exclude": ["#","mailto:","tel:","javascript:","/login","/register",
                    "/candidate","/employer","/resume","/cv","/premium",
                    "/testimonials","/terms","/privacy","/contact"],
    },
    "psc": {
        "include": ["/job","/vacanc","/post","/recruit","/advert"],
        "exclude": ["#","mailto:","tel:","javascript:","/login","/register",
                    "/help","/terms","/privacy","/contact","/home"],
    },
    "unicef": {
        "include": ["/job/","/jobs/","/cw/en-us/job","/vacanc"],
        "exclude": ["#","mailto:","tel:","javascript:","/login","/register",
                    "/privacy","/terms","/filter","/search","/faq"],
    },
    "un_zimbabwe": {
        "include": ["/job","/vacanc","/career","/opportunit"],
        "exclude": ["#","mailto:","tel:","javascript:","/login","/register",
                    "/privacy","/terms","/contact","/about","/news","/stories"],
    },
    "ihararejobs": {
        "include": ["/job","/jobs/","/vacanc","/listing"],
        "exclude": ["#","mailto:","tel:","javascript:","/login","/register",
                    "/employer","/candidate","/cv","/terms","/privacy","/contact",
                    "/category","/tag"],
    },
    "carezimbabwe": {
        "include": ["/careers","/career","/job","/vacanc","/opportunit"],
        "exclude": ["#","mailto:","tel:","javascript:","/login","/register",
                    "/terms","/privacy","/contact","/about","/news","/donate"],
    },
}

JOB_WORDS = re.compile(
    r"\b(job|jobs|vacancy|vacancies|career|careers|officer|manager|assistant|"
    r"developer|engineer|technician|accountant|finance|nurse|doctor|teacher|"
    r"lecturer|driver|clerk|attachee|intern|graduate|consultant|specialist|"
    r"administrator|receptionist|sales|marketing|auditor|data|ict|it|hr|"
    r"procurement|logistics|mechanic|fitter|electrician|operator|coordinator|"
    r"programme|program|advisor|analyst|director|associate|fellow)\b", re.I)

NON_JOB_WORDS = re.compile(
    r"\b(login|register|sign in|sign up|privacy|terms|cookie|contact|about|"
    r"advertise|post a job|submit cv|upload cv|browse categories|read more|"
    r"home|faq|help|newsletter|whatsapp|facebook|twitter|linkedin|instagram|"
    r"previous|next|search|filter|premium|testimonial|candidate|employer|donate)\b", re.I)

ZIM_LOCATIONS = [
    "Harare","Bulawayo","Mutare","Gweru","Masvingo","Kwekwe","Kadoma",
    "Chitungwiza","Chinhoyi","Marondera","Bindura","Victoria Falls","Zvishavane",
    "Chegutu","Norton","Rusape","Hwange","Kariba","Beitbridge","Gwanda","Lupane",
    "Binga","Chipinge","Chiredzi","Mvurwi","Ruwa","Zimbabwe","Remote",
    "Mashonaland East","Mashonaland West","Mashonaland Central","Matabeleland North",
    "Matabeleland South","Manicaland","Midlands","Masvingo Province",
]

DATE_PATTERNS = [
    re.compile(r"\b(20\d{2})[-/](0?[1-9]|1[0-2])[-/](0?[1-9]|[12]\d|3[01])\b"),
    re.compile(r"\b(0?[1-9]|[12]\d|3[01])[-/](0?[1-9]|1[0-2])[-/](20\d{2})\b"),
]
MONTHS = {"jan":1,"january":1,"feb":2,"february":2,"mar":3,"march":3,"apr":4,
    "april":4,"may":5,"jun":6,"june":6,"jul":7,"july":7,"aug":8,"august":8,
    "sep":9,"sept":9,"september":9,"oct":10,"october":10,"nov":11,"november":11,
    "dec":12,"december":12}
MONTH_DATE_PATTERNS = [
    re.compile(r"\b([A-Za-z]{3,9})\s+([0-3]?\d),?\s+(20\d{2})\b", re.I),
    re.compile(r"\b([0-3]?\d)\s+([A-Za-z]{3,9})\s+(20\d{2})\b", re.I),
]

JOB_TYPE_PATTERNS = [
    ("Full Time", re.compile(r"\bfull[\s-]?time\b", re.I)),
    ("Part Time", re.compile(r"\bpart[\s-]?time\b", re.I)),
    ("Contract",  re.compile(r"\bcontract\b", re.I)),
    ("Temporary", re.compile(r"\btemporary\b", re.I)),
    ("Internship",re.compile(r"\b(intern(ship)?|attach(ment|ee)|graduate trainee)\b", re.I)),
    ("Consultancy",re.compile(r"\b(consultanc|consultant)\b", re.I)),
    ("Volunteer", re.compile(r"\bvolunteer\b", re.I)),
]

EXPIRY_CUE   = re.compile(r"(expire[sd]?|deadline|closing date|apply before|valid through|application deadline)[:\s-]*", re.I)
POSTED_CUE   = re.compile(r"(posted|published|date posted|advertised)[:\s-]*", re.I)


def now_utc():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

def clean_text(value):
    if not value: return ""
    value = unescape(str(value))
    value = re.sub(r"<[^>]+>", " ", value)
    value = re.sub(r"\s+", " ", value)
    return value.strip(" \t\r\n-–—|•")

def normalize_url(url):
    url = (url or "").strip()
    if not url: return ""
    url = url.split("#", 1)[0].strip()
    p = urlparse(url)
    if p.scheme not in ("http","https") or not p.netloc: return ""
    return url

def is_valid_job_url(url):
    p = urlparse(url)
    if p.scheme not in ("http","https") or not p.netloc: return False
    low = url.lower()
    bad = (".jpg",".jpeg",".png",".gif",".svg",".webp",".pdf",".zip",".rar",
           ".css",".js",".ico",".xml")
    if low.endswith(bad): return False
    if any(x in low for x in ["mailto:","tel:","javascript:"]): return False
    return True

def trim_summary(text, max_len=420):
    text = clean_text(text)
    if not text: return ""
    text = re.sub(r"\b(apply now|view job|read more|more details)\b","",text,flags=re.I)
    text = clean_text(text)
    if len(text) <= max_len: return text
    cut = text[:max_len].rsplit(" ",1)[0]
    return clean_text(cut) + "..."

def title_case_if_needed(title):
    title = clean_text(title)
    if not title: return ""
    if len(title) > 120: title = title[:120].rsplit(" ",1)[0]
    if title.isupper() and len(title) > 8: return title.title()
    return title

def map_category(title, summary="", default="Other"):
    text = f"{title} {summary}".lower()
    rules = [
        ("Technology",["software","developer","programmer","frontend","backend",
            "fullstack"," it ","ict","information technology","data","database",
            "systems","network","cyber","web","digital"," ai ","machine learning",
            "devops","cloud","support engineer"]),
        ("Finance",["accountant","accounting","finance","financial","audit","auditor",
            "bank","banking","bookkeeper","payroll","tax","treasury","credit"]),
        ("Healthcare",["nurse","doctor","clinic","clinical","health","hospital",
            "pharmacy","pharmacist","medical","midwife","laboratory","dentist",
            "radiographer","physiotherapist"]),
        ("Education",["teacher","lecturer","school","training","trainer","tutor",
            "education","academic","instructor","principal","curriculum","teaching"]),
        ("Engineering",["engineer","engineering","technician","construction","civil",
            "mechanical","electrical","fitter","artisan","builder","surveyor",
            "architect","electrician","maintenance","solar"]),
        ("Sales & Marketing",["sales","marketing","customer","business development",
            "brand","market","call centre","call center","commercial","key accounts"]),
        ("NGO & Development",["ngo","humanitarian","development","programme officer",
            "program officer","unicef","united nations","relief","wash","nutrition",
            "donor","grant","monitoring and evaluation","m&e"]),
        ("Government",["public service","ministry","government","council","municipal",
            "commission","parastatal"]),
        ("Administration",["admin","administrator","administration","receptionist",
            "office","hr","human resources","assistant","secretary","clerk",
            "data entry","operations","procurement","logistics","stores","driver"]),
    ]
    padded = f" {text} "
    for category, keywords in rules:
        for kw in keywords:
            if kw in padded or kw.strip() in text:
                return category
    return default

def guess_location(text):
    text = clean_text(text); low = text.lower()
    for loc in ZIM_LOCATIONS:
        if loc.lower() in low:
            if loc.lower() == "remote": return "Remote"
            if loc.lower() == "zimbabwe": continue
            return loc
    if "remote" in low: return "Remote"
    return "Zimbabwe"

def guess_company(title, context, default="Unknown"):
    context = clean_text(context)
    patterns = [
        r"\bat\s+([A-Z][A-Za-z0-9&.,'’()\/ -]{2,80})",
        r"\bcompany\s*[:\-]\s*([A-Za-z0-9&.,'’()\/ -]{2,80})",
        r"\bemployer\s*[:\-]\s*([A-Za-z0-9&.,'’()\/ -]{2,80})",
        r"\borganis?z?ation\s*[:\-]\s*([A-Za-z0-9&.,'’()\/ -]{2,80})",
    ]
    for pat in patterns:
        m = re.search(pat, context, re.I)
        if m:
            company = clean_text(m.group(1))
            company = re.split(r"\s{2,}| Location | Full Time | Expires | Deadline | Apply ",
                               company, flags=re.I)[0]
            company = clean_text(company)
            if 2 <= len(company) <= 80 and not NON_JOB_WORDS.search(company):
                return company
    pipe_parts = [clean_text(p) for p in re.split(r"\s+[|–—-]\s+", title) if clean_text(p)]
    if len(pipe_parts) >= 2:
        possible = pipe_parts[-1]
        if 2 <= len(possible) <= 80 and not JOB_WORDS.search(possible):
            return possible
    return default

def parse_date(text):
    text = clean_text(text)
    if not text: return None
    for pat in DATE_PATTERNS:
        m = pat.search(text)
        if m:
            parts = m.groups()
            try:
                if len(parts[0]) == 4:
                    y,mo,d = int(parts[0]),int(parts[1]),int(parts[2])
                else:
                    d,mo,y = int(parts[0]),int(parts[1]),int(parts[2])
                return datetime(y,mo,d,tzinfo=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
            except ValueError: pass
    for pat in MONTH_DATE_PATTERNS:
        m = pat.search(text)
        if m:
            try:
                if m.group(1).lower() in MONTHS:
                    mo = MONTHS[m.group(1).lower()]; d = int(m.group(2)); y = int(m.group(3))
                else:
                    d = int(m.group(1)); mo = MONTHS[m.group(2).lower()]; y = int(m.group(3))
                return datetime(y,mo,d,tzinfo=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
            except Exception: pass
    return None

def parse_cue_date(cue_regex, context):
    """Find a date that follows an 'Expires:'/'Posted:' style cue."""
    context = clean_text(context)
    m = cue_regex.search(context)
    if not m: return None
    tail = context[m.end(): m.end()+40]
    return parse_date(tail)

def guess_job_type(text):
    for label, pat in JOB_TYPE_PATTERNS:
        if pat.search(text or ""):
            return label
    return ""


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
        attrs = dict(attrs or {}); tag = tag.lower()
        if tag == "script" and "ld+json" in attrs.get("type","").lower():
            self._in_jsonld = True; self._jsonld_buf = []
        if tag == "a":
            href = attrs.get("href") or ""
            url = urljoin(self.base_url, href)
            self._current = {"url": normalize_url(url),"text":"",
                "title": attrs.get("title",""),"aria": attrs.get("aria-label",""),
                "class": attrs.get("class","")}
    def handle_endtag(self, tag):
        tag = tag.lower()
        if tag == "script" and self._in_jsonld:
            raw = "".join(self._jsonld_buf).strip()
            if raw: self._jsonld.append(raw)
            self._in_jsonld = False; self._jsonld_buf = []
        if tag == "a" and self._current:
            text = clean_text(" ".join([self._current.get("text",""),
                self._current.get("title",""),self._current.get("aria","")]))
            url = self._current.get("url","")
            if text and url:
                self.links.append({"url":url,"text":text,"class":self._current.get("class","")})
            self._current = None
    def handle_data(self, data):
        if self._in_jsonld: self._jsonld_buf.append(data)
        if self._current is not None: self._current["text"] += " " + data
    @property
    def jsonld(self): return self._jsonld


def extract_jsonld_jobs(jsonld_blocks, base_url, source_name):
    jobs = []
    def walk(obj):
        if isinstance(obj, list):
            for item in obj: yield from walk(item)
        elif isinstance(obj, dict):
            yield obj
            for v in obj.values():
                if isinstance(v,(dict,list)): yield from walk(v)
    for raw in jsonld_blocks:
        raw = raw.strip()
        if not raw: continue
        try:
            data = json.loads(raw)
        except Exception:
            try: data = json.loads(re.sub(r"[\x00-\x1f]+"," ",raw))
            except Exception: continue
        for obj in walk(data):
            ot = obj.get("@type") or obj.get("type") or ""
            if isinstance(ot, list): ot = " ".join(str(x) for x in ot)
            if "JobPosting" not in str(ot): continue
            title = title_case_if_needed(obj.get("title") or "")
            if not title: continue
            org = obj.get("hiringOrganization") or {}
            company = clean_text(org.get("name") if isinstance(org,dict) else str(org)) or "Unknown"
            location = "Zimbabwe"
            loc_obj = obj.get("jobLocation") or obj.get("applicantLocationRequirements") or {}
            if isinstance(loc_obj, list) and loc_obj: loc_obj = loc_obj[0]
            if isinstance(loc_obj, dict):
                address = loc_obj.get("address") or loc_obj
                if isinstance(address, dict):
                    location = clean_text(" ".join(str(address.get(k,"")) for k in
                        ("addressLocality","addressRegion","addressCountry") if address.get(k))) or "Zimbabwe"
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
            if not url: continue
            emp = obj.get("employmentType") or ""
            if isinstance(emp, list): emp = ", ".join(str(x) for x in emp)
            job_type = clean_text(emp).replace("_"," ").title() or guess_job_type(summary)
            posted = parse_date(str(obj.get("datePosted") or "")) 
            expiry = parse_date(str(obj.get("validThrough") or ""))
            jobs.append({
                "title": title, "company": company or "Unknown",
                "location": location or "Zimbabwe", "summary": summary or title,
                "apply_url": url, "job_type": job_type,
                "posted_date": posted or "", "expiry_date": expiry or "",
                "created_at": posted or now_utc(),
            })
    return jobs


def should_keep_link(source_name, url, text):
    if not is_valid_job_url(url): return False
    text = clean_text(text)
    if not text: return False
    low_url = url.lower(); 
    hints = SOURCE_HINTS.get(source_name, {})
    includes = hints.get("include", []); excludes = hints.get("exclude", [])
    if any(ex in low_url for ex in excludes): return False
    if any(ex in text.lower() for ex in excludes): return False
    if len(text) < 4 or len(text) > 180: return False
    include_url = any(inc in low_url for inc in includes)
    include_text = bool(JOB_WORDS.search(text))
    if source_name in ("psc","carezimbabwe","un_zimbabwe"):
        if include_url and not NON_JOB_WORDS.fullmatch(text): return True
        if include_text and any(k in low_url for k in ("job","vacanc","career","career","recruit")): return True
        return include_url and include_text
    if source_name == "unicef":
        if include_url and include_text: return True
        if "/job" in low_url and include_text: return True
        return False
    if source_name in ("vacancymail","ihararejobs"):
        if include_url and not NON_JOB_WORDS.search(text): return True
        if include_text and ("job" in low_url or "vacanc" in low_url): return True
        return False
    return include_url or include_text


def get_context_near_url(page_text, url, link_text):
    if not page_text: return link_text
    candidates = []
    for needle in (url, urlparse(url).path, (link_text or "")[:80]):
        if not needle: continue
        m = re.search(re.escape(needle), page_text, re.I)
        if m:
            start = max(m.start()-900, 0); end = min(m.end()+1400, len(page_text))
            candidates.append(page_text[start:end])
    if not candidates: return link_text
    return clean_text(" ".join(clean_text(x) for x in candidates))


def infer_title(source_name, text, url, context):
    text = clean_text(text)
    if source_name == "vacancymail":
        text = clean_text(re.sub(r"\b(View|Full Time|Part Time|Contract|Expires?:.*)$","",text,flags=re.I))
    title = title_case_if_needed(text)
    if title.lower() in ("view","apply","apply now","read more","details"):
        m = re.search(r"([A-Z][A-Za-z0-9&'’.,()/ -]{5,100})\s+(?:Harare|Bulawayo|Mutare|Gweru|Zimbabwe|Remote|Full Time|Part Time|Expires)", context)
        if m: title = title_case_if_needed(m.group(1))
    title = re.sub(r"\s+\|\s+.*$","",title)
    title = re.sub(r"\s+[-–—]\s+Apply.*$","",title,flags=re.I)
    title = clean_text(re.sub(r"\b\s*Apply\s*$","",title,flags=re.I))
    if len(title) < 4:
        slug = os.path.basename(urlparse(url).path).replace("-"," ")
        title = title_case_if_needed(slug)
    return title_case_if_needed(title)


def infer_summary(source_name, title, context):
    context = clean_text(context)
    if not context: return title
    context = re.sub(re.escape(title),"",context,count=1,flags=re.I)
    parts = re.split(r"(?<=[.!?])\s+", context)
    useful = []
    for part in parts:
        part = clean_text(part)
        if len(part) < 25: continue
        if NON_JOB_WORDS.search(part): continue
        useful.append(part)
        if len(" ".join(useful)) > 260: break
    if useful: return trim_summary(" ".join(useful))
    words = context.split()
    if len(words) > 18: return trim_summary(" ".join(words[:70]))
    return title


def build_jobs_from_links(source_name, base_url, links, page_text):
    jobs = []; seen = set()
    default_cat = SOURCE_DEFAULT_CATEGORY.get(source_name, "Other")
    for link in links:
        url = normalize_url(link.get("url",""))
        text = title_case_if_needed(link.get("text",""))
        if not should_keep_link(source_name, url, text): continue
        if url in seen: continue
        seen.add(url)
        context = get_context_near_url(page_text, url, text)
        title = infer_title(source_name, text, url, context)
        if not title or len(title) < 4: continue
        if NON_JOB_WORDS.fullmatch(title): continue
        summary  = infer_summary(source_name, title, context)
        location = guess_location(f"{title} {context}")
        company  = guess_company(title, context)
        job_type = guess_job_type(f"{title} {context}")
        posted   = parse_cue_date(POSTED_CUE, context) or parse_date(context)
        expiry   = parse_cue_date(EXPIRY_CUE, context)
        jobs.append({
            "title": title, "company": company or "Unknown",
            "location": location or "Zimbabwe", "summary": summary or title,
            "apply_url": url, "job_type": job_type,
            "posted_date": posted or "", "expiry_date": expiry or "",
            "created_at": posted or now_utc(),
            "_default_category": default_cat,
        })
    return jobs


def parse_source(source_name, base_url, file_path):
    result = {"source": source_name, "found": 0, "jobs": [], "error": None}
    try:
        with open(file_path,"rb") as f: raw = f.read()
    except Exception as exc:
        result["error"] = f"could not read file: {exc}"; return result
    if not raw:
        result["error"] = "empty source file"; return result
    html = raw.decode("utf-8", errors="replace")
    parser = LinkCollector(base_url)
    try: parser.feed(html)
    except Exception: pass

    jsonld_jobs = extract_jsonld_jobs(parser.jsonld, base_url, source_name)
    link_jobs   = build_jobs_from_links(source_name, base_url, parser.links, html)

    default_cat = SOURCE_DEFAULT_CATEGORY.get(source_name, "Other")
    combined = []; seen_urls = set()
    for job in jsonld_jobs + link_jobs:
        url = normalize_url(job.get("apply_url",""))
        title = title_case_if_needed(job.get("title",""))
        if not url or not title or not is_valid_job_url(url): continue
        if url in seen_urls: continue
        summary  = trim_summary(job.get("summary") or title)
        company  = clean_text(job.get("company")) or "Unknown"
        location = clean_text(job.get("location")) or "Zimbabwe"
        category = map_category(title, summary, default=job.get("_default_category", default_cat))
        combined.append({
            "source": source_name, "title": title, "company": company,
            "location": location, "category": category, "summary": summary,
            "apply_url": url, "job_type": clean_text(job.get("job_type")),
            "posted_date": clean_text(job.get("posted_date")),
            "expiry_date": clean_text(job.get("expiry_date")),
            "source_url": base_url, "featured": 0,
            "created_at": job.get("created_at") or now_utc(),
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
    except Exception: pass


def insert_jobs(conn, source_results):
    totals = {"total_found":0,"inserted":0,"skipped_duplicates":0,"failed":0,"per_source":{}}
    for src in source_results:
        source = src["source"]
        totals["per_source"].setdefault(source, {"found":0,"inserted":0,
            "skipped_duplicates":0,"failed":0,"error":src.get("error")})
        if src.get("error"):
            totals["per_source"][source]["failed"] += 1; totals["failed"] += 1; continue
        jobs = src.get("jobs") or []
        totals["per_source"][source]["found"] = len(jobs)
        totals["total_found"] += len(jobs)
        for job in jobs:
            try:
                title = title_case_if_needed(job.get("title"))
                apply_url = normalize_url(job.get("apply_url"))
                if not title or not apply_url or not is_valid_job_url(apply_url):
                    totals["failed"] += 1; totals["per_source"][source]["failed"] += 1; continue
                if conn.execute("SELECT 1 FROM jobs WHERE apply_url=? LIMIT 1",(apply_url,)).fetchone():
                    totals["skipped_duplicates"] += 1
                    totals["per_source"][source]["skipped_duplicates"] += 1; continue
                company  = clean_text(job.get("company")) or "Unknown"
                location = clean_text(job.get("location")) or "Zimbabwe"
                summary  = trim_summary(job.get("summary") or title)
                category = job.get("category") if job.get("category") in ALLOWED_CATEGORIES else map_category(title, summary)
                job_type    = clean_text(job.get("job_type")) or None
                posted_date = clean_text(job.get("posted_date")) or None
                expiry_date = clean_text(job.get("expiry_date")) or None
                source_url  = clean_text(job.get("source_url")) or None
                featured = int(job.get("featured") or 0)
                created_at = clean_text(job.get("created_at")) or now_utc()
                conn.execute("""
                    INSERT INTO jobs
                    (title, company, location, category, summary, apply_url,
                     featured, created_at, job_type, posted_date, expiry_date, source_url)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
                """,(title,company,location,category,summary,apply_url,featured,
                     created_at,job_type,posted_date,expiry_date,source_url))
                totals["inserted"] += 1
                totals["per_source"][source]["inserted"] += 1
            except Exception:
                totals["failed"] += 1; totals["per_source"][source]["failed"] += 1
        try: conn.commit()
        except Exception:
            conn.rollback()
            totals["failed"] += len(jobs)
            totals["per_source"][source]["failed"] += len(jobs)
    return totals


def main():
    source_results = []
    for arg in SOURCE_ARGS:
        try: source_name, base_url, file_path = arg.split("|",2)
        except ValueError: continue
        print(f"[{now_utc()} UTC] Parsing source: {source_name}", flush=True)
        result = parse_source(source_name, base_url, file_path)
        source_results.append(result)
        if result.get("error"):
            print(f"[{now_utc()} UTC] WARN: {source_name} parse failed: {result['error']}", flush=True)
        else:
            print(f"[{now_utc()} UTC] {source_name}: found {result['found']} candidate job(s)", flush=True)

    conn = sqlite3.connect(DB_PATH)
    try: conn.execute("PRAGMA journal_mode=WAL")
    except Exception: pass
    ensure_index(conn)
    try: totals = insert_jobs(conn, source_results)
    finally: conn.close()

    for source, stats in totals["per_source"].items():
        err = f" error={stats['error']}" if stats.get("error") else ""
        print(f"[{now_utc()} UTC] Source summary: {source} found={stats['found']} "
              f"inserted={stats['inserted']} skipped_duplicates={stats['skipped_duplicates']} "
              f"failed={stats['failed']}{err}", flush=True)
    print(f"[{now_utc()} UTC] Done. total_found={totals['total_found']} "
          f"inserted={totals['inserted']} skipped_duplicates={totals['skipped_duplicates']} "
          f"failed={totals['failed']}", flush=True)


if __name__ == "__main__":
    main()
PY

PY_RC=$?
if [ "$PY_RC" -ne 0 ]; then
  log "WARN: Parser/insert step exited with code ${PY_RC}. Treating as non-fatal runtime failure."
fi

log "Scrape run finished"
exit 0