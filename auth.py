import hashlib, logging, os, re, secrets, sqlite3
from functools import wraps
from flask import (Blueprint, g, request, render_template, redirect,
                   url_for, session, abort, flash)
from werkzeug.security import generate_password_hash, check_password_hash

from app import (get_db, DB_PATH, ADMIN_TOKEN, CATEGORIES,
                 safe_redirect_target, clean_control_chars, log_event,
                 request_context_snapshot)

auth_bp = Blueprint("auth", __name__)

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


# ----------------------------- database -----------------------------
def init_auth_db():
    db = sqlite3.connect(DB_PATH)
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("""CREATE TABLE IF NOT EXISTS users(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        email TEXT NOT NULL UNIQUE,
        name TEXT NOT NULL,
        password_hash TEXT NOT NULL,
        role TEXT NOT NULL DEFAULT 'user',
        is_active INTEGER NOT NULL DEFAULT 1,
        created_at TEXT DEFAULT (datetime('now')))""")
    db.execute("""CREATE TABLE IF NOT EXISTS saved_jobs(
        user_id INTEGER NOT NULL,
        job_id INTEGER NOT NULL,
        created_at TEXT DEFAULT (datetime('now')),
        PRIMARY KEY(user_id, job_id))""")
    # Keep FTS in sync when jobs are edited via the admin portal
    # (existing schema only had INSERT/DELETE triggers).
    db.execute("""CREATE TRIGGER IF NOT EXISTS jobs_au AFTER UPDATE ON jobs BEGIN
        INSERT INTO jobs_fts(jobs_fts,rowid,title,company,summary,location)
        VALUES('delete',old.id,old.title,old.company,old.summary,old.location);
        INSERT INTO jobs_fts(rowid,title,company,summary,location)
        VALUES (new.id,new.title,new.company,new.summary,new.location); END""")
    # Bootstrap an admin account on first run.
    if not db.execute("SELECT 1 FROM users WHERE role='admin'").fetchone():
        db.execute("""INSERT OR IGNORE INTO users(email,name,password_hash,role)
            VALUES(?,?,?,?)""",
            (os.environ.get("ADMIN_EMAIL") or "admin@zimjobs.local",
             "Administrator",
             generate_password_hash(
                 os.environ.get("ADMIN_PASSWORD") or ADMIN_TOKEN),
             "admin"))
    db.commit()
    db.close()


# ----------------------------- helpers ------------------------------
@auth_bp.before_app_request
def load_user():
    g.user = None
    uid = session.get("uid")
    if uid:
        row = get_db().execute(
            "SELECT * FROM users WHERE id=? AND is_active=1", (uid,)).fetchone()
        if row:
            g.user = row
        else:
            session.pop("uid", None)


@auth_bp.app_context_processor
def inject_user():
    return {"current_user": g.get("user")}


@auth_bp.app_template_global("csrf_token")
def csrf_token():
    if "_csrf" not in session:
        session["_csrf"] = secrets.token_hex(16)
    return session["_csrf"]


def check_csrf():
    sent = request.form.get("_csrf") or request.headers.get("X-CSRF-Token") or ""
    expected = session.get("_csrf") or ""
    if not expected or not secrets.compare_digest(sent, expected):
        log_event(logging.WARNING, "csrf_blocked", request=request_context_snapshot())
        abort(400)


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not g.get("user"):
            return redirect(url_for("auth.login", next=request.path))
        return view(*args, **kwargs)
    return wrapped


def admin_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not g.get("user"):
            return redirect(url_for("auth.login", next=request.path))
        if g.user["role"] != "admin":
            abort(403)
        return view(*args, **kwargs)
    return wrapped


@auth_bp.app_template_global("is_saved")
def is_saved(job_id):
    u = g.get("user")
    if not u:
        return False
    return bool(get_db().execute(
        "SELECT 1 FROM saved_jobs WHERE user_id=? AND job_id=?",
        (u["id"], job_id)).fetchone())


def _safe_next(nxt):
    return safe_redirect_target(nxt, "")


def _email_hash(email):
    return hashlib.sha256(email.encode("utf-8")).hexdigest()[:12] if email else ""


