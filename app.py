import os, re, sqlite3
from datetime import datetime, timezone
from math import ceil
from xml.sax.saxutils import escape
from flask import (Flask, g, request, render_template, abort,
                   Response, redirect, url_for)
from flask_compress import Compress

DB_PATH     = os.environ.get("DB_PATH", "/data/jobs.db")
ADMIN_TOKEN = os.environ.get("ADMIN_TOKEN", "change-me")
SITE_URL    = os.environ.get("SITE_URL", "http://localhost:8000").rstrip("/")
PER_PAGE    = 20

CATEGORIES = ["NGO & Development", "Government", "Private Sector",
              "Remote & International", "Internships", "Gigs"]

# Optional, nullable columns added on top of the original schema. Names mirror
# the scraper's OPTIONAL_COLUMNS_SQL so scraped metadata lands in real columns
# instead of being appended to the summary text. All are additive and safe.
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

SIMILAR_LIMIT = 4

app = Flask(__name__)
app.config["SEND_FILE_MAX_AGE_DEFAULT"] = 604800   # 7-day static cache
Compress(app)                                       # gzip/brotli all responses


# ----------------------------- database -----------------------------
def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
    return g.db


@app.teardown_appcontext
def close_db(_=None):
    db = g.pop("db", None)
    if db:
        db.close()


def init_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
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

    if db.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 0:
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


# ----------------------------- helpers ------------------------------
@app.template_filter("slug")
def slug(text):
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


def job_columns(db=None):
    db = db or get_db()
    return {r[1] for r in db.execute("PRAGMA table_info(jobs)").fetchall()}


def optional_job_values(form, cols):
    """Map optional job columns present in both the form and the schema."""
    out = {}
    for col in OPTIONAL_JOB_COLUMNS:
        if col in cols and col in form:
            out[col] = form.get(col, "").strip() or None
    return out


def distinct_values(db, column):
    if column not in job_columns(db):
        return []
    rows = db.execute(
        f"SELECT DISTINCT {column} v FROM jobs "
        f"WHERE {column} IS NOT NULL AND TRIM({column}) <> '' ORDER BY v").fetchall()
    return [r["v"] for r in rows]


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


STOP_WORDS = {
    "and", "are", "but", "can", "for", "from", "has", "have", "hire",
    "job", "jobs", "must", "not", "our", "the", "this", "via", "with",
    "work", "will", "you", "your", "years", "required", "requireds",
    "experience", "skills", "strong", "apply", "official", "site",
}


def row_value(row, key, default=""):
    """Read optional sqlite.Row columns without assuming every DB is migrated."""
    return row[key] if key in row.keys() and row[key] is not None else default


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
        if is_expired_job(candidate):
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
    q     = request.args.get("q", "").strip()
    cat   = request.args.get("cat", "").strip()
    jtype = request.args.get("type", "").strip()
    work  = request.args.get("remote", "").strip()
    exp   = request.args.get("exp", "").strip()
    loc   = request.args.get("loc", "").strip()
    sort  = request.args.get("sort", "").strip()
    if sort not in SORT_OPTIONS:
        sort = "featured"
    page  = max(int(request.args.get("page", 1) or 1), 1)
    db    = get_db()
    cols  = job_columns(db)

    if q and fts_match(q):
        base = ("FROM jobs JOIN jobs_fts ON jobs.id = jobs_fts.rowid "
                "WHERE jobs_fts MATCH ?")
        args = [fts_match(q)]
    else:
        base, args = "FROM jobs WHERE 1=1", []
    if cat:
        base += " AND category = ?"
        args.append(cat)
    if jtype and "employment_type" in cols:
        base += " AND employment_type = ?"
        args.append(jtype)
    if work and "remote_status" in cols:
        base += " AND remote_status = ?"
        args.append(work)
    if exp and "experience_level" in cols:
        base += " AND experience_level = ?"
        args.append(exp)
    if loc:
        base += " AND location LIKE ?"
        args.append(f"%{loc}%")

    if sort == "newest":
        order = "created_at DESC"
    elif sort == "deadline" and "expires_at" in cols:
        order = ("(expires_at IS NULL OR TRIM(expires_at)='') ASC, "
                 "expires_at ASC, created_at DESC")
    else:
        order = "featured DESC, created_at DESC"

    try:
        total = db.execute("SELECT COUNT(*) c " + base, args).fetchone()["c"]
        jobs  = db.execute(
            "SELECT jobs.* " + base +
            f" ORDER BY {order} LIMIT ? OFFSET ?",
            args + [PER_PAGE, (page - 1) * PER_PAGE]).fetchall()
    except sqlite3.OperationalError:
        total, jobs = 0, []

    filters = {"q": q, "cat": cat, "type": jtype, "remote": work,
               "exp": exp, "loc": loc, "sort": sort}
    active = any(v for k, v in filters.items()
                 if k not in ("sort",) and v) or sort != "featured"
    return render_template(
        "index.html", jobs=jobs, q=q, cat=cat, categories=CATEGORIES,
        page=page, total=total, pages=max(ceil(total / PER_PAGE), 1),
        filters=filters, active_filters=active, sort=sort,
        sort_options=SORT_OPTIONS,
        type_options=distinct_values(db, "employment_type") or [],
        remote_options=distinct_values(db, "remote_status") or [],
        exp_options=distinct_values(db, "experience_level") or [])


