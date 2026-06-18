import asyncio, hmac, html as html_lib, json, logging, os, re, secrets, sqlite3, sys, threading, time
from collections import deque
from datetime import datetime, timedelta, timezone
from math import ceil
from traceback import format_exception
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from xml.sax.saxutils import escape
import requests
from flask import (Flask, g, request, render_template, abort,
                   Response, redirect, url_for, has_request_context, flash)
from flask_compress import Compress
from werkzeug.exceptions import HTTPException
from werkzeug.middleware.proxy_fix import ProxyFix

from job_schema import build_job_posting_json_ld

SCRAPER_SRC = os.path.join(os.path.dirname(__file__), "zimjobs_scraper", "src")
if os.path.isdir(SCRAPER_SRC) and SCRAPER_SRC not in sys.path:
    sys.path.insert(0, SCRAPER_SRC)
try:
    from zimjobs_scraper.normalization import (
        is_probable_merged_job_text as _is_probable_merged_job_text,
        normalize_job_text as _normalize_job_text,
    )
except ImportError:  # pragma: no cover - fallback for unusual deployments.
    def _normalize_job_text(value, *, max_chars=None, **_):
        text = re.sub(r"[ \t\f\v]+", " ", str(value or "").replace("\r\n", "\n").replace("\r", "\n"))
        text = "\n".join(line.strip() for line in text.split("\n"))
        text = re.sub(r"\n{3,}", "\n\n", text).strip()
        return text[:max_chars].rsplit(" ", 1)[0].rstrip(".,;:-") if max_chars and len(text) > max_chars else text

    def _is_probable_merged_job_text(_title, _text):
        return False

LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    stream=sys.stdout,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
log = logging.getLogger("zimjobs.web")

DB_PATH     = os.environ.get("DB_PATH", "/data/jobs.db")
ADMIN_TOKEN = os.environ.get("ADMIN_TOKEN", "change-me")
SITE_URL    = os.environ.get("SITE_URL", "http://localhost:8000").rstrip("/")
PER_PAGE    = 20
IS_PRODUCTION = (
    os.environ.get("FLASK_ENV") == "production"
    or os.environ.get("APP_ENV") == "production"
    or bool(os.environ.get("RAILWAY_ENVIRONMENT"))
)
SECRET_KEY = os.environ.get("SECRET_KEY")
if IS_PRODUCTION and not SECRET_KEY:
    raise RuntimeError("SECRET_KEY must be set in production.")
if IS_PRODUCTION and ADMIN_TOKEN == "change-me":
    raise RuntimeError("ADMIN_TOKEN must be set to a non-default value in production.")
SITE_CONFIG = {
    "site_name": "ZimJobs Hub",
    "default_country_code": "ZW",
    "default_remote_applicant_country": "Zimbabwe",
}
RESEND_API_URL = os.environ.get("RESEND_API_URL", "https://api.resend.com/emails").strip()
RESEND_API_KEY = os.environ.get("RESEND_API_KEY", "").strip()
RESEND_FROM_EMAIL = (
    os.environ.get("RESEND_FROM_EMAIL")
    or os.environ.get("EMAIL_FROM")
    or "ZimJobs Hub <onboarding@resend.dev>"
).strip()
RESEND_REPLY_TO = os.environ.get("RESEND_REPLY_TO", "").strip()
TRANSACTIONAL_EMAILS_ENABLED = os.environ.get("TRANSACTIONAL_EMAILS_ENABLED", "1") != "0"
WHATSAPP_CHANNEL_URL = os.environ.get("WHATSAPP_CHANNEL_URL", "").strip()
AFFILIATE_DISCLOSURE = (
    "This page may contain affiliate links. If you purchase through these links, "
    "we may earn a commission at no extra cost to you."
)
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
ALERT_FREQUENCIES = {"instant", "daily", "weekly"}

CATEGORIES = ["NGO & Development", "Government", "Private Sector",
              "Remote & International", "Internships", "Gigs"]

# Optional, nullable columns added on top of the original schema. Names track
# the web app's supported subset of scraper metadata. All are additive and safe.
OPTIONAL_JOB_COLUMNS = {
    "employment_type": "TEXT",
    "salary_range":    "TEXT",
    "remote_status":   "TEXT",
    "experience_level":"TEXT",
    "expires_at":      "TEXT",
    "posted_at":       "TEXT",
    "department":      "TEXT",
    "job_description": "TEXT",
    "requirements":    "TEXT",
    "tags":            "TEXT",
}

# Vocabulary aligned with the scraper's normalization so manual entries and
# scraped values collapse into the same filter buckets.
EMPLOYMENT_TYPES = ["Full-time", "Part-time", "Contract", "Internship", "Gig"]
REMOTE_OPTIONS   = ["On-site", "Hybrid", "Remote"]
EXPERIENCE_LEVELS = ["Entry level", "Mid level", "Senior", "Management"]

SORT_OPTIONS = {
    "featured": "Featured first",
    "newest":   "Newest",
    "deadline": "Deadline soon",
}

AFFILIATE_RESOURCE_PAGES = {
    "career-resources": {
        "title": "Career Resources for Job Seekers",
        "desc": "Recommended CV, interview, course, and remote-work tools for Zimbabwean job seekers.",
        "audience": "job_seeker",
        "categories": ["resume", "interview", "courses", "remote_work", "portfolio", "career_coaching"],
        "intro": "Use these tools to improve your application materials, prepare for interviews, and build skills before applying.",
    },
    "resume-tools": {
        "title": "Best Resume and CV Tools for Job Seekers",
        "desc": "Helpful CV builders, resume review services, and profile optimization tools.",
        "audience": "job_seeker",
        "categories": ["resume", "cv_review", "linkedin"],
        "intro": "A strong CV should be clear, ATS-friendly, and tailored to the job. These resources help with that work.",
    },
    "interview-prep": {
        "title": "Best Interview Prep Tools",
        "desc": "Interview practice, coaching, and salary negotiation resources.",
        "audience": "job_seeker",
        "categories": ["interview", "salary", "career_coaching"],
        "intro": "Prepare practical examples, rehearse common questions, and go into interviews with a clearer plan.",
    },
    "online-courses": {
        "title": "Best Online Courses to Improve Your Career",
        "desc": "Recommended courses and certifications for common job categories.",
        "audience": "student",
        "categories": ["courses", "certifications", "bootcamps"],
        "intro": "Short, targeted training can help close skill gaps for entry-level, technical, and professional roles.",
    },
    "remote-work-tools": {
        "title": "Best Remote Work Tools",
        "desc": "Tools for remote job seekers, freelancers, and distributed teams.",
        "audience": "remote_worker",
        "categories": ["remote_work", "productivity", "portfolio", "freelance"],
        "intro": "Remote roles reward clear communication, proof of work, reliable payments, and organized workflows.",
    },
    "employer-resources": {
        "title": "Best Tools for Employers Hiring Talent",
        "desc": "Recommended ATS, HR, payroll, screening, and onboarding tools for employers.",
        "audience": "employer",
        "categories": ["ats", "payroll", "background_checks", "hr_software", "skills_testing"],
        "intro": "Use these tools to manage applicants professionally, screen candidates fairly, and improve hiring operations.",
    },
    "before-you-apply": {
        "title": "Recommended Tools Before You Apply",
        "desc": "A practical checklist of CV, interview, and skill-building tools before sending an application.",
        "audience": "job_seeker",
        "categories": ["resume", "interview", "courses", "portfolio"],
        "intro": "Before applying, check that your CV matches the role, your profile is current, and you can speak clearly about your skills.",
    },
}

SEO_LANDING_PAGES = {
    "harare": {
        "title": "Jobs in Harare",
        "desc": "Find current Harare jobs across NGO, government, private sector, internships, and remote-friendly roles.",
        "h1": "Jobs in Harare",
        "intro": "Browse active job vacancies in Harare, including office, field, graduate, government, NGO, and private sector roles.",
        "location_like": "Harare",
        "related": ["internships-zimbabwe", "ngo-jobs-zimbabwe", "graduate-trainee-zimbabwe"],
        "alert_category": "",
        "alert_location": "Harare",
    },
    "ngo-jobs-zimbabwe": {
        "title": "NGO Jobs in Zimbabwe",
        "desc": "Latest NGO and development jobs in Zimbabwe, including programme, finance, M&E, health, and operations roles.",
        "h1": "NGO Jobs in Zimbabwe",
        "intro": "Find active NGO and development vacancies from local and international organisations hiring in Zimbabwe.",
        "category": "NGO & Development",
        "related": ["harare", "government-jobs-zimbabwe", "remote-jobs-zimbabweans"],
        "alert_category": "NGO & Development",
        "alert_location": "",
    },
    "internships-zimbabwe": {
        "title": "Internships in Zimbabwe",
        "desc": "Browse current internships, attachments, graduate entry roles, and student opportunities in Zimbabwe.",
        "h1": "Internships in Zimbabwe",
        "intro": "Explore active internship and student opportunity listings for Zimbabwean graduates and early-career applicants.",
        "category": "Internships",
        "any_terms": ["intern", "internship", "attachment", "graduate"],
        "related": ["industrial-attachment-zimbabwe", "graduate-trainee-zimbabwe", "harare"],
        "alert_category": "Internships",
        "alert_location": "",
    },
    "government-jobs-zimbabwe": {
        "title": "Government Jobs in Zimbabwe",
        "desc": "Find current government, ministry, council, public sector, and parastatal jobs in Zimbabwe.",
        "h1": "Government Jobs in Zimbabwe",
        "intro": "Track active public sector opportunities from ministries, councils, parastatals, and government-linked employers.",
        "category": "Government",
        "any_terms": ["government", "ministry", "council", "parastatal"],
        "related": ["harare", "ngo-jobs-zimbabwe", "graduate-trainee-zimbabwe"],
        "alert_category": "Government",
        "alert_location": "",
    },
    "industrial-attachment-zimbabwe": {
        "title": "Industrial Attachment in Zimbabwe",
        "desc": "Find industrial attachment, student placement, and work-related learning opportunities in Zimbabwe.",
        "h1": "Industrial Attachment in Zimbabwe",
        "intro": "Browse attachment and placement roles for students who need practical work experience in Zimbabwe.",
        "category": "Internships",
        "any_terms": ["attachment", "industrial attachment", "work related learning", "student placement"],
        "related": ["internships-zimbabwe", "graduate-trainee-zimbabwe", "harare"],
        "alert_category": "Internships",
        "alert_location": "",
    },
    "graduate-trainee-zimbabwe": {
        "title": "Graduate Trainee Jobs in Zimbabwe",
        "desc": "Current graduate trainee, entry-level, and junior professional jobs for Zimbabwean graduates.",
        "h1": "Graduate Trainee Jobs in Zimbabwe",
        "intro": "Find graduate trainee, junior, and entry-level vacancies for recent graduates and early-career professionals.",
        "any_terms": ["graduate trainee", "graduate", "entry level", "junior"],
        "related": ["internships-zimbabwe", "industrial-attachment-zimbabwe", "harare"],
        "alert_category": "",
        "alert_location": "",
    },
    "remote-jobs-zimbabweans": {
        "title": "Remote Jobs for Zimbabweans",
        "desc": "Find remote and international jobs that Zimbabweans can apply for, including customer support, admin, tech, and freelance roles.",
        "h1": "Remote Jobs for Zimbabweans",
        "intro": "Browse remote-friendly jobs and international opportunities that can be suitable for applicants based in Zimbabwe.",
        "category": "Remote & International",
        "remote_status": "Remote",
        "any_terms": ["remote", "worldwide", "international", "virtual"],
        "related": ["ngo-jobs-zimbabwe", "harare", "graduate-trainee-zimbabwe"],
        "alert_category": "Remote & International",
        "alert_location": "",
    },
}

