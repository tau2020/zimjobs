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


# ------------------------------ routes ------------------------------
@app.route("/")
def index():
    q    = request.args.get("q", "").strip()
    cat  = request.args.get("cat", "").strip()
    page = max(int(request.args.get("page", 1) or 1), 1)
    db   = get_db()

    if q and fts_match(q):
        base = ("FROM jobs JOIN jobs_fts ON jobs.id = jobs_fts.rowid "
                "WHERE jobs_fts MATCH ?")
        args = [fts_match(q)]
    else:
        base, args = "FROM jobs WHERE 1=1", []
    if cat:
        base += " AND category = ?"
        args.append(cat)

    try:
        total = db.execute("SELECT COUNT(*) c " + base, args).fetchone()["c"]
        jobs  = db.execute(
            "SELECT jobs.* " + base +
            " ORDER BY featured DESC, created_at DESC LIMIT ? OFFSET ?",
            args + [PER_PAGE, (page - 1) * PER_PAGE]).fetchall()
    except sqlite3.OperationalError:
        total, jobs = 0, []

    return render_template("index.html", jobs=jobs, q=q, cat=cat,
                           categories=CATEGORIES, page=page, total=total,
                           pages=max(ceil(total / PER_PAGE), 1))


@app.route("/job/<int:job_id>")
@app.route("/job/<int:job_id>/<s>")
def job(job_id, s=None):
    row = get_db().execute("SELECT * FROM jobs WHERE id=?",
                           (job_id,)).fetchone()
    if not row:
        abort(404)
    url = f"{SITE_URL}/job/{row['id']}/{slug(row['title'])}"
    return render_template("job.html", job=row, url=url,
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
            db = get_db()
            db.execute("""INSERT INTO jobs(title,company,location,category,
                summary,apply_url,featured) VALUES(?,?,?,?,?,?,?)""",
                (f["title"].strip(), f["company"].strip(),
                 f["location"].strip(), f["category"], f["summary"].strip(),
                 f["apply_url"].strip(), 1 if f.get("featured") else 0))
            db.commit()
            return redirect(url_for("index"))
    return render_template("post.html", categories=CATEGORIES,
                           cat=None, q="", error=error)


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
    return {"status": "ok"}


@app.errorhandler(404)
def not_found(_):
    return render_template("404.html", categories=CATEGORIES,
                           cat=None, q=""), 404


init_db()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000, debug=True)