@app.route("/job/<int:job_id>")
@app.route("/job/<int:job_id>/<s>")
def job(job_id, s=None):
    db  = get_db()
    row = db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
    if not row:
        abort(404)
    url = f"{SITE_URL}/job/{row['id']}/{slug(row['title'])}"
    similar = similar_jobs(db, row)
    return render_template("job.html", job=row, url=url, similar=similar,
                           categories=CATEGORIES, cat=None, q="")


@app.route("/services")
def services():
    return render_template("services.html", categories=CATEGORIES,
                           cat=None, q="")


@app.route("/post", methods=["GET", "POST"])
def post():
    """Form for you/recruiters + token-protected API for your scraper."""
    error = None
    if request.method == "POST":
        f = request.form
        if f.get("token") != ADMIN_TOKEN:
            error = "Invalid admin token."
        elif not all(f.get(k, "").strip() for k in
                     ("title", "company", "location", "category",
                      "summary", "apply_url")):
            error = "All fields are required."
        else:
            db   = get_db()
            core = ("title", "company", "location", "category",
                    "summary", "apply_url")
            opt  = optional_job_values(f, job_columns(db))
            cols = list(core) + ["featured"] + list(opt.keys())
            vals = [f[k].strip() for k in core] + \
                   [1 if f.get("featured") else 0] + list(opt.values())
            db.execute(
                f"INSERT INTO jobs({','.join(cols)}) "
                f"VALUES({','.join('?' * len(cols))})", vals)
            db.commit()
            return redirect(url_for("index"))
    return render_template("post.html", categories=CATEGORIES,
                           cat=None, q="", error=error,
                           employment_types=EMPLOYMENT_TYPES,
                           remote_options=REMOTE_OPTIONS,
                           experience_levels=EXPERIENCE_LEVELS)


# ------------------------------- SEO --------------------------------
@app.route("/sitemap.xml")
def sitemap():
    rows = get_db().execute(
        "SELECT id,title,created_at FROM jobs ORDER BY id").fetchall()
    urls = [f"<url><loc>{SITE_URL}/</loc></url>"] + [
        f"<url><loc>{SITE_URL}/job/{r['id']}/{slug(r['title'])}</loc>"
        f"<lastmod>{r['created_at'][:10]}</lastmod></url>" for r in rows]
    return Response('<?xml version="1.0" encoding="UTF-8"?>'
                    '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
                    + "".join(urls) + "</urlset>",
                    mimetype="application/xml")


@app.route("/feed.xml")
def feed():
    rows = get_db().execute(
        "SELECT * FROM jobs ORDER BY created_at DESC LIMIT 30").fetchall()
    items = "".join(
        f"<item><title>{escape(r['title'])} — {escape(r['company'])}</title>"
        f"<link>{SITE_URL}/job/{r['id']}/{slug(r['title'])}</link>"
        f"<description>{escape(r['summary'])}</description>"
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
    return Response(f"User-agent: *\nAllow: /\nSitemap: {SITE_URL}/sitemap.xml",
                    mimetype="text/plain")


@app.route("/sw.js")
def sw():
    resp = app.send_static_file("sw.js")
    resp.headers["Cache-Control"] = "no-cache"
    return resp


@app.route("/health")
def health():
    try:
        jobs_count = get_db().execute("SELECT COUNT(*) AS count FROM jobs").fetchone()["count"]
    except sqlite3.Error:
        jobs_count = None
    return {"status": "ok", "jobs": jobs_count}


@app.errorhandler(404)
def not_found(_):
    return render_template("404.html", categories=CATEGORIES,
                           cat=None, q=""), 404


init_db()

# ---------------------- user & admin modules ------------------------
import os as _os
app.secret_key = _os.environ.get("SECRET_KEY") or ADMIN_TOKEN

from auth import auth_bp, init_auth_db   # noqa: E402
from admin import admin_bp               # noqa: E402

app.register_blueprint(auth_bp)
app.register_blueprint(admin_bp)
init_auth_db()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000, debug=True)