SIMILAR_LIMIT = 4
CLOSED_TAG = "closed"
MAX_TEXT_LENGTHS = {
    "q": 80,
    "loc": 80,
    "title": 140,
    "company": 120,
    "location": 120,
    "category": 40,
    "summary": 2000,
    "apply_url": 2048,
    "employment_type": 40,
    "salary_range": 160,
    "remote_status": 40,
    "experience_level": 40,
    "expires_at": 10,
    "posted_at": 32,
    "department": 120,
    "job_description": 12000,
    "requirements": 4000,
    "tags": 300,
}
ENUM_FIELDS = {
    "category": set(CATEGORIES),
    "employment_type": set(EMPLOYMENT_TYPES),
    "remote_status": set(REMOTE_OPTIONS),
    "experience_level": set(EXPERIENCE_LEVELS),
}

app = Flask(__name__)
app.config.update(
    SECRET_KEY=SECRET_KEY or (
        ADMIN_TOKEN if ADMIN_TOKEN != "change-me" else "dev-secret-change-me"
    ),
    SEND_FILE_MAX_AGE_DEFAULT=604800,   # 7-day static cache
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=IS_PRODUCTION or SITE_URL.startswith("https://"),
    PERMANENT_SESSION_LIFETIME=timedelta(days=14),
    MAX_CONTENT_LENGTH=int(os.environ.get("MAX_CONTENT_LENGTH", str(1024 * 1024))),
)
if os.environ.get("TRUST_PROXY_HEADERS") == "1":
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)
Compress(app)                                       # gzip/brotli all responses

SENSITIVE_KEYS = (
    "authorization", "cookie", "set-cookie", "token", "secret", "password",
    "passwd", "api-key", "apikey", "x-api-key", "csrf", "session",
)


def is_sensitive_key(key):
    key = (key or "").lower()
    return any(part in key for part in SENSITIVE_KEYS)


def truncate(value, limit=500):
    value = "" if value is None else str(value)
    return value if len(value) <= limit else value[:limit] + "...[truncated]"


def sanitize_url(url):
    if not url:
        return ""
    parts = urlsplit(url)
    query = urlencode([
        (key, "[redacted]" if is_sensitive_key(key) else truncate(value, 200))
        for key, value in parse_qsl(parts.query, keep_blank_values=True)
    ])
    return urlunsplit((parts.scheme, parts.netloc, parts.path, query, ""))


def safe_headers():
    if not has_request_context():
        return {}
    safe = {}
    for key, value in request.headers.items():
        if is_sensitive_key(key):
            safe[key] = "[redacted]"
        elif key.lower() in {"referer", "referrer"}:
            safe[key] = sanitize_url(value)
        else:
            safe[key] = truncate(value)
    return safe


def request_context_snapshot():
    if not has_request_context():
        return {}
    return {
        "method": request.method,
        "url": sanitize_url(request.url),
        "path": request.path,
        "remote_addr": request.headers.get("X-Forwarded-For", request.remote_addr),
        "user_agent": truncate(request.headers.get("User-Agent", "")),
        "headers": safe_headers(),
    }


def runtime_config_snapshot():
    return {
        "db_path": DB_PATH,
        "site_url": SITE_URL,
        "port": os.environ.get("PORT"),
        "log_level": LOG_LEVEL,
        "secret_key_set": bool(os.environ.get("SECRET_KEY")),
        "admin_token_set": bool(ADMIN_TOKEN),
        "admin_token_uses_default": ADMIN_TOKEN == "change-me",
        "admin_email_set": bool(os.environ.get("ADMIN_EMAIL")),
        "admin_password_set": bool(os.environ.get("ADMIN_PASSWORD")),
        "resend_api_key_set": bool(RESEND_API_KEY),
        "transactional_emails_enabled": TRANSACTIONAL_EMAILS_ENABLED,
        "resend_from_email_set": bool(RESEND_FROM_EMAIL),
        "enable_scraper_cron": os.environ.get("ENABLE_SCRAPER_CRON"),
        "scraper_cron_schedule_set": bool(os.environ.get("SCRAPER_CRON_SCHEDULE")),
        "python_version": sys.version.split()[0],
    }


def error_snapshot(error, status_code=None):
    return {
        "type": error.__class__.__name__,
        "message": str(error),
        "repr": repr(error),
        "status_code": status_code,
        "stack": "".join(format_exception(type(error), error, error.__traceback__)),
    }


def log_event(level, event, **fields):
    log.log(level, "%s %s", event, json.dumps(fields, default=str, sort_keys=True))


RATE_LIMIT_BUCKETS: dict[str, deque[float]] = {}
RATE_LIMIT_RULES = {
    "login": (10, 10 * 60),
    "register": (10, 60 * 60),
    "post_job": (20, 60 * 60),
    "search": (120, 60),
    "email_alert": (20, 60 * 60),
}


def client_ip():
    forwarded = request.headers.get("X-Forwarded-For", "")
    if os.environ.get("TRUST_PROXY_HEADERS") == "1" and forwarded:
        return forwarded.split(",", 1)[0].strip()
    return request.remote_addr or "unknown"


def check_rate_limit(scope, limit, window_seconds):
    now = time.time()
    key = f"{scope}:{client_ip()}"
    bucket = RATE_LIMIT_BUCKETS.setdefault(key, deque())
    while bucket and bucket[0] <= now - window_seconds:
        bucket.popleft()
    if len(bucket) >= limit:
        log_event(
            logging.WARNING,
            "rate_limit_blocked",
            scope=scope,
            limit=limit,
            window_seconds=window_seconds,
            request=request_context_snapshot(),
        )
        abort(429)
    bucket.append(now)


def enforce_route_rate_limit():
    if os.environ.get("RATE_LIMITS_ENABLED", "1") == "0":
        return
    endpoint = request.endpoint or ""
    rule = None
    if endpoint == "auth.login" and request.method == "POST":
        rule = ("login", *RATE_LIMIT_RULES["login"])
    elif endpoint == "auth.register" and request.method == "POST":
        rule = ("register", *RATE_LIMIT_RULES["register"])
    elif endpoint == "post" and request.method == "POST":
        rule = ("post_job", *RATE_LIMIT_RULES["post_job"])
    elif endpoint == "email_alert_signup" and request.method == "POST":
        rule = ("email_alert", *RATE_LIMIT_RULES["email_alert"])
    elif endpoint == "index" and request.args.get("q"):
        rule = ("search", *RATE_LIMIT_RULES["search"])
    if rule:
        check_rate_limit(*rule)


def security_csp():
    directives = [
        "default-src 'self'",
        "base-uri 'self'",
        "object-src 'none'",
        "frame-ancestors 'none'",
        "form-action 'self'",
        "img-src 'self' data: https:",
        "font-src 'self' data:",
        "style-src 'self' 'unsafe-inline'",
        "script-src 'self' 'unsafe-inline'",
        "connect-src 'self'",
        "manifest-src 'self'",
        "worker-src 'self'",
    ]
    if SITE_URL.startswith("https://"):
        directives.append("upgrade-insecure-requests")
    return "; ".join(directives)


def _global_exception_hook(exc_type, exc, tb):
    log.critical(
        "uncaught_exception %s",
        json.dumps({
            "error": {
                "type": exc_type.__name__,
                "message": str(exc),
                "repr": repr(exc),
                "stack": "".join(format_exception(exc_type, exc, tb)),
            },
            "config": runtime_config_snapshot(),
        }, default=str, sort_keys=True),
        exc_info=(exc_type, exc, tb),
    )


def _thread_exception_hook(args):
    _global_exception_hook(args.exc_type, args.exc_value, args.exc_traceback)


def _asyncio_exception_handler(_loop, context):
    error = context.get("exception")
    fields = {"message": context.get("message"), "config": runtime_config_snapshot()}
    if error:
        fields["error"] = error_snapshot(error)
        log.error(
            "unhandled_asyncio_exception %s",
            json.dumps(fields, default=str, sort_keys=True),
            exc_info=(type(error), error, error.__traceback__),
        )
    else:
        log.error("unhandled_asyncio_exception %s", json.dumps(fields, default=str, sort_keys=True))


sys.excepthook = _global_exception_hook
threading.excepthook = _thread_exception_hook
try:
    asyncio.get_event_loop().set_exception_handler(_asyncio_exception_handler)
except RuntimeError:
    pass


@app.before_request
def start_request_timer():
    g.request_started_at = time.perf_counter()
    enforce_route_rate_limit()


@app.after_request
def add_security_headers(response):
    response.headers.setdefault("Content-Security-Policy", security_csp())
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    response.headers.setdefault(
        "Permissions-Policy",
        "camera=(), microphone=(), geolocation=(), payment=(), usb=()",
    )
    if request.is_secure or SITE_URL.startswith("https://"):
        response.headers.setdefault(
            "Strict-Transport-Security",
            "max-age=31536000; includeSubDomains",
        )
    return response


@app.after_request
def log_error_response(response):
    if response.status_code >= 500:
        elapsed_ms = None
        if hasattr(g, "request_started_at"):
            elapsed_ms = round((time.perf_counter() - g.request_started_at) * 1000, 2)
        log_event(
            logging.ERROR,
            "http_error_response",
            status_code=response.status_code,
            elapsed_ms=elapsed_ms,
            request=request_context_snapshot(),
            config=runtime_config_snapshot(),
        )
    return response


# ----------------------------- database -----------------------------
def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
    return g.db


def db_columns(conn, table="jobs"):
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def closed_jobs_where_sql(cols, table="jobs"):
    prefix = f"{table}." if table else ""
    conditions = []
    if "tags" in cols:
        tags_expr = (
            f"(',' || replace(replace(lower(coalesce({prefix}tags,'')), "
            "';', ','), '|', ',') || ',') LIKE '%,closed,%'"
        )
        conditions.append(tags_expr)
    if "expires_at" in cols:
        conditions.append(
            f"({prefix}expires_at IS NOT NULL AND TRIM({prefix}expires_at) <> '' "
            f"AND date(substr({prefix}expires_at,1,10)) < date('now'))"
        )
    return "(" + " OR ".join(conditions) + ")" if conditions else "(0)"


def active_jobs_where_sql(cols, table="jobs"):
    return f"NOT {closed_jobs_where_sql(cols, table)}"


