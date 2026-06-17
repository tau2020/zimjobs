from math import ceil
from flask import (Blueprint, g, request, render_template, redirect,
                   url_for, abort, flash)

from app import (get_db, CATEGORIES, PER_PAGE, EMPLOYMENT_TYPES,
                 REMOTE_OPTIONS, EXPERIENCE_LEVELS, job_columns,
                 optional_job_values, form_values_are_closed,
                 clean_core_job_values, limited_arg, page_arg,
                 validate_job_values, safe_redirect_target,
                 request_context_snapshot, log_event, clean_inline_job_text,
                 is_safe_public_url, AFFILIATE_DISCLOSURE)
import logging
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
        "aff_clicks": db.execute("SELECT COUNT(*) c FROM affiliate_events "
                                 "WHERE event_type='click'").fetchone()["c"],
        "aff_impressions": db.execute("SELECT COUNT(*) c FROM affiliate_events "
                                      "WHERE event_type='impression'").fetchone()["c"],
    }
    by_cat = db.execute("SELECT category, COUNT(*) c FROM jobs "
                        "GROUP BY category ORDER BY c DESC").fetchall()
    recent_users = db.execute("SELECT * FROM users "
                              "ORDER BY created_at DESC LIMIT 5").fetchall()
    return render_template("admin/dashboard.html", stats=stats, by_cat=by_cat,
                           recent_users=recent_users,
                           categories=CATEGORIES, cat=None, q="")


# ---------------------------- affiliates ----------------------------
AFFILIATE_FIELDS = (
    "name", "offer_category", "audience_type", "job_category_targets",
    "placement_locations", "affiliate_url", "display_title", "description",
    "cta_text", "image_url", "disclosure_text", "priority_score", "tracking_id",
)


def affiliate_form_values(form):
    values = {}
    for field in AFFILIATE_FIELDS:
        value = clean_inline_job_text(form.get(field, ""))
        if field == "description":
            value = value[:500]
        elif field == "affiliate_url":
            value = value[:2048]
        else:
            value = value[:240]
        values[field] = value
    values["is_active"] = 1 if form.get("is_active") else 0
    try:
        values["priority_score"] = int(values["priority_score"] or 0)
    except ValueError:
        values["priority_score"] = 0
    return values


def validate_affiliate_offer(values):
    required = (
        "name", "offer_category", "audience_type", "placement_locations",
        "affiliate_url", "display_title", "description", "cta_text",
    )
    if any(not values.get(field) for field in required):
        return "All required offer fields must be filled."
    if not is_safe_public_url(values["affiliate_url"]):
        return "Affiliate URL must be a valid http or https URL."
    if values.get("image_url") and not is_safe_public_url(values["image_url"]):
        return "Image URL must be a valid http or https URL."
    return None


@admin_bp.route("/affiliates")
@admin_required
def affiliates():
    db = get_db()
    rows = db.execute(
        """SELECT o.*,
            SUM(CASE WHEN e.event_type='impression' THEN 1 ELSE 0 END) impressions,
            SUM(CASE WHEN e.event_type='click' THEN 1 ELSE 0 END) clicks
           FROM affiliate_offers o
           LEFT JOIN affiliate_events e ON e.offer_id=o.id
           GROUP BY o.id
           ORDER BY o.is_active DESC, o.priority_score DESC, o.updated_at DESC"""
    ).fetchall()
    return render_template("admin/affiliates.html", offers=rows,
                           categories=CATEGORIES, cat=None, q="")


@admin_bp.route("/affiliates/new", methods=["GET", "POST"])
@admin_bp.route("/affiliates/<int:offer_id>/edit", methods=["GET", "POST"])
@admin_required
def affiliate_form(offer_id=None):
    db, offer, error = get_db(), None, None
    if offer_id:
        offer = db.execute(
            "SELECT * FROM affiliate_offers WHERE id=?", (offer_id,)
        ).fetchone()
        if not offer:
            abort(404)
    if request.method == "POST":
        check_csrf()
        values = affiliate_form_values(request.form)
        error = validate_affiliate_offer(values)
        if error:
            offer = values
        elif offer_id:
            set_cols = list(AFFILIATE_FIELDS) + ["is_active"]
            db.execute(
                "UPDATE affiliate_offers SET " +
                ",".join(f"{col}=?" for col in set_cols) +
                ",updated_at=datetime('now') WHERE id=?",
                [values[col] for col in set_cols] + [offer_id],
            )
            db.commit()
            flash("Affiliate offer updated.")
            return redirect(url_for("admin.affiliates"))
        else:
            cols = list(AFFILIATE_FIELDS) + ["is_active"]
            db.execute(
                f"INSERT INTO affiliate_offers({','.join(cols)}) "
                f"VALUES({','.join('?' * len(cols))})",
                [values[col] for col in cols],
            )
            db.commit()
            flash("Affiliate offer created.")
            return redirect(url_for("admin.affiliates"))
    defaults = {"disclosure_text": AFFILIATE_DISCLOSURE, "priority_score": 50,
                "is_active": 1}
    return render_template("admin/affiliate_form.html",
                           offer=offer or defaults,
                           offer_id=offer_id, error=error,
                           categories=CATEGORIES, cat=None, q="")