# ------------------------------ routes ------------------------------
@auth_bp.route("/register", methods=["GET", "POST"])
def register():
    if g.get("user"):
        return redirect(url_for("auth.account"))
    error = None
    if request.method == "POST":
        check_csrf()
        f = request.form
        name  = clean_control_chars(f.get("name", "")).strip()
        email = clean_control_chars(f.get("email", "")).strip().lower()
        pw    = f.get("password", "")
        if not name or not email or not pw:
            error = "All fields are required."
        elif len(name) > 120 or len(email) > 254 or len(pw) > 256:
            error = "One or more fields are too long."
        elif not EMAIL_RE.match(email):
            error = "Enter a valid email address."
        elif len(pw) < 8:
            error = "Password must be at least 8 characters."
        else:
            db = get_db()
            try:
                cur = db.execute(
                    "INSERT INTO users(email,name,password_hash) VALUES(?,?,?)",
                    (email, name, generate_password_hash(pw)))
                db.commit()
                session.clear()
                session.permanent = True
                session["uid"] = cur.lastrowid
                flash("Welcome to ZimJobs Hub!")
                return redirect(url_for("auth.account"))
            except sqlite3.IntegrityError:
                error = "An account with that email already exists."
    return render_template("register.html", categories=CATEGORIES,
                           cat=None, q="", error=error)


@auth_bp.route("/login", methods=["GET", "POST"])
def login():
    if g.get("user"):
        return redirect(url_for("auth.account"))
    error = None
    nxt = _safe_next(request.values.get("next", ""))
    if request.method == "POST":
        check_csrf()
        f = request.form
        email = clean_control_chars(f.get("email", "")).strip().lower()
        row = get_db().execute(
            "SELECT * FROM users WHERE email=?",
            (email,)).fetchone()
        if not row or not check_password_hash(row["password_hash"],
                                              f.get("password", "")):
            error = "Invalid email or password."
            log_event(
                logging.WARNING,
                "login_failed",
                email_hash=_email_hash(email),
                request=request_context_snapshot(),
            )
        elif not row["is_active"]:
            error = "This account has been deactivated."
        else:
            session.clear()
            session.permanent = True
            session["uid"] = row["id"]
            if nxt:
                return redirect(nxt)
            return redirect(url_for("admin.dashboard")
                            if row["role"] == "admin"
                            else url_for("auth.account"))
    return render_template("login.html", categories=CATEGORIES,
                           cat=None, q="", error=error, next=nxt)


@auth_bp.route("/logout", methods=["POST"])
def logout():
    check_csrf()
    session.clear()
    return redirect(url_for("index"))


@auth_bp.route("/account")
@login_required
def account():
    saved = get_db().execute(
        """SELECT jobs.*, saved_jobs.created_at AS saved_at
           FROM saved_jobs JOIN jobs ON jobs.id = saved_jobs.job_id
           WHERE saved_jobs.user_id=?
           ORDER BY saved_jobs.created_at DESC""",
        (g.user["id"],)).fetchall()
    return render_template("account.html", saved=saved,
                           categories=CATEGORIES, cat=None, q="")


@auth_bp.route("/account/update", methods=["POST"])
@login_required
def account_update():
    check_csrf()
    f = request.form
    name  = clean_control_chars(f.get("name", "")).strip()
    email = clean_control_chars(f.get("email", "")).strip().lower()
    if not name or len(name) > 120 or len(email) > 254 or not EMAIL_RE.match(email):
        flash("Enter a valid name and email address.")
    else:
        try:
            db = get_db()
            db.execute("UPDATE users SET name=?, email=? WHERE id=?",
                       (name, email, g.user["id"]))
            db.commit()
            flash("Profile updated.")
        except sqlite3.IntegrityError:
            flash("That email is already in use.")
    return redirect(url_for("auth.account"))


@auth_bp.route("/account/password", methods=["POST"])
@login_required
def account_password():
    check_csrf()
    f = request.form
    if not check_password_hash(g.user["password_hash"], f.get("current", "")):
        flash("Current password is incorrect.")
    elif len(f.get("new", "")) < 8:
        flash("New password must be at least 8 characters.")
    elif len(f.get("new", "")) > 256:
        flash("New password is too long.")
    else:
        db = get_db()
        db.execute("UPDATE users SET password_hash=? WHERE id=?",
                   (generate_password_hash(f["new"]), g.user["id"]))
        db.commit()
        flash("Password changed.")
    return redirect(url_for("auth.account"))


@auth_bp.route("/job/<int:job_id>/save", methods=["POST"])
@login_required
def save_job(job_id):
    check_csrf()
    db = get_db()
    if not db.execute("SELECT 1 FROM jobs WHERE id=?", (job_id,)).fetchone():
        abort(404)
    db.execute("INSERT OR IGNORE INTO saved_jobs(user_id,job_id) VALUES(?,?)",
               (g.user["id"], job_id))
    db.commit()
    return redirect(safe_redirect_target(request.referrer, url_for("job", job_id=job_id)))


@auth_bp.route("/job/<int:job_id>/unsave", methods=["POST"])
@login_required
def unsave_job(job_id):
    check_csrf()
    db = get_db()
    db.execute("DELETE FROM saved_jobs WHERE user_id=? AND job_id=?",
               (g.user["id"], job_id))
    db.commit()
    return redirect(safe_redirect_target(request.referrer, url_for("job", job_id=job_id)))