def purge_closed_jobs(conn):
    cols = db_columns(conn)
    where = closed_jobs_where_sql(cols)
    if where == "(0)":
        return 0

    saved_exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='saved_jobs'"
    ).fetchone()
    if saved_exists:
        conn.execute(
            f"DELETE FROM saved_jobs WHERE job_id IN "
            f"(SELECT id FROM jobs WHERE {where})"
        )
    cur = conn.execute(f"DELETE FROM jobs WHERE {where}")
    return cur.rowcount if cur.rowcount != -1 else 0


@app.teardown_appcontext
def close_db(_=None):
    db = g.pop("db", None)
    if db:
        db.close()


def init_db():
    db_dir = os.path.dirname(DB_PATH)
    if db_dir:
        os.makedirs(db_dir, exist_ok=True)
    db = sqlite3.connect(DB_PATH)
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("""CREATE TABLE IF NOT EXISTS jobs(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        title TEXT NOT NULL, company TEXT NOT NULL,
        location TEXT NOT NULL, category TEXT NOT NULL,
        summary TEXT NOT NULL, apply_url TEXT NOT NULL,
        featured INTEGER DEFAULT 0,
        created_at TEXT DEFAULT (datetime('now')))""")
    db.execute("CREATE INDEX IF NOT EXISTS idx_cat ON jobs(category, created_at)")

    # Additive migration: add optional columns if missing (safe on existing rows).
    existing_cols = {r[1] for r in db.execute("PRAGMA table_info(jobs)").fetchall()}
    for col, sql_type in OPTIONAL_JOB_COLUMNS.items():
        if col not in existing_cols:
            db.execute(f"ALTER TABLE jobs ADD COLUMN {col} {sql_type}")
    db.execute("""CREATE VIRTUAL TABLE IF NOT EXISTS jobs_fts USING fts5(
        title, company, summary, location,
        content='jobs', content_rowid='id')""")
    db.execute("""CREATE TRIGGER IF NOT EXISTS jobs_ai AFTER INSERT ON jobs BEGIN
        INSERT INTO jobs_fts(rowid,title,company,summary,location)
        VALUES (new.id,new.title,new.company,new.summary,new.location); END""")
    db.execute("""CREATE TRIGGER IF NOT EXISTS jobs_ad AFTER DELETE ON jobs BEGIN
        INSERT INTO jobs_fts(jobs_fts,rowid,title,company,summary,location)
        VALUES('delete',old.id,old.title,old.company,old.summary,old.location); END""")
    db.execute("""CREATE TABLE IF NOT EXISTS affiliate_offers(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        offer_category TEXT NOT NULL,
        audience_type TEXT NOT NULL,
        job_category_targets TEXT DEFAULT '',
        placement_locations TEXT NOT NULL,
        affiliate_url TEXT NOT NULL,
        display_title TEXT NOT NULL,
        description TEXT NOT NULL,
        cta_text TEXT NOT NULL,
        image_url TEXT,
        disclosure_text TEXT,
        is_active INTEGER NOT NULL DEFAULT 1,
        priority_score INTEGER NOT NULL DEFAULT 0,
        tracking_id TEXT,
        created_at TEXT DEFAULT (datetime('now')),
        updated_at TEXT DEFAULT (datetime('now')))""")
    db.execute("""CREATE TABLE IF NOT EXISTS affiliate_events(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        offer_id INTEGER NOT NULL,
        placement_id TEXT NOT NULL,
        event_type TEXT NOT NULL,
        page_path TEXT,
        job_category TEXT,
        user_type TEXT,
        device_type TEXT,
        user_id INTEGER,
        created_at TEXT DEFAULT (datetime('now')),
        FOREIGN KEY(offer_id) REFERENCES affiliate_offers(id))""")
    db.execute(
        "CREATE INDEX IF NOT EXISTS idx_affiliate_events_offer "
        "ON affiliate_events(offer_id, event_type, created_at)")
    db.execute(
        "CREATE INDEX IF NOT EXISTS idx_affiliate_events_placement "
        "ON affiliate_events(placement_id, event_type, created_at)")
    db.execute("""CREATE TABLE IF NOT EXISTS email_alerts(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        email TEXT NOT NULL,
        category TEXT DEFAULT '',
        location TEXT DEFAULT '',
        source TEXT DEFAULT '',
        frequency TEXT NOT NULL DEFAULT 'instant',
        active INTEGER NOT NULL DEFAULT 1,
        unsubscribe_token TEXT,
        last_sent_at TEXT,
        last_error TEXT,
        delivery_failures INTEGER NOT NULL DEFAULT 0,
        unsubscribed_at TEXT,
        created_at TEXT DEFAULT (datetime('now')),
        updated_at TEXT DEFAULT (datetime('now')))""")
    existing_alert_cols = {r[1] for r in db.execute("PRAGMA table_info(email_alerts)").fetchall()}
    alert_columns = {
        "frequency": "TEXT NOT NULL DEFAULT 'instant'",
        "active": "INTEGER NOT NULL DEFAULT 1",
        "unsubscribe_token": "TEXT",
        "last_sent_at": "TEXT",
        "last_error": "TEXT",
        "delivery_failures": "INTEGER NOT NULL DEFAULT 0",
        "unsubscribed_at": "TEXT",
    }
    for col, sql_type in alert_columns.items():
        if col not in existing_alert_cols:
            db.execute(f"ALTER TABLE email_alerts ADD COLUMN {col} {sql_type}")
    db.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_email_alerts_unique "
        "ON email_alerts(email, category, location)")
    db.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_email_alerts_unsubscribe_token "
        "ON email_alerts(unsubscribe_token) "
        "WHERE unsubscribe_token IS NOT NULL AND unsubscribe_token <> ''")
    missing_tokens = db.execute(
        "SELECT id FROM email_alerts WHERE unsubscribe_token IS NULL OR unsubscribe_token=''"
    ).fetchall()
    for row in missing_tokens:
        db.execute(
            "UPDATE email_alerts SET unsubscribe_token=? WHERE id=?",
            (secrets.token_urlsafe(24), row["id"] if isinstance(row, sqlite3.Row) else row[0]),
        )

    if db.execute("SELECT COUNT(*) FROM affiliate_offers").fetchone()[0] == 0:
        seed_affiliate_offers(db)

    had_jobs = db.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] > 0

    if not had_jobs:
        seed = [
            ("Programme Officer", "Local Development NGO", "Harare",
             "NGO & Development",
             "Coordinate community livelihood projects, manage donor reporting "
             "and liaise with district stakeholders. Degree in development "
             "studies plus 2 years NGO experience required.",
             "https://example.org/apply", 1),
            ("Registered General Nurse", "Private Hospital Group", "Bulawayo",
             "Private Sector",
             "Provide patient care in a busy private hospital. Must be "
             "registered with the Nurses Council of Zimbabwe.",
             "https://example.org/apply", 0),
            ("Customer Support Agent (Remote)", "US SaaS Startup",
             "Remote — Worldwide", "Remote & International",
             "Answer customer tickets via email and chat for a US software "
             "company. Hires worldwide, pays via PayPal/Payoneer. Strong "
             "written English required.",
             "https://example.org/apply", 1),
            ("Accounts Clerk", "Retail Group", "Harare", "Private Sector",
             "Process invoices, reconcile accounts and support month-end "
             "reporting. Diploma in accounting plus Pastel knowledge.",
             "https://example.org/apply", 0),
            ("Graduate Intern — Agriculture", "Government Ministry", "Gweru",
             "Internships",
             "12-month attachment for agriculture graduates. Field work, "
             "data collection and extension support.",
             "https://example.org/apply", 0),
            ("Virtual Assistant (Remote)", "UK Agency", "Remote — Worldwide",
             "Remote & International",
             "Diary management, research and admin for UK clients. Flexible "
             "hours, paid in GBP via Wise or PayPal.",
             "https://example.org/apply", 0),
            ("Monitoring & Evaluation Officer", "International NGO", "Mutare",
             "NGO & Development",
             "Design M&E frameworks for a health programme. Experience with "
             "donor-funded projects (USAID/Global Fund) is an advantage.",
             "https://example.org/apply", 0),
            ("Delivery Rider — Own Motorbike", "Food Delivery Service",
             "Harare", "Gigs",
             "Flexible gig work delivering food orders. Paid per delivery "
             "plus tips. Valid licence and own bike required.",
             "https://example.org/apply", 0),
        ]
        db.executemany("""INSERT INTO jobs(title,company,location,category,
            summary,apply_url,featured) VALUES(?,?,?,?,?,?,?)""", seed)
    elif db.execute("SELECT COUNT(*) FROM jobs_fts").fetchone()[0] == 0:
        db.execute("INSERT INTO jobs_fts(jobs_fts) VALUES('rebuild')")
    db.commit()
    db.close()