@admin_bp.route("/affiliates/<int:offer_id>/delete", methods=["POST"])
@admin_required
def affiliate_delete(offer_id):
    check_csrf()
    db = get_db()
    db.execute("DELETE FROM affiliate_events WHERE offer_id=?", (offer_id,))
    db.execute("DELETE FROM affiliate_offers WHERE id=?", (offer_id,))
    db.commit()
    flash("Affiliate offer deleted.")
    return redirect(url_for("admin.affiliates"))


@admin_bp.route("/affiliate-report")
@admin_required
def affiliate_report():
    db = get_db()
    totals = db.execute(
        """SELECT
            SUM(CASE WHEN event_type='impression' THEN 1 ELSE 0 END) impressions,
            SUM(CASE WHEN event_type='click' THEN 1 ELSE 0 END) clicks
           FROM affiliate_events"""
    ).fetchone()
    by_offer = db.execute(
        """SELECT o.display_title, o.offer_category,
            SUM(CASE WHEN e.event_type='impression' THEN 1 ELSE 0 END) impressions,
            SUM(CASE WHEN e.event_type='click' THEN 1 ELSE 0 END) clicks
           FROM affiliate_offers o
           LEFT JOIN affiliate_events e ON e.offer_id=o.id
           GROUP BY o.id
           ORDER BY clicks DESC, impressions DESC, o.priority_score DESC"""
    ).fetchall()
    by_placement = db.execute(
        """SELECT placement_id,
            SUM(CASE WHEN event_type='impression' THEN 1 ELSE 0 END) impressions,
            SUM(CASE WHEN event_type='click' THEN 1 ELSE 0 END) clicks
           FROM affiliate_events
           GROUP BY placement_id
           ORDER BY clicks DESC, impressions DESC"""
    ).fetchall()
    recent = db.execute(
        """SELECT e.*, o.display_title
           FROM affiliate_events e
           JOIN affiliate_offers o ON o.id=e.offer_id
           ORDER BY e.created_at DESC LIMIT 50"""
    ).fetchall()
    return render_template("admin/affiliate_report.html", totals=totals,
                           by_offer=by_offer, by_placement=by_placement,
                           recent=recent, categories=CATEGORIES, cat=None, q="")


# ------------------------------- jobs -------------------------------
@admin_bp.route("/jobs")
@admin_required
def jobs():
    q    = limited_arg("q", max_chars=80)
    page = page_arg()
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
        cleaned_core = clean_core_job_values(f, fields)
        opt = optional_job_values(f, job_columns(db))
        cleaned_values = {**cleaned_core, **opt}
        validation_error = validate_job_values(cleaned_values, fields)
        if validation_error:
            error = validation_error
            job = dict(f)
            log_event(logging.WARNING, "invalid_admin_job_form", reason=validation_error, request=request_context_snapshot())
        elif form_values_are_closed(f):
            error = "Closed or expired jobs are not saved."
            job = dict(f)
        else:
            if job_id:
                set_cols = list(fields) + ["featured"] + list(opt.keys())
                vals = [cleaned_core[k] for k in fields] + \
                       [1 if f.get("featured") else 0] + list(opt.values())
                db.execute(
                    "UPDATE jobs SET " +
                    ",".join(f"{c}=?" for c in set_cols) +
                    " WHERE id=?", vals + [job_id])
            else:
                cols = list(fields) + ["featured"] + list(opt.keys())
                vals = [cleaned_core[k] for k in fields] + \
                       [1 if f.get("featured") else 0] + list(opt.values())
                db.execute(
                    f"INSERT INTO jobs({','.join(cols)}) "
                    f"VALUES({','.join('?' * len(cols))})", vals)
            db.commit()
            flash("Job saved.")
            return redirect(url_for("admin.jobs"))
    return render_template("admin/job_form.html", job=job, job_id=job_id,
                           error=error, categories=CATEGORIES, cat=None, q="",
                           employment_types=EMPLOYMENT_TYPES,
                           remote_options=REMOTE_OPTIONS,
                           experience_levels=EXPERIENCE_LEVELS)


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
    return redirect(safe_redirect_target(request.referrer, url_for("admin.jobs")))


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
    q    = limited_arg("q", max_chars=80)
    page = page_arg()
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
    return redirect(safe_redirect_target(request.referrer, url_for("admin.users")))


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
    return redirect(safe_redirect_target(request.referrer, url_for("admin.users")))


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
