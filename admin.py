from math import ceil
from flask import (Blueprint, g, request, render_template, redirect,
                   url_for, abort, flash)

from app import get_db, CATEGORIES, PER_PAGE
from auth import admin_required, check_csrf

admin_bp = Blueprint("admin", __name__, url_prefix="/admin")


# ----------------------------- dashboard ----------------------------
@admin_bp.route("/")
@admin_required
def dashboard():
    db = get_db()
    stats = {
        "jobs":      db.execute("SELECT COUNT(*) c FROM jobs").fetchone()["c"],
        "featured":  db.execute("SELECT COUNT(*) c FROM jobs "
                                "WHERE featured=1").fetchone()["c"],
        "users":     db.execute("SELECT COUNT(*) c FROM users").fetchone()["c"],
        "new_users": db.execute("SELECT COUNT(*) c FROM users WHERE "
                                "created_at >= datetime('now','-7 days')"
                                ).fetchone()["c"],
        "saved":     db.execute("SELECT COUNT(*) c FROM saved_jobs"
                                ).fetchone()["c"],
    }
    by_cat = db.execute("SELECT category, COUNT(*) c FROM jobs "
                        "GROUP BY category ORDER BY c DESC").fetchall()
    recent_users = db.execute("SELECT * FROM users "
                              "ORDER BY created_at DESC LIMIT 5").fetchall()
    return render_template("admin/dashboard.html", stats=stats, by_cat=by_cat,
                           recent_users=recent_users,
                           categories=CATEGORIES, cat=None, q="")


# ------------------------------- jobs -------------------------------
@admin_bp.route("/jobs")
@admin_required
def jobs():
    q    = request.args.get("q", "").strip()
    page = max(int(request.args.get("page", 1) or 1), 1)
    db   = get_db()
    base, args = "FROM jobs WHERE 1=1", []
    if q:
        base += " AND (title LIKE ? OR company LIKE ?)"
        args += [f"%{q}%", f"%{q}%"]
    total = db.execute("SELECT COUNT(*) c " + base, args).fetchone()["c"]
    rows  = db.execute("SELECT * " + base +
                       " ORDER BY created_at DESC LIMIT ? OFFSET ?",
                       args + [PER_PAGE, (page - 1) * PER_PAGE]).fetchall()
    return render_template("admin/jobs.html", jobs=rows, total=total,
                           page=page, pages=max(ceil(total / PER_PAGE), 1),
                           categories=CATEGORIES, cat=None, q=q)


@admin_bp.route("/jobs/new", methods=["GET", "POST"])
@admin_bp.route("/jobs/<int:job_id>/edit", methods=["GET", "POST"])
@admin_required
def job_form(job_id=None):
    db, job, error = get_db(), None, None
    if job_id:
        job = db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        if not job:
            abort(404)
    if request.method == "POST":
        check_csrf()
        f = request.form
        fields = ("title", "company", "location", "category",
                  "summary", "apply_url")
        if not all(f.get(k, "").strip() for k in fields):
            error = "All fields are required."
            job = dict(f)
        else:
            vals = [f[k].strip() for k in fields] + \
                   [1 if f.get("featured") else 0]
            if job_id:
                db.execute("""UPDATE jobs SET title=?,company=?,location=?,
                    category=?,summary=?,apply_url=?,featured=?
                    WHERE id=?""", vals + [job_id])
            else:
                db.execute("""INSERT INTO jobs(title,company,location,category,
                    summary,apply_url,featured) VALUES(?,?,?,?,?,?,?)""", vals)
            db.commit()
            flash("Job saved.")
            return redirect(url_for("admin.jobs"))
    return render_template("admin/job_form.html", job=job, job_id=job_id,
                           error=error, categories=CATEGORIES, cat=None, q="")


@admin_bp.route("/jobs/<int:job_id>/feature", methods=["POST"])
@admin_required
def job_feature(job_id):
    check_csrf()
    db  = get_db()
    row = db.execute("SELECT featured FROM jobs WHERE id=?",
                     (job_id,)).fetchone()
    if not row:
        abort(404)
    db.execute("UPDATE jobs SET featured=? WHERE id=?",
               (0 if row["featured"] else 1, job_id))
    db.commit()
    flash("Job updated.")
    return redirect(request.referrer or url_for("admin.jobs"))


@admin_bp.route("/jobs/<int:job_id>/delete", methods=["POST"])
@admin_required
def job_delete(job_id):
    check_csrf()
    db = get_db()
    db.execute("DELETE FROM saved_jobs WHERE job_id=?", (job_id,))
    db.execute("DELETE FROM jobs WHERE id=?", (job_id,))
    db.commit()
    flash("Job deleted.")
    return redirect(url_for("admin.jobs"))


# ------------------------------- users ------------------------------
@admin_bp.route("/users")
@admin_required
def users():
    q    = request.args.get("q", "").strip()
    page = max(int(request.args.get("page", 1) or 1), 1)
    db   = get_db()
    base, args = "FROM users WHERE 1=1", []
    if q:
        base += " AND (email LIKE ? OR name LIKE ?)"
        args += [f"%{q}%", f"%{q}%"]
    total = db.execute("SELECT COUNT(*) c " + base, args).fetchone()["c"]
    rows  = db.execute("SELECT * " + base +
                       " ORDER BY created_at DESC LIMIT ? OFFSET ?",
                       args + [PER_PAGE, (page - 1) * PER_PAGE]).fetchall()
    return render_template("admin/users.html", users=rows, total=total,
                           page=page, pages=max(ceil(total / PER_PAGE), 1),
                           categories=CATEGORIES, cat=None, q=q)


@admin_bp.route("/users/<int:user_id>/toggle-active", methods=["POST"])
@admin_required
def user_toggle_active(user_id):
    check_csrf()
    if user_id == g.user["id"]:
        flash("You cannot deactivate your own account.")
        return redirect(url_for("admin.users"))
    db  = get_db()
    row = db.execute("SELECT is_active FROM users WHERE id=?",
                     (user_id,)).fetchone()
    if not row:
        abort(404)
    db.execute("UPDATE users SET is_active=? WHERE id=?",
               (0 if row["is_active"] else 1, user_id))
    db.commit()
    flash("User updated.")
    return redirect(request.referrer or url_for("admin.users"))


@admin_bp.route("/users/<int:user_id>/toggle-role", methods=["POST"])
@admin_required
def user_toggle_role(user_id):
    check_csrf()
    if user_id == g.user["id"]:
        flash("You cannot change your own role.")
        return redirect(url_for("admin.users"))
    db  = get_db()
    row = db.execute("SELECT role FROM users WHERE id=?",
                     (user_id,)).fetchone()
    if not row:
        abort(404)
    db.execute("UPDATE users SET role=? WHERE id=?",
               ("user" if row["role"] == "admin" else "admin", user_id))
    db.commit()
    flash("User updated.")
    return redirect(request.referrer or url_for("admin.users"))


@admin_bp.route("/users/<int:user_id>/delete", methods=["POST"])
@admin_required
def user_delete(user_id):
    check_csrf()
    if user_id == g.user["id"]:
        flash("You cannot delete your own account.")
        return redirect(url_for("admin.users"))
    db = get_db()
    db.execute("DELETE FROM saved_jobs WHERE user_id=?", (user_id,))
    db.execute("DELETE FROM users WHERE id=?", (user_id,))
    db.commit()
    flash("User deleted.")
    return redirect(url_for("admin.users"))