def seed_affiliate_offers(db):
    offers = [
        {
            "name": "ATS CV Builder",
            "offer_category": "resume",
            "audience_type": "job_seeker,student,career_changer",
            "job_category_targets": "NGO & Development,Government,Private Sector,Internships,Remote & International,Gigs",
            "placement_locations": "job_detail,empty_search,saved_jobs,resource_page",
            "affiliate_url": "https://example.com/affiliate/cv-builder",
            "display_title": "Build an ATS-friendly CV",
            "description": "Create a clean CV you can tailor before applying to roles on ZimJobs Hub.",
            "cta_text": "Try the CV builder",
            "priority_score": 95,
            "tracking_id": "cv-builder-placeholder",
        },
        {
            "name": "Professional CV Review",
            "offer_category": "cv_review",
            "audience_type": "job_seeker,career_changer",
            "job_category_targets": "NGO & Development,Government,Private Sector,Internships,Remote & International",
            "placement_locations": "job_detail,saved_jobs,resource_page",
            "affiliate_url": "https://example.com/affiliate/cv-review",
            "display_title": "Get your CV reviewed",
            "description": "A second pair of eyes can help catch weak bullet points, formatting issues, and missing keywords.",
            "cta_text": "Review my CV",
            "priority_score": 88,
            "tracking_id": "cv-review-placeholder",
        },
        {
            "name": "Interview Practice",
            "offer_category": "interview",
            "audience_type": "job_seeker,student,career_changer",
            "job_category_targets": "NGO & Development,Government,Private Sector,Internships,Remote & International",
            "placement_locations": "job_detail,saved_jobs,resource_page",
            "affiliate_url": "https://example.com/affiliate/interview-practice",
            "display_title": "Practice interview questions",
            "description": "Rehearse structured answers for common interview questions before you apply or get shortlisted.",
            "cta_text": "Start practicing",
            "priority_score": 84,
            "tracking_id": "interview-placeholder",
        },
        {
            "name": "Online Career Courses",
            "offer_category": "courses",
            "audience_type": "job_seeker,student,career_changer",
            "job_category_targets": "Internships,Private Sector,Remote & International,NGO & Development",
            "placement_locations": "job_detail,empty_search,resource_page",
            "affiliate_url": "https://example.com/affiliate/career-courses",
            "display_title": "Close a skill gap with a short course",
            "description": "Find practical courses for Excel, project management, customer support, data, and business skills.",
            "cta_text": "Browse courses",
            "priority_score": 76,
            "tracking_id": "courses-placeholder",
        },
        {
            "name": "Coding Interview Prep",
            "offer_category": "certifications",
            "audience_type": "job_seeker,student,remote_worker",
            "job_category_targets": "Remote & International,Private Sector",
            "placement_locations": "job_detail,resource_page",
            "affiliate_url": "https://example.com/affiliate/coding-interview-prep",
            "display_title": "Prepare for technical interviews",
            "description": "Practice coding, systems, GitHub portfolio reviews, and cloud certification paths for software roles.",
            "cta_text": "Prepare for tech roles",
            "priority_score": 90,
            "tracking_id": "tech-prep-placeholder",
        },
        {
            "name": "Healthcare Certification Prep",
            "offer_category": "certifications",
            "audience_type": "job_seeker,student",
            "job_category_targets": "Private Sector,Government,NGO & Development",
            "placement_locations": "job_detail,resource_page",
            "affiliate_url": "https://example.com/affiliate/healthcare-certifications",
            "display_title": "Prepare healthcare credentials",
            "description": "Review licensing, compliance, and healthcare interview resources before applying to clinical roles.",
            "cta_text": "View healthcare prep",
            "priority_score": 82,
            "tracking_id": "healthcare-placeholder",
        },
        {
            "name": "Remote Work Toolkit",
            "offer_category": "remote_work",
            "audience_type": "job_seeker,remote_worker,freelancer",
            "job_category_targets": "Remote & International,Gigs",
            "placement_locations": "job_detail,empty_search,resource_page",
            "affiliate_url": "https://example.com/affiliate/remote-work-tools",
            "display_title": "Set up for remote work",
            "description": "Organize applications, portfolio links, payments, and productivity tools for remote roles.",
            "cta_text": "View remote tools",
            "priority_score": 86,
            "tracking_id": "remote-toolkit-placeholder",
        },
        {
            "name": "Portfolio Website Builder",
            "offer_category": "portfolio",
            "audience_type": "job_seeker,student,remote_worker,career_changer",
            "job_category_targets": "Remote & International,Private Sector,Gigs",
            "placement_locations": "job_detail,resource_page",
            "affiliate_url": "https://example.com/affiliate/portfolio-builder",
            "display_title": "Build a simple portfolio site",
            "description": "Show projects, writing samples, case studies, or freelance work with a professional portfolio.",
            "cta_text": "Build a portfolio",
            "priority_score": 72,
            "tracking_id": "portfolio-placeholder",
        },
        {
            "name": "Salary Negotiation Guide",
            "offer_category": "salary",
            "audience_type": "job_seeker,career_changer",
            "job_category_targets": "Private Sector,Remote & International,NGO & Development",
            "placement_locations": "job_detail,resource_page",
            "affiliate_url": "https://example.com/affiliate/salary-negotiation",
            "display_title": "Prepare your salary conversation",
            "description": "Compare expectations, script negotiation points, and avoid underselling senior experience.",
            "cta_text": "Plan negotiation",
            "priority_score": 70,
            "tracking_id": "salary-placeholder",
        },
        {
            "name": "Applicant Tracking System",
            "offer_category": "ats",
            "audience_type": "employer,recruiter",
            "job_category_targets": "All",
            "placement_locations": "employer_page,post_job,resource_page",
            "affiliate_url": "https://example.com/affiliate/ats",
            "display_title": "Manage applicants in one place",
            "description": "Use an ATS to collect applications, shortlist candidates, and keep hiring notes organized.",
            "cta_text": "Compare ATS tools",
            "priority_score": 94,
            "tracking_id": "ats-placeholder",
        },
        {
            "name": "Payroll and HR Software",
            "offer_category": "payroll",
            "audience_type": "employer,recruiter",
            "job_category_targets": "All",
            "placement_locations": "employer_page,post_job,resource_page",
            "affiliate_url": "https://example.com/affiliate/payroll-hr",
            "display_title": "Simplify payroll and HR admin",
            "description": "Evaluate payroll, leave, onboarding, and employee record tools for growing teams.",
            "cta_text": "View HR tools",
            "priority_score": 82,
            "tracking_id": "payroll-placeholder",
        },
        {
            "name": "Background Check Service",
            "offer_category": "background_checks",
            "audience_type": "employer,recruiter,job_seeker",
            "job_category_targets": "Private Sector,Government,NGO & Development",
            "placement_locations": "employer_page,job_detail,resource_page",
            "affiliate_url": "https://example.com/affiliate/background-checks",
            "display_title": "Prepare background checks professionally",
            "description": "Helpful for employers screening candidates and applicants preparing required documents.",
            "cta_text": "Review options",
            "priority_score": 78,
            "tracking_id": "background-placeholder",
        },
        {
            "name": "Skills Testing Platform",
            "offer_category": "skills_testing",
            "audience_type": "employer,recruiter",
            "job_category_targets": "All",
            "placement_locations": "employer_page,resource_page",
            "affiliate_url": "https://example.com/affiliate/skills-testing",
            "display_title": "Screen candidates with skills tests",
            "description": "Add structured tests for technical, admin, language, and customer support roles.",
            "cta_text": "Explore testing tools",
            "priority_score": 76,
            "tracking_id": "skills-placeholder",
        },
    ]
    db.executemany(
        """INSERT INTO affiliate_offers(
            name, offer_category, audience_type, job_category_targets,
            placement_locations, affiliate_url, display_title, description,
            cta_text, priority_score, tracking_id, disclosure_text)
            VALUES(:name,:offer_category,:audience_type,:job_category_targets,
            :placement_locations,:affiliate_url,:display_title,:description,
            :cta_text,:priority_score,:tracking_id,:disclosure_text)""",
        [{**offer, "disclosure_text": AFFILIATE_DISCLOSURE} for offer in offers],
    )


# ----------------------------- helpers ------------------------------
@app.template_filter("slug")
def slug(text):
    text = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", str(text or ""))
    text = re.sub(r"<[^>]+>", " ", text)
    text = html_lib.unescape(text)
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


@app.template_filter("ago")
def ago(ts):
    try:
        dt = datetime.strptime(ts, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return (ts or "")[:10]
    s = (datetime.now(timezone.utc) - dt).total_seconds()
    if s < 3600:   return f"{max(int(s // 60), 1)}m ago"
    if s < 86400:  return f"{int(s // 3600)}h ago"
    d = int(s // 86400)
    return f"{d}d ago" if d < 30 else ts[:10]


def fts_match(q):
    tokens = re.findall(r"\w+", q)[:6]
    return " ".join(f'"{t}"*' for t in tokens)


def job_filter_clauses(cols, cat="", jtype="", work="", exp="", loc=""):
    clauses = [active_jobs_where_sql(cols, "jobs")]
    args = []
    if cat:
        clauses.append("jobs.category = ?")
        args.append(cat)
    if jtype and "employment_type" in cols:
        clauses.append("jobs.employment_type = ?")
        args.append(jtype)
    if work and "remote_status" in cols:
        clauses.append("jobs.remote_status = ?")
        args.append(work)
    if exp and "experience_level" in cols:
        clauses.append("jobs.experience_level = ?")
        args.append(exp)
    if loc:
        clauses.append("jobs.location LIKE ?")
        args.append(f"%{loc}%")
    return clauses, args


def job_like_search_clauses(q):
    terms = re.findall(r"\w+", q)[:6]
    clauses = []
    args = []
    fields = ("jobs.title", "jobs.company", "jobs.summary", "jobs.location")
    for term in terms:
        clauses.append("(" + " OR ".join(f"{field} LIKE ?" for field in fields) + ")")
        args.extend([f"%{term}%"] * len(fields))
    return clauses, args


def clean_control_chars(value):
    return re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", " ", str(value or ""))


def limited_arg(name, default="", max_chars=80):
    value = clean_control_chars(request.args.get(name, default)).strip()
    if len(value) > max_chars:
        log_event(
            logging.WARNING,
            "invalid_input",
            field=name,
            reason="too_long",
            max_chars=max_chars,
            request=request_context_snapshot(),
        )
        abort(400)
    return value


def page_arg(name="page"):
    raw = request.args.get(name, "1")
    try:
        page = int(raw or 1)
    except (TypeError, ValueError):
        log_event(
            logging.WARNING,
            "invalid_input",
            field=name,
            reason="not_integer",
            request=request_context_snapshot(),
        )
        abort(400)
    if page < 1 or page > 1000:
        log_event(
            logging.WARNING,
            "invalid_input",
            field=name,
            reason="out_of_range",
            request=request_context_snapshot(),
        )
        abort(400)
    return page


def require_allowed_value(field, value, allowed):
    if value and value not in allowed:
        log_event(
            logging.WARNING,
            "invalid_input",
            field=field,
            reason="invalid_choice",
            request=request_context_snapshot(),
        )
        abort(400)
    return value


def safe_redirect_target(target, fallback):
    target = (target or "").strip()
    parts = urlsplit(target)
    if not target.startswith("/") or target.startswith("//") or parts.scheme or parts.netloc:
        return fallback
    return target


def email_hash(email):
    import hashlib
    return hashlib.sha256(str(email or "").lower().encode("utf-8")).hexdigest()[:12]


def normalize_recipients(to):
    recipients = [to] if isinstance(to, str) else list(to or [])
    cleaned = []
    for recipient in recipients[:50]:
        email = clean_inline_job_text(recipient).lower()
        if EMAIL_RE.match(email):
            cleaned.append(email)
    return cleaned


def resend_tag(name, value):
    name = re.sub(r"[^A-Za-z0-9_-]+", "_", str(name or ""))[:256].strip("_")
    value = re.sub(r"[^A-Za-z0-9_-]+", "_", str(value or ""))[:256].strip("_")
    return {"name": name or "type", "value": value or "unknown"}


def send_transactional_email(to, subject, text, html="", tags=None,
                             idempotency_key=""):
    recipients = normalize_recipients(to)
    if not recipients:
        log_event(logging.WARNING, "email_skipped", reason="invalid_recipient")
        return False
    if not TRANSACTIONAL_EMAILS_ENABLED:
        log_event(logging.INFO, "email_skipped", reason="disabled",
                  recipient_hashes=[email_hash(r) for r in recipients])
        return False
    if not RESEND_API_KEY:
        log_event(logging.INFO, "email_skipped", reason="resend_not_configured",
                  recipient_hashes=[email_hash(r) for r in recipients])
        return False

    payload = {
        "from": RESEND_FROM_EMAIL,
        "to": recipients,
        "subject": clean_inline_job_text(subject)[:998],
        "text": str(text or "").strip(),
    }
    if html:
        payload["html"] = str(html)
    if RESEND_REPLY_TO:
        payload["reply_to"] = RESEND_REPLY_TO
    if tags:
        payload["tags"] = [resend_tag(k, v) for k, v in tags.items()]

    headers = {
        "Authorization": f"Bearer {RESEND_API_KEY}",
        "Content-Type": "application/json",
    }
    if idempotency_key:
        headers["Idempotency-Key"] = re.sub(
            r"[^A-Za-z0-9:_-]+",
            "_",
            clean_inline_job_text(idempotency_key),
        )[:256]

    try:
        response = requests.post(
            RESEND_API_URL,
            json=payload,
            headers=headers,
            timeout=10,
        )
    except requests.RequestException as exc:
        log_event(
            logging.WARNING,
            "email_send_failed",
            provider="resend",
            error=error_snapshot(exc),
            recipient_hashes=[email_hash(r) for r in recipients],
        )
        return False

    if not 200 <= response.status_code < 300:
        log_event(
            logging.WARNING,
            "email_send_rejected",
            provider="resend",
            status_code=response.status_code,
            response=truncate(response.text, 500),
            recipient_hashes=[email_hash(r) for r in recipients],
        )
        return False

    message_id = ""
    try:
        message_id = (response.json() or {}).get("id", "")
    except ValueError:
        pass
    log_event(
        logging.INFO,
        "email_sent",
        provider="resend",
        message_id=message_id,
        recipient_hashes=[email_hash(r) for r in recipients],
        tags=tags or {},
    )
    return True


def email_button(label, href):
    return (
        '<p><a href="' + escape(href) + '" '
        'style="display:inline-block;background:#0F766E;color:#fff;'
        'padding:10px 16px;border-radius:8px;text-decoration:none;'
        'font-weight:700">' + escape(label) + "</a></p>"
    )


def send_welcome_email(email, name=""):
    first_name = clean_inline_job_text(name).split(" ", 1)[0] or "there"
    subject = "Welcome to ZimJobs Hub"
    text = (
        f"Hi {first_name},\n\n"
        "Welcome to ZimJobs Hub. You can now save jobs and manage your profile.\n\n"
        f"Browse jobs: {absolute_url('/')}\n"
    )
    html = (
        f"<p>Hi {escape(first_name)},</p>"
        "<p>Welcome to ZimJobs Hub. You can now save jobs and manage your profile.</p>"
        + email_button("Browse jobs", absolute_url("/"))
    )
    return send_transactional_email(
        email,
        subject,
        text,
        html,
        tags={"type": "welcome"},
        idempotency_key=f"welcome:{email_hash(email)}",
    )


def clean_alert_frequency(value):
    value = clean_inline_job_text(value or "instant").lower()
    return value if value in ALERT_FREQUENCIES else "instant"


def new_unsubscribe_token():
    return secrets.token_urlsafe(24)


def alert_unsubscribe_url(token):
    return absolute_url(url_for("email_alert_unsubscribe", token=token)) if token else ""


def send_email_alert_confirmation(email, category="", location="", unsubscribe_token=""):
    parts = [part for part in (category, location) if part]
    label = " for " + " in ".join(parts) if parts else ""
    if not unsubscribe_token and has_request_context():
        row = get_db().execute(
            """SELECT unsubscribe_token FROM email_alerts
               WHERE email=? AND category=? AND location=?""",
            (email, category, location),
        ).fetchone()
        unsubscribe_token = row["unsubscribe_token"] if row else ""
    unsubscribe_url = alert_unsubscribe_url(unsubscribe_token)
    subject = "Your ZimJobs Hub job alerts are active"
    text = (
        f"Your ZimJobs Hub email job alerts{label} are active.\n\n"
        "We will use this address for job alert updates as the alert feature grows.\n"
        f"Browse current jobs: {absolute_url('/')}\n"
        + (f"Unsubscribe: {unsubscribe_url}\n\n" if unsubscribe_url else "\n")
        + "If you did not request this, you can ignore this email."
    )
    html = (
        f"<p>Your ZimJobs Hub email job alerts{escape(label)} are active.</p>"
        "<p>We will use this address for job alert updates as the alert feature grows.</p>"
        + email_button("Browse current jobs", absolute_url("/"))
        + (f'<p><a href="{escape(unsubscribe_url)}">Unsubscribe from these alerts</a></p>' if unsubscribe_url else "")
        + "<p>If you did not request this, you can ignore this email.</p>"
    )
    return send_transactional_email(
        email,
        subject,
        text,
        html,
        tags={"type": "email_alert", "category": category or "all"},
        idempotency_key=f"email_alert:{email_hash(email)}:{category}:{location}",
    )


def send_job_published_email(job_id, values):
    admin_email = clean_inline_job_text(os.environ.get("ADMIN_EMAIL", "")).lower()
    if not EMAIL_RE.match(admin_email):
        return False
    title = clean_inline_job_text(values.get("title", "New job"))
    company = clean_inline_job_text(values.get("company", ""))
    location = clean_inline_job_text(values.get("location", ""))
    job_url = absolute_url(f"/job/{job_id}/{slug(title)}")
    subject = f"Job published: {title[:120]}"
    text = (
        f"A job has been published on ZimJobs Hub.\n\n"
        f"Title: {title}\nCompany: {company}\nLocation: {location}\n"
        f"View: {job_url}\n"
    )
    html = (
        "<p>A job has been published on ZimJobs Hub.</p>"
        "<ul>"
        f"<li><strong>Title:</strong> {escape(title)}</li>"
        f"<li><strong>Company:</strong> {escape(company)}</li>"
        f"<li><strong>Location:</strong> {escape(location)}</li>"
        "</ul>"
        + email_button("View job", job_url)
    )
    return send_transactional_email(
        admin_email,
        subject,
        text,
        html,
        tags={"type": "job_published", "category": values.get("category", "")},
        idempotency_key=f"job_published:{job_id}",
    )


def is_safe_public_url(url):
    url = clean_control_chars(url).strip()
    if not url or len(url) > MAX_TEXT_LENGTHS["apply_url"]:
        return False
    parts = urlsplit(url)
    return parts.scheme in {"http", "https"} and bool(parts.netloc)


def valid_admin_token(value):
    return bool(value) and hmac.compare_digest(str(value), ADMIN_TOKEN)


@app.template_filter("safe_external_url")
def safe_external_url(url):
    url = clean_control_chars(url).strip()
    return url if is_safe_public_url(url) else ""


def validate_job_values(values, required_fields):
    for field in required_fields:
        if not values.get(field):
            return "All fields are required."
    for field, value in values.items():
        if value is None:
            continue
        max_chars = MAX_TEXT_LENGTHS.get(field)
        if max_chars and len(value) > max_chars:
            return f"{field.replace('_', ' ').title()} is too long."
    for field, allowed in ENUM_FIELDS.items():
        if values.get(field) and values[field] not in allowed:
            return f"Invalid {field.replace('_', ' ')}."
    if values.get("apply_url") and not is_safe_public_url(values["apply_url"]):
        return "Apply URL must be a valid http or https URL."
    for date_field in ("expires_at", "posted_at"):
        value = values.get(date_field)
        if value and not re.match(r"^\d{4}-\d{2}-\d{2}(?:\s+\d{2}:\d{2}:\d{2})?$", value):
            return f"Invalid {date_field.replace('_', ' ')}."
    return None


def job_columns(db=None):
    db = db or get_db()
    return db_columns(db)


def optional_job_values(form, cols):
    """Map optional job columns present in both the form and the schema."""
    out = {}
    for col in OPTIONAL_JOB_COLUMNS:
        if col in cols and col in form:
            out[col] = clean_job_form_value(col, form.get(col, "")) or None
    return out


MULTILINE_JOB_FIELDS = {"summary", "job_description", "requirements"}


def clean_job_display_text(value, max_chars=12000):
    return _normalize_job_text(value, max_chars=max_chars)


def clean_inline_job_text(value):
    return re.sub(r"\s+", " ", clean_control_chars(value)).strip()


def truncate_meta_description(value, limit=160):
    text = clean_inline_job_text(value)
    if len(text) <= limit:
        return text
    truncated = text[:limit + 1].rsplit(" ", 1)[0].rstrip(".,;:-")
    return truncated or text[:limit].rstrip()


def job_meta_description(row):
    title = clean_inline_job_text(row_value(row, "title"))
    company = clean_inline_job_text(row_value(row, "company"))
    location = clean_inline_job_text(row_value(row, "location"))
    summary = clean_inline_job_text(
        clean_job_display_text(
            row_value(row, "job_description") or row_value(row, "summary"),
            max_chars=260,
        )
    )
    intro = " ".join(part for part in [
        f"{title} at {company}" if title and company else title or company,
        f"- {location}" if location else "",
    ] if part)
    if intro and summary:
        description = f"{intro}. {summary}"
    else:
        description = intro or summary
    return truncate_meta_description(description)


def clean_job_form_value(field, value):
    if field in MULTILINE_JOB_FIELDS:
        return clean_job_display_text(value)
    return clean_inline_job_text(value)


def clean_core_job_values(form, fields):
    return {field: clean_job_form_value(field, form.get(field, "")) for field in fields}


def distinct_values(db, column):
    if column not in job_columns(db):
        return []
    rows = db.execute(
        f"SELECT DISTINCT {column} v FROM jobs "
        f"WHERE {column} IS NOT NULL AND TRIM({column}) <> '' ORDER BY v").fetchall()
    return [r["v"] for r in rows]


def csv_values(value):
    return {
        part.strip().lower()
        for part in re.split(r"[,;|]", value or "")
        if part.strip()
    }


def affiliate_device_type():
    ua = (request.headers.get("User-Agent") or "").lower()
    if any(token in ua for token in ("mobile", "android", "iphone")):
        return "mobile"
    if any(token in ua for token in ("ipad", "tablet")):
        return "tablet"
    return "desktop"


def current_affiliate_user_type(default="job_seeker"):
    user = g.get("user")
    if user and row_value(user, "role") == "admin":
        return "employer"
    return default


def affiliate_click_url(offer, placement_id, job_category=""):
    args = {
        "placement": placement_id,
        "page": request.path if has_request_context() else "",
        "job_category": job_category or "",
    }
    return url_for("affiliate_click", offer_id=offer["id"], **args)


def affiliate_outbound_url(offer, placement_id):
    url = safe_external_url(offer["affiliate_url"])
    if not url:
        return ""
    parts = urlsplit(url)
    query = dict(parse_qsl(parts.query, keep_blank_values=True))
    query.setdefault("utm_source", "job_board")
    query.setdefault("utm_medium", "affiliate")
    query.setdefault("utm_campaign", row_value(offer, "offer_category", "affiliate"))
    query.setdefault("utm_content", placement_id)
    if row_value(offer, "tracking_id"):
        query.setdefault("tracking_id", offer["tracking_id"])
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))


def log_affiliate_event(offer_id, placement_id, event_type, page_path="",
                        job_category="", user_type="", device_type=""):
    if event_type not in {"impression", "click"}:
        return
    user = g.get("user")
    get_db().execute(
        """INSERT INTO affiliate_events(
            offer_id, placement_id, event_type, page_path, job_category,
            user_type, device_type, user_id)
            VALUES(?,?,?,?,?,?,?,?)""",
        (
            int(offer_id),
            clean_inline_job_text(placement_id)[:80],
            event_type,
            clean_inline_job_text(page_path or request.path)[:240],
            clean_inline_job_text(job_category)[:80],
            clean_inline_job_text(user_type or current_affiliate_user_type())[:40],
            clean_inline_job_text(device_type or affiliate_device_type())[:40],
            user["id"] if user else None,
        ),
    )
    get_db().commit()


def affiliate_keyword_score(offer, job=None, query=""):
    haystack = " ".join([
        row_value(job, "title") if job else "",
        row_value(job, "summary") if job else "",
        row_value(job, "job_description") if job else "",
        row_value(job, "requirements") if job else "",
        row_value(job, "remote_status") if job else "",
        row_value(job, "experience_level") if job else "",
        query or "",
    ]).lower()
    category = row_value(offer, "offer_category")
    if category in {"certifications", "bootcamps"} and any(
        word in haystack for word in (
            "software", "developer", "engineer", "data", "cloud", "python",
            "javascript", "it ", "ict", "cyber", "systems"
        )
    ):
        return 30
    if category == "certifications" and any(
        word in haystack for word in (
            "nurse", "clinical", "medical", "health", "pharmacy", "doctor",
            "midwife", "patient"
        )
    ):
        return 28
    if category == "remote_work" and any(
        word in haystack for word in ("remote", "virtual", "worldwide", "freelance")
    ):
        return 26
    if category == "portfolio" and any(
        word in haystack for word in ("designer", "developer", "writer", "creative", "remote")
    ):
        return 18
    if category == "salary" and any(
        word in haystack for word in ("senior", "manager", "lead", "director", "management")
    ):
        return 20
    if category in {"resume", "cv_review", "interview"} and any(
        word in haystack for word in ("intern", "graduate", "junior", "entry")
    ):
        return 16
    return 0


def select_affiliate_offers(audience="job_seeker", placement="resource_page",
                            job=None, category="", query="", limit=2,
                            offer_categories=None):
    db = get_db()
    rows = db.execute(
        "SELECT * FROM affiliate_offers WHERE is_active=1"
    ).fetchall()
    wanted_categories = set(offer_categories or [])
    scored = []
    job_category = category or (row_value(job, "category") if job else "")
    for offer in rows:
        offer_audiences = csv_values(offer["audience_type"])
        offer_placements = csv_values(offer["placement_locations"])
        offer_targets = csv_values(offer["job_category_targets"])
        if audience and audience.lower() not in offer_audiences:
            continue
        if placement.lower() not in offer_placements:
            continue
        if wanted_categories and offer["offer_category"] not in wanted_categories:
            continue

        score = int(row_value(offer, "priority_score", 0) or 0)
        if "all" in offer_targets:
            score += 8
        elif job_category and job_category.lower() in offer_targets:
            score += 30
        elif job_category:
            score -= 18
        score += affiliate_keyword_score(offer, job=job, query=query)
        if placement == "job_detail" and offer["offer_category"] in {"resume", "cv_review", "interview"}:
            score += 12
        scored.append((score, offer))

    scored.sort(key=lambda item: (item[0], item[1]["priority_score"], item[1]["id"]), reverse=True)
    selected = []
    seen_categories = set()
    for _, offer in scored:
        if offer["offer_category"] in seen_categories and len(scored) > limit:
            continue
        data = dict(offer)
        data["job_category"] = job_category
        data["click_url"] = affiliate_click_url(offer, placement, job_category)
        selected.append(data)
        seen_categories.add(offer["offer_category"])
        if len(selected) >= limit:
            break
    return selected


@app.template_filter("is_new")
def is_new(ts, days=3):
    try:
        dt = datetime.strptime(ts, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return False
    return (datetime.now(timezone.utc) - dt).total_seconds() < days * 86400


@app.template_filter("tags")
def tags(text):
    if not text:
        return []
    return [t.strip() for t in re.split(r"[,;|]", text) if t.strip()][:8]


@app.template_filter("job_text")
def job_text(value):
    return clean_job_display_text(value)


STOP_WORDS = {
    "and", "are", "but", "can", "for", "from", "has", "have", "hire",
    "job", "jobs", "must", "not", "our", "the", "this", "via", "with",
    "work", "will", "you", "your", "years", "required", "requireds",
    "experience", "skills", "strong", "apply", "official", "site",
}

LOW_VALUE_JOB_TITLES = {
    "jobs",
    "job",
    "categories",
    "category",
    "employers",
    "employer",
    "cookies policy",
    "cookie policy",
    "privacy policy",
    "terms",
    "terms and conditions",
    "disclaimer",
}


def row_value(row, key, default=""):
    """Read optional sqlite.Row columns without assuming every DB is migrated."""
    return row[key] if key in row.keys() and row[key] is not None else default


def row_looks_low_value_job(row):
    title = clean_inline_job_text(row_value(row, "title")).lower()
    if not title or title in LOW_VALUE_JOB_TITLES:
        return True
    if len(title) > MAX_TEXT_LENGTHS["title"]:
        return True
    apply_path = urlsplit(row_value(row, "apply_url")).path.strip("/").lower()
    if apply_path in {"jobs", "job", "categories", "employers"} and title in LOW_VALUE_JOB_TITLES:
        return True
    return False


def row_is_public_job(row):
    return (
        not is_closed_job(row)
        and not row_looks_accidentally_merged(row)
        and not row_looks_low_value_job(row)
    )


def absolute_url(path=""):
    if not path:
        path = "/"
    if path.startswith(("http://", "https://")):
        return path
    if not path.startswith("/"):
        path = "/" + path
    return f"{SITE_URL}{path}"


def canonical_path():
    endpoint = request.endpoint or ""
    if endpoint == "index":
        return "/"
    if endpoint == "services":
        return "/services"
    if endpoint == "post":
        return "/post"
    if endpoint == "resource_page":
        return request.path
    if endpoint == "seo_landing_page":
        return request.path
    return ""


@app.context_processor
def inject_seo_context():
    if not has_request_context():
        return {}

    endpoint = request.endpoint or ""
    robots_meta = ""
    if endpoint == "index" and request.args:
        robots_meta = "noindex,follow"
    elif endpoint.startswith("auth.") or endpoint.startswith("admin."):
        robots_meta = "noindex,follow"
    elif endpoint in {"affiliate_click", "affiliate_event", "health", "health_live"}:
        robots_meta = "noindex,follow"

    path = canonical_path()
    return {
        "default_canonical_url": absolute_url(path) if path else "",
        "default_robots_meta": robots_meta,
        "whatsapp_channel_url": WHATSAPP_CHANNEL_URL,
    }


def url_with_query_param(target, key, value):
    parts = urlsplit(target or "/")
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
             if k != key]
    query.append((key, value))
    return urlunsplit((parts.scheme, parts.netloc, parts.path,
                       urlencode(query), parts.fragment))


def safe_form_next(default="/"):
    target = request.form.get("next") or request.referrer or default
    if target.startswith(SITE_URL):
        parts = urlsplit(target)
        target = urlunsplit(("", "", parts.path or "/", parts.query, ""))
    return safe_redirect_target(target, default)


def sitemap_entry(loc, lastmod="", changefreq="", priority=""):
    bits = [f"<loc>{escape(loc)}</loc>"]
    if lastmod:
        bits.append(f"<lastmod>{escape(lastmod[:10])}</lastmod>")
    if changefreq:
        bits.append(f"<changefreq>{escape(changefreq)}</changefreq>")
    if priority:
        bits.append(f"<priority>{escape(priority)}</priority>")
    return "<url>" + "".join(bits) + "</url>"


def row_looks_accidentally_merged(row):
    text = row_value(row, "job_description") or row_value(row, "summary")
    return _is_probable_merged_job_text(row["title"], clean_job_display_text(text))


def normalize_token(token):
    token = token.lower()
    if len(token) > 4 and token.endswith("s"):
        token = token[:-1]
    return token


def keyword_set(*texts):
    words = set()
    for text in texts:
        for word in re.findall(r"[a-z0-9]+", (text or "").lower()):
            word = normalize_token(word)
            if len(word) >= 3 and word not in STOP_WORDS:
                words.add(word)
    return words


def tag_set(text):
    out = set()
    for tag in tags(text):
        norm = " ".join(sorted(keyword_set(tag)))
        if norm:
            out.add(norm)
    return out


def parsed_date(text):
    if not text:
        return None
    m = re.search(r"\d{4}-\d{2}-\d{2}", str(text))
    if not m:
        return None
    try:
        return datetime.strptime(m.group(), "%Y-%m-%d").date()
    except ValueError:
        return None


def is_expired_job(row):
    d = parsed_date(row_value(row, "expires_at"))
    return bool(d and d < datetime.now(timezone.utc).date())


def is_closed_tagged_job(row):
    return CLOSED_TAG in {t.lower() for t in tags(row_value(row, "tags"))}


def is_closed_job(row):
    return is_closed_tagged_job(row) or is_expired_job(row)


def form_values_are_closed(form):
    d = parsed_date(form.get("expires_at", ""))
    return (
        CLOSED_TAG in {t.lower() for t in tags(form.get("tags", ""))}
        or bool(d and d < datetime.now(timezone.utc).date())
    )


def parsed_created_at(row):
    try:
        return datetime.strptime(row["created_at"], "%Y-%m-%d %H:%M:%S")
    except (KeyError, TypeError, ValueError):
        return datetime.min


def text_for_similarity(row):
    return " ".join([
        row_value(row, "summary"),
        row_value(row, "job_description"),
        row_value(row, "requirements"),
        row_value(row, "department"),
    ])


def similar_jobs(db, job, limit=SIMILAR_LIMIT):
    """Rank active jobs by how closely they match the currently viewed job."""
    base_title = keyword_set(job["title"])
    base_tags = tag_set(row_value(job, "tags"))
    base_content = keyword_set(text_for_similarity(job))
    base_location = keyword_set(row_value(job, "location"))

    candidates = db.execute(
        "SELECT * FROM jobs WHERE id<>? ORDER BY featured DESC, created_at DESC",
        (job["id"],)).fetchall()
    ranked, fallback = [], []
    for candidate in candidates:
        if not row_is_public_job(candidate):
            continue

        fallback.append(candidate)
        score = 0
        job_category_words = keyword_set(job["category"])
        candidate_category_words = keyword_set(candidate["category"])
        if candidate["category"] == job["category"]:
            score += 24
        elif "remote" in job_category_words and "remote" in candidate_category_words:
            score += 8

        for field, weight in (
            ("employment_type", 8),
            ("remote_status", 8),
            ("experience_level", 6),
        ):
            if row_value(job, field) and row_value(job, field) == row_value(candidate, field):
                score += weight

        score += len(base_title & keyword_set(candidate["title"])) * 8
        score += len(base_tags & tag_set(row_value(candidate, "tags"))) * 10
        score += min(len(base_content & keyword_set(text_for_similarity(candidate))), 10) * 2
        score += min(len(base_location & keyword_set(row_value(candidate, "location"))), 2) * 3

        if score:
            ranked.append((score, candidate))

    ranked.sort(key=lambda item: (
        item[0],
        int(row_value(item[1], "featured", 0) or 0),
        parsed_created_at(item[1]),
        item[1]["id"],
    ), reverse=True)

    selected, seen = [], set()
    for _, candidate in ranked:
        selected.append(candidate)
        seen.add(candidate["id"])
        if len(selected) == limit:
            return selected

    for candidate in fallback:
        if candidate["id"] not in seen:
            selected.append(candidate)
            seen.add(candidate["id"])
        if len(selected) == limit:
            break
    return selected


def landing_page_jobs(db, page, limit=PER_PAGE):
    cols = job_columns(db)
    where = [active_jobs_where_sql(cols, "jobs")]
    args = []

    category = page.get("category")
    if category:
        where.append("category = ?")
        args.append(category)

    remote_status = page.get("remote_status")
    if remote_status and "remote_status" in cols:
        where.append("(remote_status = ? OR location LIKE ?)")
        args.extend([remote_status, f"%{remote_status}%"])

    location_like = page.get("location_like")
    if location_like:
        where.append("location LIKE ?")
        args.append(f"%{location_like}%")

    terms = [term for term in page.get("any_terms", []) if term]
    if terms:
        search_fields = ["title", "company", "summary", "location"]
        if "job_description" in cols:
            search_fields.append("job_description")
        if "requirements" in cols:
            search_fields.append("requirements")
        term_groups = []
        for term in terms:
            term_groups.append("(" + " OR ".join(f"{field} LIKE ?" for field in search_fields) + ")")
            args.extend([f"%{term}%"] * len(search_fields))
        where.append("(" + " OR ".join(term_groups) + ")")

    rows = db.execute(
        "SELECT * FROM jobs WHERE " + " AND ".join(where) +
        " ORDER BY featured DESC, created_at DESC LIMIT ?",
        args + [limit],
    ).fetchall()
    return [row for row in rows if row_is_public_job(row)]


@app.template_filter("deadline")
def deadline(ts):
    """Human-friendly deadline label from a YYYY-MM-DD(ish) string."""
    if not ts:
        return ""
    m = re.search(r"\d{4}-\d{2}-\d{2}", ts)
    if not m:
        return ts.strip()[:40]
    try:
        d = datetime.strptime(m.group(), "%Y-%m-%d").date()
    except ValueError:
        return ts.strip()[:40]
    days = (d - datetime.now(timezone.utc).date()).days
    if days < 0:
        return "Closed"
    if days == 0:
        return "Closes today"
    if days == 1:
        return "Closes tomorrow"
    if days <= 7:
        return f"Closes in {days} days"
    return d.strftime("%d %b %Y")


# ------------------------------ routes ------------------------------
@app.route("/")
def index():
    q     = limited_arg("q", max_chars=MAX_TEXT_LENGTHS["q"])
    cat   = require_allowed_value("cat", limited_arg("cat", max_chars=MAX_TEXT_LENGTHS["category"]), CATEGORIES)
    jtype = require_allowed_value("type", limited_arg("type", max_chars=MAX_TEXT_LENGTHS["employment_type"]), EMPLOYMENT_TYPES)
    work  = require_allowed_value("remote", limited_arg("remote", max_chars=MAX_TEXT_LENGTHS["remote_status"]), REMOTE_OPTIONS)
    exp   = require_allowed_value("exp", limited_arg("exp", max_chars=MAX_TEXT_LENGTHS["experience_level"]), EXPERIENCE_LEVELS)
    loc   = limited_arg("loc", max_chars=MAX_TEXT_LENGTHS["loc"])
    sort  = limited_arg("sort", max_chars=20)
    if sort not in SORT_OPTIONS:
        sort = "featured"
    page  = page_arg()
    db    = get_db()
    cols  = job_columns(db)

    fts_query = fts_match(q) if q else ""
    filter_clauses, filter_args = job_filter_clauses(cols, cat, jtype, work, exp, loc)
    using_fts = bool(fts_query)
    if using_fts:
        base = ("FROM jobs JOIN jobs_fts ON jobs.id = jobs_fts.rowid "
                "WHERE jobs_fts MATCH ?")
        args = [fts_query]
    else:
        base, args = "FROM jobs WHERE 1=1", []
    base += " AND " + " AND ".join(filter_clauses)
    args += filter_args

    if sort == "newest":
        order = "jobs.created_at DESC"
    elif sort == "deadline" and "expires_at" in cols:
        order = ("(jobs.expires_at IS NULL OR TRIM(jobs.expires_at)='') ASC, "
                 "jobs.expires_at ASC, jobs.created_at DESC")
    else:
        order = "jobs.featured DESC, jobs.created_at DESC"

    try:
        total = db.execute("SELECT COUNT(*) c " + base, args).fetchone()["c"]
        jobs  = db.execute(
            "SELECT jobs.* " + base +
            f" ORDER BY {order} LIMIT ? OFFSET ?",
            args + [PER_PAGE, (page - 1) * PER_PAGE]).fetchall()
        jobs = [job for job in jobs if row_is_public_job(job)]
    except sqlite3.OperationalError as exc:
        if not using_fts:
            log_event(
                logging.ERROR,
                "jobs_query_failed",
                error=error_snapshot(exc),
                request=request_context_snapshot(),
                query={"base": base, "args_count": len(args), "sort": sort},
                config=runtime_config_snapshot(),
            )
            total, jobs = 0, []
        else:
            log_event(
                logging.WARNING,
                "jobs_fts_fallback",
                error=error_snapshot(exc),
                request=request_context_snapshot(),
                query={"args_count": len(args), "sort": sort},
            )
            like_clauses, like_args = job_like_search_clauses(q)
            fallback_clauses = filter_clauses + like_clauses
            fallback_args = filter_args + like_args
            fallback_base = "FROM jobs WHERE " + " AND ".join(fallback_clauses)
            try:
                total = db.execute("SELECT COUNT(*) c " + fallback_base, fallback_args).fetchone()["c"]
                jobs = db.execute(
                    "SELECT jobs.* " + fallback_base +
                    f" ORDER BY {order} LIMIT ? OFFSET ?",
                    fallback_args + [PER_PAGE, (page - 1) * PER_PAGE],
                ).fetchall()
                jobs = [job for job in jobs if row_is_public_job(job)]
            except sqlite3.OperationalError as fallback_exc:
                log_event(
                    logging.ERROR,
                    "jobs_query_failed",
                    error=error_snapshot(fallback_exc),
                    request=request_context_snapshot(),
                    query={"base": fallback_base, "args_count": len(fallback_args), "sort": sort},
                    config=runtime_config_snapshot(),
                )
                total, jobs = 0, []

    filters = {"q": q, "cat": cat, "type": jtype, "remote": work,
               "exp": exp, "loc": loc, "sort": sort}
    active = any(v for k, v in filters.items()
                 if k not in ("sort",) and v) or sort != "featured"
    empty_search_offers = select_affiliate_offers(
        audience="job_seeker",
        placement="empty_search",
        category=cat,
        query=q,
        limit=2,
    ) if not jobs else []
    return render_template(
        "index.html", jobs=jobs, q=q, cat=cat, categories=CATEGORIES,
        page=page, total=total, pages=max(ceil(total / PER_PAGE), 1),
        filters=filters, active_filters=active, sort=sort,
        sort_options=SORT_OPTIONS,
        type_options=distinct_values(db, "employment_type") or [],
        remote_options=distinct_values(db, "remote_status") or [],
        exp_options=distinct_values(db, "experience_level") or [],
        empty_search_offers=empty_search_offers)


@app.route("/job/<int:job_id>")
@app.route("/job/<int:job_id>/<s>")
def job(job_id, s=None):
    if s and (len(s) > 160 or not re.match(r"^[A-Za-z0-9_-]+$", s)):
        abort(400)
    db  = get_db()
    row = db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
    if not row:
        abort(404)
    if is_closed_job(row):
        abort(410)
    if not row_is_public_job(row):
        abort(404)
    canonical_slug = slug(row["title"])
    url = f"{SITE_URL}/job/{row['id']}/{canonical_slug}"
    if s != canonical_slug:
        return redirect(url, code=301)
    similar = similar_jobs(db, row)
    job_json_ld = build_job_posting_json_ld(row, {**SITE_CONFIG, "job_url": url})
    affiliate_offers = select_affiliate_offers(
        audience="job_seeker",
        placement="job_detail",
        job=row,
        limit=2,
    )
    return render_template("job.html", job=row, url=url, similar=similar,
                           canonical_url=url,
                           meta_description=job_meta_description(row),
                           job_json_ld=job_json_ld,
                           affiliate_offers=affiliate_offers,
                           categories=CATEGORIES, cat=None, q="")


@app.route("/jobs/<slug_name>/")
def seo_landing_page(slug_name):
    page = SEO_LANDING_PAGES.get(slug_name)
    if not page:
        abort(404)
    db = get_db()
    jobs = landing_page_jobs(db, page)
    related_pages = [
        (related_slug, SEO_LANDING_PAGES[related_slug])
        for related_slug in page.get("related", [])
        if related_slug in SEO_LANDING_PAGES
    ]
    canonical_url = absolute_url(f"/jobs/{slug_name}/")
    return render_template(
        "landing.html",
        page=page,
        slug_name=slug_name,
        jobs=jobs,
        related_pages=related_pages,
        canonical_url=canonical_url,
        categories=CATEGORIES,
        cat=None,
        q="",
    )


@app.route("/services")
def services():
    affiliate_offers = select_affiliate_offers(
        audience="job_seeker",
        placement="resource_page",
        limit=3,
        offer_categories=["resume", "cv_review", "linkedin", "interview"],
    )
    return render_template("services.html", categories=CATEGORIES,
                           cat=None, q="", affiliate_offers=affiliate_offers)


@app.route("/resources")
def resources_home():
    return redirect(url_for("resource_page", slug_name="career-resources"))


@app.route("/resources/<slug_name>")
def resource_page(slug_name):
    page = AFFILIATE_RESOURCE_PAGES.get(slug_name)
    if not page:
        abort(404)
    offers = select_affiliate_offers(
        audience=page["audience"],
        placement="resource_page",
        limit=12,
        offer_categories=page["categories"],
    )
    return render_template(
        "resources.html",
        page=page,
        pages=AFFILIATE_RESOURCE_PAGES,
        offers=offers,
        disclosure=AFFILIATE_DISCLOSURE,
        categories=CATEGORIES,
        cat=None,
        q="",
    )


@app.route("/alerts/email", methods=["POST"])
def email_alert_signup():
    from auth import check_csrf
    check_csrf()
    email = clean_inline_job_text(request.form.get("email", "")).lower()
    category = clean_inline_job_text(request.form.get("category", ""))[:80]
    location = clean_inline_job_text(request.form.get("location", ""))[:80]
    source = clean_inline_job_text(request.form.get("source", "unknown"))[:80]
    frequency = clean_alert_frequency(request.form.get("frequency", "instant"))
    target = safe_form_next(url_for("index"))

    if len(email) > 254 or not EMAIL_RE.match(email):
        flash("Enter a valid email address for job alerts.")
        return redirect(url_with_query_param(target, "email_alert", "error"))

    unsubscribe_token = new_unsubscribe_token()
    db = get_db()
    db.execute(
        """INSERT INTO email_alerts(
                email, category, location, source, frequency, active,
                unsubscribe_token, unsubscribed_at, delivery_failures, last_error
           )
           VALUES(?,?,?,?,?,1,?,NULL,0,NULL)
           ON CONFLICT(email, category, location)
           DO UPDATE SET source=excluded.source,
                         frequency=excluded.frequency,
                         active=1,
                         unsubscribed_at=NULL,
                         last_error=NULL,
                         updated_at=datetime('now')""",
        (email, category, location, source, frequency, unsubscribe_token),
    )
    db.commit()
    send_email_alert_confirmation(email, category, location)
    flash("Email job alerts enabled.")
    return redirect(url_with_query_param(target, "email_alert", "success"))


@app.route("/alerts/email/unsubscribe/<token>")
def email_alert_unsubscribe(token):
    token = clean_inline_job_text(token)
    if len(token) < 20 or len(token) > 128:
        abort(404)
    db = get_db()
    cur = db.execute(
        """UPDATE email_alerts
           SET active=0, unsubscribed_at=datetime('now'), updated_at=datetime('now')
           WHERE unsubscribe_token=? AND active=1""",
        (token,),
    )
    db.commit()
    if cur.rowcount == 0:
        flash("This alert is already inactive or the link is invalid.")
    else:
        flash("Email job alerts unsubscribed.")
    return redirect(url_for("index"))


@app.route("/affiliate/click/<int:offer_id>")
def affiliate_click(offer_id):
    placement_id = limited_arg("placement", "unknown", max_chars=80)
    page_path = limited_arg("page", request.referrer or "", max_chars=240)
    job_category = limited_arg("job_category", "", max_chars=80)
    offer = get_db().execute(
        "SELECT * FROM affiliate_offers WHERE id=? AND is_active=1",
        (offer_id,),
    ).fetchone()
    if not offer:
        abort(404)
    outbound = affiliate_outbound_url(offer, placement_id)
    if not outbound:
        abort(404)
    log_affiliate_event(
        offer_id,
        placement_id,
        "click",
        page_path=page_path,
        job_category=job_category,
    )
    return redirect(outbound)


@app.route("/affiliate/event", methods=["POST"])
def affiliate_event():
    data = request.get_json(silent=True) or {}
    event_type = clean_inline_job_text(data.get("event_type", ""))[:40]
    if event_type != "impression":
        abort(400)
    try:
        offer_id = int(data.get("offer_id"))
    except (TypeError, ValueError):
        abort(400)
    if not get_db().execute(
        "SELECT 1 FROM affiliate_offers WHERE id=? AND is_active=1",
        (offer_id,),
    ).fetchone():
        abort(404)
    log_affiliate_event(
        offer_id,
        clean_inline_job_text(data.get("placement_id", "unknown"))[:80],
        event_type,
        page_path=clean_inline_job_text(data.get("page_path", request.path))[:240],
        job_category=clean_inline_job_text(data.get("job_category", ""))[:80],
    )
    return {"ok": True}


@app.route("/post", methods=["GET", "POST"])
def post():
    """Form for you/recruiters + token-protected API for your scraper."""
    error = None
    if request.method == "POST":
        api_token_valid = valid_admin_token(request.headers.get("X-Admin-Token"))
        if not api_token_valid:
            from auth import check_csrf
            check_csrf()
        f = request.form
        core = ("title", "company", "location", "category",
                "summary", "apply_url")
        cleaned_core = clean_core_job_values(f, core)
        db   = get_db()
        opt  = optional_job_values(f, job_columns(db))
        cleaned_values = {**cleaned_core, **opt}
        validation_error = validate_job_values(cleaned_values, core)
        if not valid_admin_token(f.get("token") or request.headers.get("X-Admin-Token")):
            error = "Invalid admin token."
            log_event(logging.WARNING, "invalid_admin_token", request=request_context_snapshot())
        elif validation_error:
            error = validation_error
            log_event(logging.WARNING, "invalid_job_post", reason=validation_error, request=request_context_snapshot())
        elif form_values_are_closed(f):
            error = "Closed or expired jobs are not published."
        else:
            cols = list(core) + ["featured"] + list(opt.keys())
            vals = [cleaned_core[k] for k in core] + \
                   [1 if f.get("featured") else 0] + list(opt.values())
            cur = db.execute(
                f"INSERT INTO jobs({','.join(cols)}) "
                f"VALUES({','.join('?' * len(cols))})", vals)
            db.commit()
            if not api_token_valid:
                send_job_published_email(cur.lastrowid, cleaned_values)
            return redirect(url_for("index"))
    return render_template("post.html", categories=CATEGORIES,
                           cat=None, q="", error=error,
                           employment_types=EMPLOYMENT_TYPES,
                           remote_options=REMOTE_OPTIONS,
                           experience_levels=EXPERIENCE_LEVELS,
                           employer_offers=select_affiliate_offers(
                               audience="employer",
                               placement="post_job",
                               limit=2,
                           ))


# ------------------------------- SEO --------------------------------
@app.route("/sitemap.xml")
def sitemap():
    db = get_db()
    where = active_jobs_where_sql(job_columns(db), "jobs")
    rows = db.execute(
        f"SELECT * FROM jobs WHERE {where} ORDER BY id"
    ).fetchall()
    active_rows = [row for row in rows if row_is_public_job(row)]
    urls = [
        sitemap_entry(absolute_url("/"), changefreq="daily", priority="1.0"),
        sitemap_entry(absolute_url("/services"), changefreq="monthly", priority="0.5"),
        sitemap_entry(absolute_url("/post"), changefreq="monthly", priority="0.6"),
    ]
    urls.extend(
        sitemap_entry(absolute_url(f"/jobs/{name}/"), changefreq="daily", priority="0.8")
        for name in SEO_LANDING_PAGES
    )
    urls.extend(
        sitemap_entry(absolute_url(f"/resources/{name}"), changefreq="monthly", priority="0.4")
        for name in AFFILIATE_RESOURCE_PAGES
    )
    urls.extend(
        sitemap_entry(
            absolute_url(f"/job/{r['id']}/{slug(r['title'])}"),
            lastmod=row_value(r, "posted_at") or row_value(r, "created_at"),
            changefreq="weekly",
            priority="0.7",
        )
        for r in active_rows
    )
    return Response('<?xml version="1.0" encoding="UTF-8"?>'
                    '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
                    + "".join(urls) + "</urlset>",
                    mimetype="application/xml")


@app.route("/feed.xml")
def feed():
    db = get_db()
    where = active_jobs_where_sql(job_columns(db), "jobs")
    rows = db.execute(
        f"SELECT * FROM jobs WHERE {where} ORDER BY created_at DESC LIMIT 30"
    ).fetchall()
    rows = [row for row in rows if row_is_public_job(row)]
    items = "".join(
        f"<item><title>{escape(r['title'])} — {escape(r['company'])}</title>"
        f"<link>{SITE_URL}/job/{r['id']}/{slug(r['title'])}</link>"
        f"<description>{escape(clean_job_display_text(r['summary']))}</description>"
        f"<guid>{SITE_URL}/job/{r['id']}</guid></item>" for r in rows)
    return Response('<?xml version="1.0" encoding="UTF-8"?>'
                    '<rss version="2.0"><channel>'
                    '<title>ZimJobs Hub — Latest Jobs</title>'
                    f'<link>{SITE_URL}</link>'
                    '<description>Jobs in Zimbabwe and remote work for '
                    'Zimbabweans</description>' + items + '</channel></rss>',
                    mimetype="application/rss+xml")


@app.route("/robots.txt")
def robots():
    body = "\n".join([
        "User-agent: *",
        "Allow: /",
        "Disallow: /admin/",
        "Disallow: /account",
        "Disallow: /login",
        "Disallow: /register",
        "Disallow: /logout",
        "Disallow: /dashboard",
        "Disallow: /api/",
        "Disallow: /internal/",
        "Disallow: /preview/",
        "Disallow: /staging/",
        "Disallow: /affiliate/",
        "Disallow: /alerts/",
        "Disallow: /health",
        "Disallow: /healthz/",
        f"Sitemap: {SITE_URL}/sitemap.xml",
        "",
    ])
    return Response(body, mimetype="text/plain")


@app.route("/sw.js")
def sw():
    resp = app.send_static_file("sw.js")
    resp.headers["Cache-Control"] = "no-cache"
    return resp


@app.route("/health")
def health():
    try:
        jobs_count = get_db().execute("SELECT COUNT(*) AS count FROM jobs").fetchone()["count"]
    except sqlite3.Error as exc:
        log_event(
            logging.ERROR,
            "health_db_error",
            error=error_snapshot(exc),
            request=request_context_snapshot(),
            config=runtime_config_snapshot(),
        )
        jobs_count = None
    return {"status": "ok", "jobs": jobs_count}


@app.route("/healthz/live")
def health_live():
    return {"status": "ok"}


@app.errorhandler(404)
def not_found(_):
    return render_template("404.html", categories=CATEGORIES,
                           cat=None, q=""), 404


def error_page(status_code, title, message):
    return render_template(
        "error.html",
        title=title,
        message=message,
        categories=CATEGORIES,
        cat=None,
        q="",
    ), status_code


@app.errorhandler(400)
def bad_request(_):
    return error_page(400, "Bad request", "The request could not be processed.")


@app.errorhandler(403)
def forbidden(_):
    return error_page(403, "Forbidden", "You do not have permission to access this page.")


@app.errorhandler(429)
def too_many_requests(_):
    return error_page(429, "Too many requests", "Please wait a moment and try again.")


@app.errorhandler(500)
def internal_error(_):
    return error_page(500, "Server error", "Something went wrong. Please try again later.")


@app.errorhandler(Exception)
def handle_unexpected_error(error):
    if isinstance(error, HTTPException):
        if error.code and error.code >= 500:
            log_event(
                logging.ERROR,
                "http_exception",
                error=error_snapshot(error, error.code),
                request=request_context_snapshot(),
                config=runtime_config_snapshot(),
            )
        return error_page(
            error.code or 500,
            error.name or "Request error",
            "The request could not be processed.",
        )

    log_event(
        logging.ERROR,
        "unhandled_request_exception",
        error=error_snapshot(error, 500),
        request=request_context_snapshot(),
        config=runtime_config_snapshot(),
    )
    return error_page(500, "Server error", "Something went wrong. Please try again later.")


log_event(logging.INFO, "app_starting", config=runtime_config_snapshot())
try:
    init_db()
    log_event(logging.INFO, "db_initialized", db_path=DB_PATH)
except Exception as exc:
    log_event(
        logging.CRITICAL,
        "db_init_failed",
        error=error_snapshot(exc),
        config=runtime_config_snapshot(),
    )
    raise

# ---------------------- user & admin modules ------------------------
if __name__ == "__main__":
    sys.modules.setdefault("app", sys.modules[__name__])

from auth import auth_bp, init_auth_db   # noqa: E402
from admin import admin_bp               # noqa: E402

app.register_blueprint(auth_bp)
app.register_blueprint(admin_bp)
try:
    init_auth_db()
    log_event(logging.INFO, "auth_db_initialized", db_path=DB_PATH)
except Exception as exc:
    log_event(
        logging.CRITICAL,
        "auth_db_init_failed",
        error=error_snapshot(exc),
        config=runtime_config_snapshot(),
    )
    raise

if __name__ == "__main__":
    app.run(
        host="0.0.0.0",
        port=int(os.environ.get("PORT", "8000")),
        debug=os.environ.get("FLASK_DEBUG") == "1",
    )
