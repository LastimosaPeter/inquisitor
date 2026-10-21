from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import sqlite3
import tempfile
import threading
import time
import zipfile
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher
from pathlib import Path

from flask import Blueprint, abort, flash, jsonify, make_response, redirect, render_template, request, send_file, session, url_for
from werkzeug.security import generate_password_hash

bp = Blueprint("v1", __name__)

CONTEST_SCOPES = [
    ("department", "Department"),
    ("college", "College"),
    ("cross_college", "Cross-College / University"),
    ("city", "City"),
    ("regional", "Regional"),
    ("national", "National"),
    ("international", "International"),
]
SCORING_MODELS = [
    ("icpc", "ICPC (solved + penalty)"),
    ("points", "Points"),
    ("custom", "Custom points"),
]
LIFECYCLE_PHASES = [
    ("draft", "Draft"),
    ("registration", "Registration Open"),
    ("registration_closed", "Registration Closed"),
    ("practice", "Practice"),
    ("scheduled", "Scheduled"),
    ("running", "Running"),
    ("paused", "Paused"),
    ("ended", "Ended"),
    ("finalized", "Finalized"),
    ("archived", "Archived"),
]
REGISTRATION_STATUSES = ["REGISTERED", "PENDING", "CHECKED_IN", "ACTIVE", "WITHDRAWN", "DISQUALIFIED", "DNS"]
STAFF_ROLES = {
    "contest_director": "Contest Director",
    "chief_judge": "Chief Judge",
    "judge": "Judge",
    "problem_setter": "Problem Setter",
    "technical_admin": "Technical Admin",
    "registration_staff": "Registration Staff",
    "spectator": "Spectator",
}
ROLE_PERMISSIONS = {
    "contest_director": {"all"},
    "chief_judge": {"judge", "clarifications", "rejudge", "problems", "announcements", "awards", "dq", "analytics", "emergency"},
    "judge": {"judge", "clarifications", "rejudge", "dq"},
    "problem_setter": {"problems"},
    "technical_admin": {"judge", "network", "backups", "api", "emergency", "announcements"},
    "registration_staff": {"participants", "checkin", "credentials"},
    "spectator": {"public"},
}


def _core():
    # Imported lazily so app.py can import this module without a circular import.
    import app as core
    return core


def _has_column(conn: sqlite3.Connection, table: str, column: str) -> bool:
    return column in {row[1] for row in conn.execute(f'PRAGMA table_info("{table}")')}


def _add_column(conn: sqlite3.Connection, table: str, definition: str) -> None:
    column = definition.split()[0]
    if not _has_column(conn, table, column):
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {definition}")


def ensure_schema(conn: sqlite3.Connection) -> None:
    # Contest configuration / lifecycle.
    for definition in [
        "scope TEXT NOT NULL DEFAULT 'department'",
        "scoring_model TEXT NOT NULL DEFAULT 'icpc'",
        "lifecycle_phase TEXT NOT NULL DEFAULT 'draft'",
        "registration_start TEXT",
        "registration_end TEXT",
        "eligibility_note TEXT NOT NULL DEFAULT ''",
        "allowed_departments TEXT NOT NULL DEFAULT ''",
        "allowed_colleges TEXT NOT NULL DEFAULT ''",
        "allowed_institutions TEXT NOT NULL DEFAULT ''",
        "allowed_year_levels TEXT NOT NULL DEFAULT ''",
        "max_team_size INTEGER NOT NULL DEFAULT 3",
        "requires_approval INTEGER NOT NULL DEFAULT 0",
        "require_checkin INTEGER NOT NULL DEFAULT 0",
        "custom_scoring_note TEXT NOT NULL DEFAULT ''",
        "submissions_open INTEGER NOT NULL DEFAULT 1",
        "allowed_emails TEXT NOT NULL DEFAULT ''",
        "open_entry INTEGER NOT NULL DEFAULT 0",
        "paused_at TEXT",
        "pause_accumulated_seconds INTEGER NOT NULL DEFAULT 0",
        "practice_mode INTEGER NOT NULL DEFAULT 0",
        "api_enabled INTEGER NOT NULL DEFAULT 1",
        "api_public INTEGER NOT NULL DEFAULT 1",
        "api_token_hash TEXT NOT NULL DEFAULT ''",
        "emergency_message TEXT NOT NULL DEFAULT ''",
        "finalized_at TEXT",
        "archived INTEGER NOT NULL DEFAULT 0",
        "cloned_from TEXT",
        "registration_instruction TEXT NOT NULL DEFAULT ''",
        "rules_text TEXT NOT NULL DEFAULT ''",
        "organizer_text TEXT NOT NULL DEFAULT 'TACTICS x Department of Computer Science, Ateneo de Naga University'",
        "projector_autoscroll INTEGER NOT NULL DEFAULT 0",
    ]:
        _add_column(conn, "contests", definition)

    for definition in [
        "college TEXT NOT NULL DEFAULT ''",
        "year_level TEXT NOT NULL DEFAULT ''",
        "institution_id INTEGER",
        "email TEXT NOT NULL DEFAULT ''",
    ]:
        _add_column(conn, "accounts", definition)
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS ix_accounts_email_unique ON accounts(email) WHERE email <> ''")

    for definition in [
        "registration_status TEXT NOT NULL DEFAULT 'REGISTERED'",
        "approval_status TEXT NOT NULL DEFAULT 'APPROVED'",
        "checked_in_at TEXT",
        "seat TEXT NOT NULL DEFAULT ''",
        "workstation TEXT NOT NULL DEFAULT ''",
        "coach_name TEXT NOT NULL DEFAULT ''",
        "adviser_name TEXT NOT NULL DEFAULT ''",
        "notes TEXT NOT NULL DEFAULT ''",
        "temporary_password INTEGER NOT NULL DEFAULT 0",
        "withdrawn_at TEXT",
    ]:
        _add_column(conn, "registrations", definition)

    for definition in [
        "difficulty TEXT NOT NULL DEFAULT 'Medium'",
        "tags TEXT NOT NULL DEFAULT ''",
        "source TEXT NOT NULL DEFAULT ''",
        "internal_notes TEXT NOT NULL DEFAULT ''",
        "points REAL NOT NULL DEFAULT 100",
        "enabled INTEGER NOT NULL DEFAULT 1",
        "reference_solution TEXT NOT NULL DEFAULT ''",
        "reference_language TEXT NOT NULL DEFAULT ''",
        "validator_source TEXT NOT NULL DEFAULT ''",
        "package_format TEXT NOT NULL DEFAULT 'inquisitor'",
    ]:
        _add_column(conn, "problems", definition)

    # Manual code review: one judge claims a submission, reviews the source, and may override the auto verdict.
    for definition in [
        "claimed_by INTEGER",
        "claimed_at TEXT",
        "reviewed_by INTEGER",
        "reviewed_at TEXT",
        "review_note TEXT NOT NULL DEFAULT ''",
        "manual_override INTEGER NOT NULL DEFAULT 0",
        "auto_verdict TEXT",
        "feedback TEXT NOT NULL DEFAULT ''",
        "feedback_by INTEGER",
        "feedback_at TEXT",
    ]:
        _add_column(conn, "submissions", definition)
    import diagnostics
    diagnostics.ensure_tables(conn)

    # Google sign-in: the stable Google account id (sub) of a student who signed in with ADNU Google.
    _add_column(conn, "accounts", "google_sub TEXT")
    _add_column(conn, "accounts", "alt_email TEXT")   # a second address that signs in as the same (staff) account
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS ix_accounts_google_sub ON accounts(google_sub) WHERE google_sub IS NOT NULL")

    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS institutions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE,
            short_name TEXT NOT NULL DEFAULT '',
            institution_type TEXT NOT NULL DEFAULT 'University',
            city TEXT NOT NULL DEFAULT '',
            region TEXT NOT NULL DEFAULT '',
            country TEXT NOT NULL DEFAULT 'Philippines',
            active INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS staff_assignments (
            contest_id TEXT NOT NULL REFERENCES contests(id) ON DELETE CASCADE,
            account_id INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
            staff_role TEXT NOT NULL,
            created_at TEXT NOT NULL,
            PRIMARY KEY(contest_id, account_id)
        );

        CREATE TABLE IF NOT EXISTS announcements (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            contest_id TEXT NOT NULL REFERENCES contests(id) ON DELETE CASCADE,
            title TEXT NOT NULL,
            body TEXT NOT NULL,
            severity TEXT NOT NULL DEFAULT 'info',
            audience TEXT NOT NULL DEFAULT 'all',
            pinned INTEGER NOT NULL DEFAULT 0,
            created_by INTEGER,
            created_at TEXT NOT NULL,
            expires_at TEXT
        );

        CREATE TABLE IF NOT EXISTS audit_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            contest_id TEXT,
            actor_id INTEGER,
            actor_name TEXT NOT NULL DEFAULT '',
            action TEXT NOT NULL,
            object_type TEXT NOT NULL DEFAULT '',
            object_id TEXT NOT NULL DEFAULT '',
            detail TEXT NOT NULL DEFAULT '',
            ip_address TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS ix_audit_contest_time ON audit_log(contest_id, created_at DESC);

        CREATE TABLE IF NOT EXISTS disqualification_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            contest_id TEXT NOT NULL,
            account_id INTEGER NOT NULL,
            team_name TEXT NOT NULL DEFAULT '',
            action TEXT NOT NULL,
            reason TEXT NOT NULL DEFAULT '',
            evidence_notes TEXT NOT NULL DEFAULT '',
            actor_id INTEGER,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS problem_validations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            problem_id INTEGER NOT NULL REFERENCES problems(id) ON DELETE CASCADE,
            status TEXT NOT NULL,
            report_json TEXT NOT NULL,
            validated_by INTEGER,
            validated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS similarity_flags (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            contest_id TEXT NOT NULL,
            submission_a INTEGER NOT NULL,
            submission_b INTEGER NOT NULL,
            similarity REAL NOT NULL,
            status TEXT NOT NULL DEFAULT 'FLAGGED',
            reviewer_id INTEGER,
            note TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            UNIQUE(contest_id, submission_a, submission_b)
        );

        CREATE TABLE IF NOT EXISTS balloon_queue (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            contest_id TEXT NOT NULL,
            submission_id INTEGER NOT NULL UNIQUE,
            problem_id INTEGER NOT NULL,
            account_id INTEGER NOT NULL,
            team_name TEXT NOT NULL DEFAULT '',
            first_to_solve INTEGER NOT NULL DEFAULT 0,
            status TEXT NOT NULL DEFAULT 'PENDING',
            claimed_by INTEGER,
            delivered_at TEXT,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS awards (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            contest_id TEXT NOT NULL REFERENCES contests(id) ON DELETE CASCADE,
            name TEXT NOT NULL,
            award_type TEXT NOT NULL DEFAULT 'manual',
            rule_json TEXT NOT NULL DEFAULT '{}',
            ordinal INTEGER NOT NULL DEFAULT 1,
            active INTEGER NOT NULL DEFAULT 1
        );

        CREATE TABLE IF NOT EXISTS award_recipients (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            award_id INTEGER NOT NULL REFERENCES awards(id) ON DELETE CASCADE,
            entity_id TEXT NOT NULL,
            entity_name TEXT NOT NULL,
            detail TEXT NOT NULL DEFAULT '',
            computed_at TEXT NOT NULL,
            UNIQUE(award_id, entity_id)
        );

        CREATE TABLE IF NOT EXISTS backups (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            contest_id TEXT,
            filename TEXT NOT NULL,
            size_bytes INTEGER NOT NULL DEFAULT 0,
            reason TEXT NOT NULL DEFAULT 'automatic',
            created_by INTEGER,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS reveal_queue (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            contest_id TEXT NOT NULL,
            entity_id TEXT NOT NULL,
            entity_name TEXT NOT NULL,
            original_rank INTEGER,
            pending_count INTEGER NOT NULL DEFAULT 0,
            reveal_order INTEGER NOT NULL,
            revealed INTEGER NOT NULL DEFAULT 0,
            revealed_at TEXT,
            UNIQUE(contest_id, entity_id)
        );

        CREATE TABLE IF NOT EXISTS contest_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            contest_id TEXT NOT NULL,
            snapshot_type TEXT NOT NULL,
            data_json TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS system_meta (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
        """
    )

    migrated = conn.execute("SELECT value FROM system_meta WHERE key='v1_schema_migrated'").fetchone()
    if not migrated:
        conn.execute("UPDATE contests SET lifecycle_phase='running',scope='cross_college',scoring_model='icpc' WHERE id='acpc-demo' AND lifecycle_phase='draft'")
        conn.execute("INSERT OR REPLACE INTO system_meta(key,value) VALUES('v1_schema_migrated','1')")

    # Seed the built-in institution if absent and connect existing ADNU accounts when possible.
    now = datetime.now(timezone.utc).isoformat(timespec="milliseconds")
    conn.execute(
        "INSERT OR IGNORE INTO institutions(name,short_name,institution_type,city,region,country,created_at) VALUES(?,?,?,?,?,?,?)",
        ("Ateneo de Naga University", "ADNU", "University", "Naga City", "Bicol Region", "Philippines", now),
    )
    iid = conn.execute("SELECT id FROM institutions WHERE name='Ateneo de Naga University'").fetchone()[0]
    conn.execute("UPDATE accounts SET institution_id=? WHERE institution_id IS NULL AND affiliation='Ateneo de Naga University'", (iid,))
    conn.commit()


def audit(conn, action: str, *, contest_id: str | None = None, object_type: str = "", object_id: str = "", detail: str = "", actor=None) -> None:
    core = _core()
    if actor is None:
        try:
            actor = core.current_user()
        except Exception:
            actor = None
    actor_id = actor["id"] if actor else None
    actor_name = actor["display_name"] if actor else "System"
    ip = request.remote_addr if request else ""
    conn.execute(
        "INSERT INTO audit_log(contest_id,actor_id,actor_name,action,object_type,object_id,detail,ip_address,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
        (contest_id, actor_id, actor_name, action, object_type, str(object_id or ""), detail[:4000], ip or "", core.iso(core.utcnow())),
    )


def _admin_required(fn):
    from functools import wraps
    @wraps(fn)
    def wrapped(*args, **kwargs):
        core = _core(); me = core.current_user()
        if not me:
            return redirect(url_for("login", next=request.path))
        if me["role"] != "admin":
            abort(403)
        return fn(*args, **kwargs)
    return wrapped


def _staff_permission(permission: str):
    from functools import wraps
    def deco(fn):
        @wraps(fn)
        def wrapped(*args, **kwargs):
            core = _core(); me = core.current_user()
            if not me:
                return redirect(url_for("login", next=request.path))
            if me["role"] == "admin":
                return fn(*args, **kwargs)
            contest = core.active_contest()
            if not contest:
                abort(403)
            row = core.get_db().execute(
                "SELECT staff_role FROM staff_assignments WHERE contest_id=? AND account_id=?",
                (contest["id"], me["id"]),
            ).fetchone()
            role = row["staff_role"] if row else ("judge" if me["role"] == "judge" else "")
            perms = ROLE_PERMISSIONS.get(role, set())
            if "all" not in perms and permission not in perms:
                abort(403)
            return fn(*args, **kwargs)
        return wrapped
    return deco


def unusable_password_hash() -> str:
    """Hash of a random 256-bit secret nobody knows, for accounts that only sign in with Google. Cheap on purpose:
    the secret cannot be guessed, so the default (memory-hungry) password hash would only waste CPU at registration time."""
    return generate_password_hash(secrets.token_hex(32), method="pbkdf2:sha256:1000")


def parse_email_list(text: str) -> list[str]:
    """Emails separated by new lines (or commas / semicolons / spaces), lower-cased, duplicates removed in order."""
    seen, out = set(), []
    for item in re.split(r"[\s,;]+", text or ""):
        item = item.strip().strip("<>").lower()
        if item and item not in seen:
            seen.add(item); out.append(item)
    return out


def whitelist_staff(conn, core, contest_id: str, email: str, staff_role: str) -> str:
    """Put `email` on a contest's staff list. They sign in with Google (no password). Returns 'director', 'created' or 'updated'."""
    existing = conn.execute("SELECT * FROM accounts WHERE lower(email)=? OR lower(alt_email)=? ORDER BY id LIMIT 1", (email, email)).fetchone()
    if existing and existing["role"] == "admin":
        return "director"
    if existing:
        account_id, outcome = existing["id"], "updated"
        conn.execute("UPDATE accounts SET role='judge' WHERE id=?", (account_id,))
    else:
        username = core._unique_username(conn, email)
        cur = conn.execute("INSERT INTO accounts(username,email,password_hash,display_name,affiliation,role,created_at) VALUES(?,?,?,?,?,?,?)",
                           (username, email, unusable_password_hash(), username, "TACTICS x ACPC", "judge", core.iso(core.utcnow())))
        account_id, outcome = cur.lastrowid, "created"
    conn.execute("DELETE FROM staff_assignments WHERE contest_id=? AND account_id=?", (contest_id, account_id))
    conn.execute("INSERT INTO staff_assignments(contest_id,account_id,staff_role,created_at) VALUES(?,?,?,?)", (contest_id, account_id, staff_role, core.iso(core.utcnow())))
    audit(conn, "staff.create", contest_id=contest_id, object_type="account", object_id=account_id, detail=f"{email} - {STAFF_ROLES[staff_role]} (from contest form)")
    return outcome


def staff_can(permission: str) -> bool:
    """True if the signed-in user holds `permission` (admins and Contest Directors hold everything)."""
    core = _core(); me = core.current_user()
    if not me:
        return False
    if me["role"] == "admin":
        return True
    contest = core.active_contest()
    if not contest:
        return False
    row = core.get_db().execute("SELECT staff_role FROM staff_assignments WHERE contest_id=? AND account_id=?", (contest["id"], me["id"])).fetchone()
    role = row["staff_role"] if row else ("judge" if me["role"] == "judge" else "")
    perms = ROLE_PERMISSIONS.get(role, set())
    return "all" in perms or permission in perms


def _csv_list(value: str) -> list[str]:
    return [x.strip() for x in re.split(r"[\n,;]+", value or "") if x.strip()]


def eligibility_errors(contest, *, department: str, college: str, institution: str, year_level: str) -> list[str]:
    errors = []
    checks = [
        ("allowed_departments", department, "department/program"),
        ("allowed_colleges", college, "college"),
        ("allowed_institutions", institution, "institution"),
        ("allowed_year_levels", year_level, "year level"),
    ]
    for field, actual, label in checks:
        allowed = _csv_list(contest[field] if field in contest.keys() else "")
        if allowed and actual.strip().casefold() not in {x.casefold() for x in allowed}:
            errors.append(f"Your {label} is not eligible for this contest.")
    return errors


def staff_display_role(contest_id: str, account_id: int, conn=None) -> str | None:
    core = _core(); conn = conn or core.get_db()
    row = conn.execute("SELECT staff_role FROM staff_assignments WHERE contest_id=? AND account_id=?", (contest_id, account_id)).fetchone()
    return row["staff_role"] if row else None


def contest_scope_label(value: str) -> str:
    return dict(CONTEST_SCOPES).get(value, value.replace("_", " ").title())


# ---------- Institutions ----------
@bp.route("/director/institutions", methods=["GET", "POST"])
@_admin_required
def institutions():
    core = _core(); conn = core.get_db(); contest = core.active_contest(conn)
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        if not name:
            flash("Institution name is required.", "danger")
        else:
            try:
                conn.execute(
                    "INSERT INTO institutions(name,short_name,institution_type,city,region,country,created_at) VALUES(?,?,?,?,?,?,?)",
                    (name, request.form.get("short_name", "").strip(), request.form.get("institution_type", "University").strip() or "University", request.form.get("city", "").strip(), request.form.get("region", "").strip(), request.form.get("country", "Philippines").strip() or "Philippines", core.iso(core.utcnow())),
                )
                audit(conn, "institution.create", contest_id=contest["id"] if contest else None, object_type="institution", detail=name)
                conn.commit(); flash("Institution added.", "success")
                return redirect(url_for("v1.institutions"))
            except sqlite3.IntegrityError:
                conn.rollback(); flash("That institution already exists.", "danger")
    rows = conn.execute("SELECT * FROM institutions ORDER BY active DESC,name").fetchall()
    return render_template("v1_institutions.html", institutions=rows)


@bp.post("/director/institutions/<int:iid>/toggle")
@_admin_required
def institution_toggle(iid):
    core = _core(); conn = core.get_db(); row = conn.execute("SELECT * FROM institutions WHERE id=?", (iid,)).fetchone()
    if not row: abort(404)
    conn.execute("UPDATE institutions SET active=? WHERE id=?", (0 if row["active"] else 1, iid)); audit(conn, "institution.toggle", object_type="institution", object_id=iid, detail=row["name"]); conn.commit()
    return redirect(url_for("v1.institutions"))


# ---------- Staff / roles ----------
@bp.route("/director/staff", methods=["GET", "POST"])
@_admin_required
def staff():
    core = _core(); conn = core.get_db(); contest = core.active_contest(conn)
    if request.method == "POST":
        username = request.form.get("username", "").strip().lower(); email = request.form.get("email", "").strip().lower(); display = request.form.get("display_name", "").strip(); password = request.form.get("password", "").strip(); staff_role = request.form.get("staff_role", "judge")
        email_ok = bool(email) and bool(re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email))
        google_only = email_ok and not password          # whitelisted by email: they sign in with Google, no password
        if staff_role not in STAFF_ROLES:
            flash("Invalid staff role.", "danger")
        elif email and not email_ok:
            flash("Enter a valid email address.", "danger")
        elif not google_only and (not re.fullmatch(r"[a-z0-9][a-z0-9_.-]{2,31}", username or "") or len(password) < 6 or not display):
            flash("Enter the staff member's ADNU email, or a name, username and a password of at least 6 characters.", "danger")
        else:
            try:
                existing = conn.execute("SELECT * FROM accounts WHERE lower(email)=? OR lower(alt_email)=? ORDER BY id LIMIT 1", (email, email)).fetchone() if email else None
                if existing and existing["role"] == "admin":
                    flash("That person is already a director.", "danger")
                    return redirect(url_for("v1.staff"))
                if existing:
                    account_id = existing["id"]; display = existing["display_name"] or display
                    conn.execute("UPDATE accounts SET role='judge' WHERE id=?", (account_id,))
                else:
                    if google_only:
                        username = username or core._unique_username(conn, email); display = display or username; pw_hash = unusable_password_hash()
                    else:
                        pw_hash = generate_password_hash(password)
                    cur = conn.execute("INSERT INTO accounts(username,email,password_hash,display_name,affiliation,role,created_at) VALUES(?,?,?,?,?,?,?)", (username, email, pw_hash, display, "TACTICS x ACPC", "judge", core.iso(core.utcnow())))
                    account_id = cur.lastrowid
                conn.execute("DELETE FROM staff_assignments WHERE contest_id=? AND account_id=?", (contest["id"], account_id))
                conn.execute("INSERT INTO staff_assignments(contest_id,account_id,staff_role,created_at) VALUES(?,?,?,?)", (contest["id"], account_id, staff_role, core.iso(core.utcnow())))
                audit(conn, "staff.create", contest_id=contest["id"], object_type="account", object_id=account_id, detail=f"{display or email} - {STAFF_ROLES[staff_role]}" + (" (Google sign-in)" if google_only else ""))
                conn.commit(); flash("Added to staff. They sign in with Google using that address." if google_only else "Staff account created.", "success"); return redirect(url_for("v1.staff"))
            except sqlite3.IntegrityError:
                conn.rollback(); flash("Username or email already exists.", "danger")
    rows = conn.execute("""SELECT a.*,s.staff_role FROM staff_assignments s JOIN accounts a ON a.id=s.account_id WHERE s.contest_id=? ORDER BY a.display_name""", (contest["id"],)).fetchall()
    return render_template("v1_staff.html", staff=rows, staff_roles=STAFF_ROLES)


@bp.post("/director/staff/<int:aid>/role")
@_admin_required
def staff_role_update(aid):
    core = _core(); conn=core.get_db(); contest=core.active_contest(conn); role=request.form.get("staff_role","")
    if role not in STAFF_ROLES: abort(400)
    acct=conn.execute("SELECT * FROM accounts WHERE id=?",(aid,)).fetchone()
    if not acct: abort(404)
    display=request.form.get("display_name",acct["display_name"]).strip() or acct["display_name"]
    username=request.form.get("username",acct["username"]).strip().lower()
    email=request.form.get("email",acct["email"] or "").strip().lower()
    if not re.fullmatch(r"[a-z0-9][a-z0-9_.-]{2,31}",username) or (email and not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+",email)):
        flash("Provide a valid username and email.","danger"); return redirect(url_for("v1.staff"))
    try:
        conn.execute("UPDATE accounts SET display_name=?,username=?,email=? WHERE id=?",(display,username,email,aid))
        conn.execute("INSERT INTO staff_assignments(contest_id,account_id,staff_role,created_at) VALUES(?,?,?,?) ON CONFLICT(contest_id,account_id) DO UPDATE SET staff_role=excluded.staff_role", (contest["id"],aid,role,core.iso(core.utcnow())))
        audit(conn,"staff.role",contest_id=contest["id"],object_type="account",object_id=aid,detail=f"{display}; {role}"); conn.commit(); flash("Staff account updated.","success")
    except sqlite3.IntegrityError:
        conn.rollback(); flash("That username or email is already used by another account.","danger")
    return redirect(url_for("v1.staff"))


# ---------- Announcements + emergency ----------
@bp.route("/director/operations", methods=["GET", "POST"])
@_staff_permission("announcements")
def operations():
    core=_core(); conn=core.get_db(); contest=core.active_contest(conn)
    if request.method=="POST":
        title=request.form.get("title","").strip(); body=request.form.get("body","").strip(); sev=request.form.get("severity","info"); audience=request.form.get("audience","all")
        if not title or not body: flash("Announcement title and message are required.","danger")
        else:
            cur=conn.execute("INSERT INTO announcements(contest_id,title,body,severity,audience,pinned,created_by,created_at) VALUES(?,?,?,?,?,?,?,?)",(contest["id"],title,body,sev,audience,1 if request.form.get("pinned")=="on" else 0,core.current_user()["id"],core.iso(core.utcnow())))
            audit(conn,"announcement.create",contest_id=contest["id"],object_type="announcement",object_id=cur.lastrowid,detail=title); conn.commit(); flash("Announcement published.","success"); return redirect(url_for("v1.operations"))
    anns=conn.execute("SELECT * FROM announcements WHERE contest_id=? ORDER BY pinned DESC,id DESC LIMIT 100",(contest["id"],)).fetchall()
    return render_template("v1_operations.html", announcements=anns, problems=core.contest_problems(contest["id"], conn))


@bp.post("/director/emergency")
@_staff_permission("emergency")
def emergency_action():
    core=_core(); conn=core.get_db(); contest=core.active_contest(conn); action=request.form.get("action","")
    now=core.iso(core.utcnow())
    if action=="pause":
        conn.execute("UPDATE contests SET lifecycle_phase='paused',paused_at=? WHERE id=?",(now,contest["id"])); msg="Contest paused."
    elif action=="resume":
        paused=core.parse_dt(contest["paused_at"]); extra=max(0,int((core.utcnow()-paused).total_seconds())) if paused else 0
        conn.execute("UPDATE contests SET lifecycle_phase='running',paused_at=NULL,pause_accumulated_seconds=pause_accumulated_seconds+?,duration_minutes=duration_minutes+? WHERE id=?",(extra, max(0,(extra+59)//60),contest["id"])); msg="Contest resumed."
    elif action=="extend":
        minutes=max(1,min(180,int(request.form.get("minutes","5")))); conn.execute("UPDATE contests SET duration_minutes=duration_minutes+? WHERE id=?",(minutes,contest["id"])); msg=f"Contest extended by {minutes} minutes."
    elif action=="stop_submissions":
        conn.execute("UPDATE contests SET submissions_open=0 WHERE id=?",(contest["id"],)); msg="Submissions stopped."
    elif action=="open_submissions":
        conn.execute("UPDATE contests SET submissions_open=1 WHERE id=?",(contest["id"],)); msg="Submissions opened."
    elif action in {"disable_problem","enable_problem"}:
        try: pid=int(request.form.get("problem_id","0"))
        except ValueError: pid=0
        problem=conn.execute("SELECT * FROM problems WHERE id=? AND contest_id=?",(pid,contest["id"])).fetchone()
        if not problem: flash("Choose a valid problem.","danger"); return redirect(url_for("v1.operations"))
        enabled=1 if action=="enable_problem" else 0
        conn.execute("UPDATE problems SET enabled=? WHERE id=?",(enabled,pid)); msg=f"Problem {problem['code']} {'enabled' if enabled else 'disabled'}."
    elif action=="broadcast":
        text=request.form.get("message","").strip()
        if not text: flash("Emergency message is required.","danger"); return redirect(url_for("v1.operations"))
        conn.execute("UPDATE contests SET emergency_message=? WHERE id=?",(text,contest["id"])); conn.execute("INSERT INTO announcements(contest_id,title,body,severity,audience,pinned,created_by,created_at) VALUES(?,?,?,?,?,?,?,?)",(contest["id"],"Emergency notice",text,"danger","all",1,core.current_user()["id"],now)); msg="Emergency notice broadcast."
    elif action=="clear_broadcast":
        conn.execute("UPDATE contests SET emergency_message='' WHERE id=?",(contest["id"],)); msg="Emergency notice cleared."
    elif action=="restart_judging":
        conn.execute("UPDATE submissions SET status='PENDING',verdict=NULL,runtime_ms=NULL,judging_started_at=NULL,judged_at=NULL,detail='Requeued by emergency control.' WHERE contest_id=? AND status!='PENDING' AND manual_override=0",(contest["id"],)); msg="All submissions requeued."
    else:
        abort(400)
    audit(conn,f"emergency.{action}",contest_id=contest["id"],object_type="contest",object_id=contest["id"],detail=msg); conn.commit(); flash(msg,"success"); return redirect(url_for("v1.operations"))


@bp.get("/announcements")
def public_announcements():
    core=_core(); conn=core.get_db(); contest=core.active_contest(conn)
    if not contest: return render_template("v1_announcements.html", announcements=[])
    me=core.current_user(); audience="participants" if me and me["role"]=="participant" else "all"
    rows=conn.execute("SELECT * FROM announcements WHERE contest_id=? AND (audience='all' OR audience=?) ORDER BY pinned DESC,id DESC",(contest["id"],audience)).fetchall()
    return render_template("v1_announcements.html", announcements=rows)


# ---------- Audit / disqualification history ----------
@bp.get("/director/audit")
@_admin_required
def audit_log():
    core=_core(); conn=core.get_db(); contest=core.active_contest(conn); q=request.args.get("q","").strip()
    if q:
        rows=conn.execute("SELECT * FROM audit_log WHERE contest_id=? AND (action LIKE ? OR actor_name LIKE ? OR detail LIKE ?) ORDER BY id DESC LIMIT 500",(contest["id"],f"%{q}%",f"%{q}%",f"%{q}%")).fetchall()
    else:
        rows=conn.execute("SELECT * FROM audit_log WHERE contest_id=? ORDER BY id DESC LIMIT 500",(contest["id"],)).fetchall()
    dq=conn.execute("SELECT d.*,a.display_name FROM disqualification_history d LEFT JOIN accounts a ON a.id=d.account_id WHERE d.contest_id=? ORDER BY d.id DESC LIMIT 100",(contest["id"],)).fetchall()
    return render_template("v1_audit.html", rows=rows,dq_history=dq,q=q)


# ---------- Problem package / validation ----------
def _problem_package_bytes(problem, tests_dir: Path) -> bytes:
    buf=io.BytesIO(); ext={"cpp":"cpp","c":"c","java":"java","kotlin":"kt","python3":"py","rust":"rs","ruby":"rb"}.get(problem["reference_language"] or "","txt")
    with zipfile.ZipFile(buf,"w",zipfile.ZIP_DEFLATED) as z:
        yaml=(f"name: {problem['title']}\nlimits:\n  time_multiplier: 1\n  memory: {problem['memory_limit_mb']}\n")
        z.writestr("problem.yaml",yaml)
        z.writestr("problem_statement/problem.en.md",f"# {problem['title']}\n\n{problem['statement']}\n")
        images_dir=tests_dir.parent/"images"
        if images_dir.is_dir():
            for pic in sorted(images_dir.iterdir()):
                if pic.is_file(): z.write(pic,f"problem_statement/{pic.name}")
        z.writestr("inquisitor.json",json.dumps({"api_id":problem["api_id"],"code":problem["code"],"title":problem["title"],"difficulty":problem["difficulty"],"tags":problem["tags"],"contributors":problem["contributors"],"source":problem["source"],"points":problem["points"],"time_limit_ms":problem["time_limit_ms"],"memory_limit_mb":problem["memory_limit_mb"],"color":problem["color"]},indent=2))
        for inp in sorted(tests_dir.glob("input*.txt")):
            num=re.findall(r"\d+",inp.stem)[-1]; out=tests_dir/f"output{num}.txt"
            z.write(inp,f"data/secret/{num}.in")
            if out.exists(): z.write(out,f"data/secret/{num}.ans")
        if problem["reference_solution"]:
            z.writestr(f"submissions/accepted/reference.{ext}",problem["reference_solution"])
    return buf.getvalue()


@bp.get("/director/problems/<int:pid>/export-package")
@_staff_permission("problems")
def export_problem_package(pid):
    core=_core(); conn=core.get_db(); p=conn.execute("SELECT * FROM problems WHERE id=?",(pid,)).fetchone()
    if not p: abort(404)
    data=_problem_package_bytes(p,core.problem_tests_dir(p)); audit(conn,"problem.export_package",contest_id=p["contest_id"],object_type="problem",object_id=pid,detail=p["code"]); conn.commit()
    return send_file(io.BytesIO(data),as_attachment=True,download_name=f"{p['code']}-{p['api_id']}.zip",mimetype="application/zip")


@bp.post("/director/problems/import-package")
@_staff_permission("problems")
def import_problem_package():
    core=_core(); conn=core.get_db(); contest=core.active_contest(conn); f=request.files.get("package")
    if not f or not f.filename: flash("Choose a problem package ZIP.","danger"); return redirect(url_for("admin_problems"))
    try:
        data=f.read()
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            names=z.namelist(); meta={}
            if "inquisitor.json" in names: meta=json.loads(z.read("inquisitor.json").decode("utf-8"))
            stmt=""
            for candidate in ("problem_statement/problem.en.md","problem_statement/problem.md","statement.md"):
                if candidate in names: stmt=z.read(candidate).decode("utf-8",errors="replace"); break
            title=meta.get("title") or (stmt.splitlines()[0].lstrip("# ") if stmt else Path(f.filename).stem)
            code=(request.form.get("code") or meta.get("code") or "P").strip().upper()[:8]
            api_id=(request.form.get("api_id") or meta.get("api_id") or re.sub(r"[^a-z0-9]+","-",title.lower()).strip("-") or "problem")[:36]
            cur=conn.execute("""INSERT INTO problems(contest_id,api_id,code,title,statement,contributors,color,sample_input,sample_output,time_limit_ms,memory_limit_mb,ordinal,difficulty,tags,source,internal_notes,points,enabled,reference_solution,reference_language,package_format)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",(contest["id"],api_id,code,title,stmt,meta.get("contributors",""),meta.get("color","#5bc98b"),"","",int(meta.get("time_limit_ms",2000)),int(meta.get("memory_limit_mb",256)),int(request.form.get("ordinal","1")),meta.get("difficulty","Medium"),meta.get("tags",""),meta.get("source",""),"Imported package",float(meta.get("points",100)),1,"","","icpc-package"))
            p=conn.execute("SELECT * FROM problems WHERE id=?",(cur.lastrowid,)).fetchone(); td=core.problem_tests_dir(p); td.mkdir(parents=True,exist_ok=True)
            pairs=[]
            for name in names:
                m=re.fullmatch(r"data/(?:secret|sample)/(\d+)\.in",name)
                if m:
                    n=m.group(1); ans=f"data/{'secret' if name.startswith('data/secret') else 'sample'}/{n}.ans"
                    if ans in names: pairs.append((name,ans))
            for i,(inp,ans) in enumerate(pairs,1):
                (td/f"input{i:02d}.txt").write_bytes(z.read(inp)); (td/f"output{i:02d}.txt").write_bytes(z.read(ans))
            for name in names:
                if name.startswith("submissions/accepted/") and not name.endswith("/"):
                    ref=z.read(name).decode("utf-8",errors="replace"); ext=Path(name).suffix.lower(); lang=core.detect_language_from_filename("x"+ext) or ""; conn.execute("UPDATE problems SET reference_solution=?,reference_language=? WHERE id=?",(ref,lang,p["id"])); break
        audit(conn,"problem.import_package",contest_id=contest["id"],object_type="problem",object_id=cur.lastrowid,detail=f.filename); conn.commit(); flash(f"Imported {title}.","success")
    except Exception as exc:
        conn.rollback(); flash(f"Could not import problem package: {exc}","danger")
    return redirect(url_for("admin_problems"))


def validate_problem(problem, conn=None) -> dict:
    core=_core(); conn=conn or core.get_db(); report={"checks":[],"ok":True}
    def check(name, ok, detail):
        report["checks"].append({"name":name,"ok":bool(ok),"detail":detail}); report["ok"] = report["ok"] and bool(ok)
    td=core.problem_tests_dir(problem); inputs=sorted(td.glob("input*.txt")); outputs=sorted(td.glob("output*.txt"))
    check("Statement",bool(problem["statement"].strip()),"Problem statement is present." if problem["statement"].strip() else "Statement is empty.")
    check("Limits",100<=problem["time_limit_ms"]<=60000 and 16<=problem["memory_limit_mb"]<=16384,f"{problem['time_limit_ms']} ms / {problem['memory_limit_mb']} MB")
    check("Judge data",len(inputs)>0 and len(inputs)==len(outputs),f"{len(inputs)} inputs / {len(outputs)} outputs")
    check("Samples",bool(problem["sample_input"].strip())==bool(problem["sample_output"].strip()),"Sample pair is complete." if bool(problem["sample_input"].strip())==bool(problem["sample_output"].strip()) else "Sample input/output pair is incomplete.")
    if problem["reference_solution"] and problem["reference_language"]:
        result=core.judge_submission(language=problem["reference_language"],source=problem["reference_solution"],tests_dir=td,time_limit_ms=problem["time_limit_ms"],memory_limit_mb=problem["memory_limit_mb"],output_limit_mb=problem["output_limit_mb"])
        check("Reference solution",result.get("verdict")=="AC",f"{result.get('verdict')}: {result.get('detail','')[:300]}")
    else:
        check("Reference solution",True,"No reference solution configured (optional): the hidden tests are used as they are.")
    if problem["validator_source"].strip():
        validator_ok=True; detail="Validator accepted all hidden inputs."
        try:
            compile(problem["validator_source"],"<validator>","exec")
            for inp in inputs:
                rp=subprocess.run([sys.executable,"-I","-c",problem["validator_source"]],input=inp.read_text(encoding="utf-8"),capture_output=True,text=True,timeout=3)
                if rp.returncode!=0:
                    validator_ok=False; detail=f"Validator rejected {inp.name}: {(rp.stderr or rp.stdout)[-300:]}"; break
        except Exception as exc:
            validator_ok=False; detail=f"Validator failed: {exc}"
        check("Input validator",validator_ok,detail)
    else:
        report["checks"].append({"name":"Input validator","ok":True,"detail":"No custom validator configured; skipped."})
    return report


@bp.post("/director/problems/<int:pid>/validate")
@_staff_permission("problems")
def validate_problem_route(pid):
    core=_core(); conn=core.get_db(); p=conn.execute("SELECT * FROM problems WHERE id=?",(pid,)).fetchone()
    if not p: abort(404)
    report=validate_problem(p,conn); cur=conn.execute("INSERT INTO problem_validations(problem_id,status,report_json,validated_by,validated_at) VALUES(?,?,?,?,?)",(pid,"PASS" if report["ok"] else "FAIL",json.dumps(report),core.current_user()["id"],core.iso(core.utcnow()))); audit(conn,"problem.validate",contest_id=p["contest_id"],object_type="problem",object_id=pid,detail="PASS" if report["ok"] else "FAIL"); conn.commit(); flash("Problem validation passed." if report["ok"] else "Problem validation failed: "+"; ".join(f"{c['name']}: {c['detail']}" for c in report["checks"] if not c["ok"])[:500],"success" if report["ok"] else "danger"); return redirect(url_for("admin_problems"))


# ---------- Registration, check-in, credentials ----------
@bp.post("/director/participants/<int:aid>/status")
@_staff_permission("participants")
def participant_status(aid):
    core=_core(); conn=core.get_db(); contest=core.active_contest(conn); status=request.form.get("status","REGISTERED")
    if status not in REGISTRATION_STATUSES: abort(400)
    values=[status, core.iso(core.utcnow()) if status=="CHECKED_IN" else None, core.iso(core.utcnow()) if status=="WITHDRAWN" else None, contest["id"], aid]
    conn.execute("UPDATE registrations SET registration_status=?,checked_in_at=COALESCE(?,checked_in_at),withdrawn_at=COALESCE(?,withdrawn_at) WHERE contest_id=? AND account_id=?",values)
    audit(conn,"registration.status",contest_id=contest["id"],object_type="account",object_id=aid,detail=status); conn.commit(); flash(f"Status changed to {status.replace('_',' ').title()}.","success"); return redirect(url_for("admin_participants"))


@bp.post("/director/participants/<int:aid>/assignment")
@_staff_permission("participants")
def participant_assignment(aid):
    core=_core(); conn=core.get_db(); contest=core.active_contest(conn)
    conn.execute("UPDATE registrations SET seat=?,workstation=?,coach_name=?,adviser_name=?,notes=? WHERE contest_id=? AND account_id=?",(request.form.get("seat","").strip(),request.form.get("workstation","").strip(),request.form.get("coach_name","").strip(),request.form.get("adviser_name","").strip(),request.form.get("notes","").strip(),contest["id"],aid)); audit(conn,"registration.assignment",contest_id=contest["id"],object_type="account",object_id=aid,detail=f"Seat {request.form.get('seat','')} / {request.form.get('workstation','')}"); conn.commit(); flash("Contest assignment updated.","success"); return redirect(url_for("admin_participants"))


@bp.post("/director/participants/<int:aid>/password-reset")
@_staff_permission("participants")
def participant_password_reset(aid):
    core=_core(); conn=core.get_db(); contest=core.active_contest(conn); password=request.form.get("password","").strip()
    if len(password)<6: flash("Temporary password must be at least 6 characters.","danger"); return redirect(url_for("admin_participants"))
    conn.execute("UPDATE accounts SET password_hash=? WHERE id=?",(generate_password_hash(password),aid)); conn.execute("UPDATE registrations SET temporary_password=1 WHERE contest_id=? AND account_id=?",(contest["id"],aid)); audit(conn,"participant.password_reset",contest_id=contest["id"],object_type="account",object_id=aid); conn.commit(); flash("Temporary password set.","success"); return redirect(url_for("admin_participants"))


@bp.post("/director/participants/<int:aid>/approval")
@_staff_permission("participants")
def participant_approval(aid):
    core=_core(); conn=core.get_db(); contest=core.active_contest(conn); status=request.form.get("approval_status","APPROVED")
    if status not in {"PENDING","APPROVED","REJECTED"}: abort(400)
    conn.execute("UPDATE registrations SET approval_status=? WHERE contest_id=? AND account_id=?",(status,contest["id"],aid)); audit(conn,"registration.approval",contest_id=contest["id"],object_type="account",object_id=aid,detail=status); conn.commit(); flash("Registration approval updated.","success"); return redirect(url_for("admin_participants"))


def _credential_pdf(contest, account, reg, temporary_password: str | None = None) -> bytes:
    from reportlab.lib.pagesizes import A6, landscape
    from reportlab.pdfgen import canvas
    import qrcode
    buf=io.BytesIO(); c=canvas.Canvas(buf,pagesize=landscape(A6)); w,h=landscape(A6)
    c.setFillColorRGB(.04,.05,.06); c.rect(0,0,w,h,fill=1,stroke=0); c.setFillColorRGB(.97,.97,.97)
    c.setFont("Helvetica-Bold",15); c.drawString(22,h-32,"Inquisitor")
    c.setFont("Helvetica",7); c.setFillColorRGB(.65,.68,.71); c.drawString(22,h-44,contest["name"][:70])
    c.setFillColorRGB(.97,.97,.97); c.setFont("Helvetica-Bold",12); c.drawString(22,h-72,account["display_name"][:42])
    c.setFont("Helvetica",8); c.setFillColorRGB(.7,.72,.75); c.drawString(22,h-86,(account["department"] or "")[:50]); c.drawString(22,h-98,(account["affiliation"] or "")[:50])
    c.setFillColorRGB(.97,.97,.97); c.setFont("Helvetica-Bold",8); c.drawString(22,42,f"Username: {account['username']}");
    if temporary_password: c.drawString(22,31,f"Temporary password: {temporary_password}")
    c.drawString(22,20,f"Seat: {reg['seat'] or '-'}    Workstation: {reg['workstation'] or '-'}")
    qr=qrcode.make(f"inquisitor://contest/{contest['id']}/account/{account['id']}"); tmp=io.BytesIO(); qr.save(tmp,format="PNG"); tmp.seek(0)
    from reportlab.lib.utils import ImageReader
    c.drawImage(ImageReader(tmp),w-90,22,62,62,mask='auto'); c.showPage(); c.save(); return buf.getvalue()


@bp.get("/director/participants/<int:aid>/credential.pdf")
@_staff_permission("credentials")
def participant_credential(aid):
    core=_core(); conn=core.get_db(); contest=core.active_contest(conn); a=conn.execute("SELECT * FROM accounts WHERE id=?",(aid,)).fetchone(); r=conn.execute("SELECT * FROM registrations WHERE contest_id=? AND account_id=?",(contest["id"],aid)).fetchone()
    if not a or not r: abort(404)
    data=_credential_pdf(contest,a,r); audit(conn,"credential.print",contest_id=contest["id"],object_type="account",object_id=aid); conn.commit(); return send_file(io.BytesIO(data),as_attachment=True,download_name=f"credential-{a['username']}.pdf",mimetype="application/pdf")


@bp.post("/director/participants/<int:aid>/credential-reset.pdf")
@_staff_permission("credentials")
def participant_credential_reset(aid):
    import secrets, string
    core=_core(); conn=core.get_db(); contest=core.active_contest(conn); a=conn.execute("SELECT * FROM accounts WHERE id=? AND role='participant'",(aid,)).fetchone(); r=conn.execute("SELECT * FROM registrations WHERE contest_id=? AND account_id=?",(contest["id"],aid)).fetchone()
    if not a or not r: abort(404)
    alphabet="ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz23456789"
    password="".join(secrets.choice(alphabet) for _ in range(12))
    conn.execute("UPDATE accounts SET password_hash=? WHERE id=?",(generate_password_hash(password),aid)); conn.execute("UPDATE registrations SET temporary_password=1 WHERE contest_id=? AND account_id=?",(contest["id"],aid)); audit(conn,"credential.reset_and_print",contest_id=contest["id"],object_type="account",object_id=aid); conn.commit()
    data=_credential_pdf(contest,a,r,password)
    return send_file(io.BytesIO(data),as_attachment=True,download_name=f"credential-reset-{a['username']}.pdf",mimetype="application/pdf")


@bp.post("/director/participants/import")
@_staff_permission("participants")
def participants_import_csv():
    core=_core(); conn=core.get_db(); contest=core.active_contest(conn); f=request.files.get("csv_file")
    if not f or not f.filename: flash("Choose a CSV file.","danger"); return redirect(url_for("admin_participants"))
    try:
        text=f.read().decode("utf-8-sig"); reader=csv.DictReader(io.StringIO(text)); created=0
        required={"username","display_name","department"}
        if not required.issubset(set(reader.fieldnames or [])): raise ValueError("CSV requires username, display_name, and department columns.")
        for row in reader:
            username=(row.get("username") or "").strip().lower(); email=(row.get("email") or "").strip().lower(); display=(row.get("display_name") or "").strip(); password=(row.get("password") or "inquisitor").strip(); department=(row.get("department") or "").strip(); affiliation=(row.get("affiliation") or "").strip(); college=(row.get("college") or "").strip(); year=(row.get("year_level") or "").strip(); team=(row.get("team_name") or "").strip()
            if not username or not display or not department: continue
            existing=conn.execute("SELECT id FROM accounts WHERE username=?",(username,)).fetchone()
            if existing: aid=existing["id"]
            else:
                cur=conn.execute("INSERT INTO accounts(username,email,password_hash,display_name,affiliation,department,college,year_level,role,created_at) VALUES(?,?,?,?,?,?,?,?,'participant',?)",(username,email,generate_password_hash(password),display,affiliation,department,college,year,core.iso(core.utcnow()))); aid=cur.lastrowid; created+=1
            conn.execute("INSERT OR IGNORE INTO registrations(contest_id,account_id,team_api_id,team_name,approval_status,registration_status) VALUES(?,?,?,?,?,?)",(contest["id"],aid,f"p-{aid}",team,"PENDING" if contest["requires_approval"] else "APPROVED","REGISTERED"))
        audit(conn,"participants.import",contest_id=contest["id"],object_type="contest",object_id=contest["id"],detail=f"{created} accounts created from {f.filename}"); conn.commit(); flash(f"CSV imported. {created} new account(s) created.","success")
    except Exception as exc:
        conn.rollback(); flash(f"Import failed: {exc}","danger")
    return redirect(url_for("admin_participants"))


# ---------- Similarity review ----------
def _normalize_source(src: str) -> str:
    src=re.sub(r"/\*.*?\*/"," ",src,flags=re.S); src=re.sub(r"//.*?$|#.*?$"," ",src,flags=re.M); src=re.sub(r"\s+"," ",src); return src.strip().lower()


SIMILARITY_DEFAULT_THRESHOLD = 0.82
SIMILARITY_MIN_SOURCE_CHARS = 40
SIMILARITY_MAX_PAIRS = 150_000


def _parse_threshold(raw) -> float:
    try:
        return max(.50, min(1.0, float(raw)))
    except (TypeError, ValueError):
        return SIMILARITY_DEFAULT_THRESHOLD


def similarity_summary(conn, contest_id: str, *, scope: str, threshold: float, problem_code: str = "", show_all: bool = False):
    """Per-participant similarity: each participant's highest-matching submission against everyone else's.

    scope "latest" compares only each participant's most recent submission per problem; "all" includes older ones.
    Nothing is stored. Two submissions are only compared when they are for the same problem in the same language.
    Returns (rows, info) where rows are sorted by highest similarity and info carries counts / errors.
    """
    sql = """SELECT s.id, s.account_id, s.problem_id, s.language, s.source, s.submitted_at, a.display_name, p.code
             FROM submissions s JOIN accounts a ON a.id=s.account_id JOIN problems p ON p.id=s.problem_id
             WHERE s.contest_id=? AND s.status='DONE'"""
    args = [contest_id]
    if problem_code:
        sql += " AND p.code=?"; args.append(problem_code)
    subs = conn.execute(sql + " ORDER BY s.id", args).fetchall()
    if scope == "latest":
        newest = {}
        for s in subs:
            newest[(s["account_id"], s["problem_id"])] = s  # ordered by id, so the last one wins
        subs = list(newest.values())
    groups = {}
    for s in subs:
        groups.setdefault((s["problem_id"], s["language"]), []).append(s)
    pair_total = sum(len(g) * (len(g) - 1) // 2 for g in groups.values())
    info = {"submissions": len(subs), "pairs": pair_total, "error": ""}
    if pair_total > SIMILARITY_MAX_PAIRS:
        info["error"] = f"{pair_total:,} pairs is too many to compare at once. Use 'latest submission per problem' or pick one problem."
        return [], info
    people = {}
    def person(s):
        return people.setdefault(s["account_id"], {"account_id": s["account_id"], "name": s["display_name"], "best": 0.0, "matches": [], "compared": 0})
    for s in subs:
        person(s)
    for group in groups.values():
        norm = [_normalize_source(s["source"]) for s in group]
        for i in range(len(group)):
            # Canonical order: seq1 = the later submission, seq2 = the earlier one (difflib caches seq2, so this is also fast).
            matcher = SequenceMatcher(None, "", norm[i], autojunk=True)
            for j in range(i + 1, len(group)):
                if group[i]["account_id"] == group[j]["account_id"]:
                    continue
                if min(len(norm[i]), len(norm[j])) < SIMILARITY_MIN_SOURCE_CHARS:
                    continue
                matcher.set_seq1(norm[j])
                if not show_all and (matcher.real_quick_ratio() < threshold or matcher.quick_ratio() < threshold):
                    continue
                score = matcher.ratio()
                if score < threshold and not show_all:
                    continue
                for me, other in ((group[i], group[j]), (group[j], group[i])):
                    p = person(me)
                    p["compared"] += 1
                    if score >= threshold:
                        p["matches"].append({"score": score, "other_id": other["account_id"], "other": other["display_name"], "code": me["code"],
                                             "language": me["language"], "mine": me["id"], "theirs": other["id"]})
                    if score > p["best"]:
                        p["best"] = score
    rows = []
    for p in people.values():
        p["matches"].sort(key=lambda m: -m["score"])
        p["flagged"] = p["best"] >= threshold
        p["top"] = p["matches"][0] if p["matches"] else None
        if p["flagged"] or show_all:
            rows.append(p)
    rows.sort(key=lambda p: (-p["best"], p["name"].lower()))
    info["participants"] = len(people)
    info["flagged"] = sum(1 for p in people.values() if p["flagged"])
    return rows, info


def _feedback_rows(conn, contest_id, show="all"):
    where = "s.contest_id=? AND s.status='DONE' AND s.verdict IS NOT NULL AND s.verdict!='AC'"
    if show == "missing": where += " AND s.feedback=''"
    elif show == "given": where += " AND s.feedback!=''"
    return conn.execute(
        f"""SELECT s.id, s.language, s.verdict, s.submitted_at, s.feedback, s.feedback_at, s.review_note,
                   a.display_name participant, a.email participant_email, p.code, p.title, fb.display_name feedback_by_name
            FROM submissions s JOIN accounts a ON a.id=s.account_id JOIN problems p ON p.id=s.problem_id
            LEFT JOIN accounts fb ON fb.id=s.feedback_by
            WHERE {where} ORDER BY a.display_name, p.ordinal, s.id""", (contest_id,)).fetchall()


@bp.get("/director/feedback")
@_staff_permission("analytics")
def feedback_report():
    core=_core(); conn=core.get_db(); contest=core.active_contest(conn)
    show = request.args.get("show", "all")
    if show not in {"all", "missing", "given"}: show = "all"
    rows = _feedback_rows(conn, contest["id"], show)
    allrows = _feedback_rows(conn, contest["id"], "all")
    per = {}
    for r in allrows:
        d = per.setdefault(r["participant"], {"total": 0, "given": 0}); d["total"] += 1; d["given"] += 1 if r["feedback"] else 0
    totals = {"not_accepted": len(allrows), "given": sum(1 for r in allrows if r["feedback"])}
    totals["missing"] = totals["not_accepted"] - totals["given"]
    return render_template("v1_feedback.html", contest=contest, rows=rows, per=per, totals=totals, show=show)


@bp.get("/director/feedback.csv")
@_staff_permission("analytics")
def feedback_csv():
    core=_core(); conn=core.get_db(); contest=core.active_contest(conn)
    def safe(v):
        v = "" if v is None else str(v)
        return "'" + v if v[:1] in ("=", "+", "-", "@", "\t", "\r") else v   # a spreadsheet must never run a cell as a formula
    out = io.StringIO(); w = csv.writer(out)
    w.writerow(["submission", "participant", "email", "problem", "title", "language", "verdict", "submitted_at", "feedback", "feedback_by", "feedback_at"])
    for r in _feedback_rows(conn, contest["id"], "all"):
        w.writerow([r["id"], safe(r["participant"]), safe(r["participant_email"]), r["code"], safe(r["title"]), r["language"], r["verdict"], r["submitted_at"], safe(r["feedback"]), safe(r["feedback_by_name"]), r["feedback_at"] or ""])
    resp = make_response("\ufeff" + out.getvalue()); resp.mimetype = "text/csv"
    resp.headers["Content-Disposition"] = f"attachment; filename=feedback-{contest['id']}.csv"
    audit(conn, "feedback.export", contest_id=contest["id"], object_type="contest", object_id=contest["id"], detail="Feedback report downloaded."); conn.commit()
    return resp


@bp.route("/director/similarity", methods=["GET", "POST"])
@_staff_permission("analytics")
def similarity_review():
    core=_core(); conn=core.get_db(); contest=core.active_contest(conn)
    if request.method=="POST" and request.form.get("action")=="flag":
        try: a=int(request.form.get("a","0")); b=int(request.form.get("b","0")); score=float(request.form.get("score","0"))
        except ValueError: abort(400)
        sa,sb=sorted((a,b))
        ok=conn.execute("SELECT COUNT(*) FROM submissions WHERE contest_id=? AND id IN (?,?)",(contest["id"],sa,sb)).fetchone()[0]==2 and sa!=sb
        if ok:
            conn.execute("INSERT OR IGNORE INTO similarity_flags(contest_id,submission_a,submission_b,similarity,created_at) VALUES(?,?,?,?,?)",(contest["id"],sa,sb,score,core.iso(core.utcnow())))
            audit(conn,"similarity.flag",contest_id=contest["id"],object_type="submission",object_id=f"{sa},{sb}",detail=f"Pair flagged for review at {score:.1%}"); conn.commit(); flash("Pair recorded for review below.","success")
        return redirect(url_for("v1.similarity_review"))
    if request.method=="POST" and request.form.get("action")=="scan":
        threshold=_parse_threshold(request.form.get("threshold",SIMILARITY_DEFAULT_THRESHOLD)); subs=conn.execute("SELECT s.*,a.display_name,p.code FROM submissions s JOIN accounts a ON a.id=s.account_id JOIN problems p ON p.id=s.problem_id WHERE s.contest_id=? AND s.status='DONE' ORDER BY s.problem_id,s.id",(contest["id"],)).fetchall(); groups={}
        for s in subs: groups.setdefault((s["problem_id"],s["language"]),[]).append(s)
        count=0
        for arr in groups.values():
            for i in range(len(arr)):
                for j in range(i+1,len(arr)):
                    if arr[i]["account_id"]==arr[j]["account_id"]: continue
                    a,b=_normalize_source(arr[i]["source"]),_normalize_source(arr[j]["source"])
                    if min(len(a),len(b))<SIMILARITY_MIN_SOURCE_CHARS: continue
                    score=SequenceMatcher(None,b,a,autojunk=True).ratio()
                    if score>=threshold:
                        sa,sb=sorted((arr[i]["id"],arr[j]["id"])); conn.execute("INSERT OR IGNORE INTO similarity_flags(contest_id,submission_a,submission_b,similarity,created_at) VALUES(?,?,?,?,?)",(contest["id"],sa,sb,score,core.iso(core.utcnow()))); count+=1
        audit(conn,"similarity.scan",contest_id=contest["id"],object_type="contest",object_id=contest["id"],detail=f"threshold={threshold}; pairs={count}"); conn.commit(); flash(f"Recorded {count} pair(s) at or above {threshold:.0%} for review.","success"); return redirect(url_for("v1.similarity_review"))
    scope="all" if request.args.get("scope")=="all" else "latest"
    threshold=_parse_threshold(request.args.get("threshold",SIMILARITY_DEFAULT_THRESHOLD)); problem_code=request.args.get("problem","").strip().upper(); show_all=request.args.get("everyone")=="1"
    summary=None; info=None
    if request.args.get("run")=="1":
        summary,info=similarity_summary(conn,contest["id"],scope=scope,threshold=threshold,problem_code=problem_code,show_all=show_all)
    problem_codes=[r["code"] for r in conn.execute("SELECT code FROM problems WHERE contest_id=? ORDER BY ordinal,code",(contest["id"],))]
    rows=conn.execute("""SELECT f.*,a1.display_name name_a,a2.display_name name_b,p.code FROM similarity_flags f JOIN submissions s1 ON s1.id=f.submission_a JOIN submissions s2 ON s2.id=f.submission_b JOIN accounts a1 ON a1.id=s1.account_id JOIN accounts a2 ON a2.id=s2.account_id JOIN problems p ON p.id=s1.problem_id WHERE f.contest_id=? ORDER BY f.similarity DESC,f.id DESC""",(contest["id"],)).fetchall()
    return render_template("v1_similarity.html",flags=rows,summary=summary,info=info,scope=scope,threshold=threshold,problem_code=problem_code,show_all=show_all,problem_codes=problem_codes)


@bp.get("/director/similarity/compare")
@_staff_permission("analytics")
def similarity_compare():
    core=_core(); conn=core.get_db(); contest=core.active_contest(conn)
    try: a=int(request.args.get("a","0")); b=int(request.args.get("b","0"))
    except ValueError: abort(400)
    fetch=lambda sid: conn.execute("SELECT s.*,x.display_name,p.code,p.title FROM submissions s JOIN accounts x ON x.id=s.account_id JOIN problems p ON p.id=s.problem_id WHERE s.id=? AND s.contest_id=?",(sid,contest["id"])).fetchone()
    left,right=fetch(a),fetch(b)
    if not left or not right: abort(404)
    ll,rl=left["source"].splitlines(),right["source"].splitlines(); rows=[]
    for tag,i1,i2,j1,j2 in SequenceMatcher(None,ll,rl,autojunk=False).get_opcodes():
        if tag=="equal":
            rows += [(ll[i1+k],rl[j1+k],"same") for k in range(i2-i1)]
        else:
            n=max(i2-i1,j2-j1)
            rows += [(ll[i1+k] if i1+k<i2 else None, rl[j1+k] if j1+k<j2 else None, "diff") for k in range(n)]
    lo,hi=sorted((left,right),key=lambda r:r["id"])  # same order as similarity_summary so the percentages agree
    score=SequenceMatcher(None,_normalize_source(hi["source"]),_normalize_source(lo["source"]),autojunk=True).ratio()
    return render_template("v1_similarity_compare.html",left=left,right=right,rows=rows,score=score)


@bp.post("/director/similarity/<int:fid>")
@_staff_permission("analytics")
def similarity_update(fid):
    core=_core(); conn=core.get_db(); status=request.form.get("status","REVIEWED"); note=request.form.get("note","").strip(); conn.execute("UPDATE similarity_flags SET status=?,reviewer_id=?,note=? WHERE id=?",(status,core.current_user()["id"],note,fid)); audit(conn,"similarity.review",object_type="similarity_flag",object_id=fid,detail=status); conn.commit(); return redirect(url_for("v1.similarity_review"))


# ---------- Awards / balloons ----------
def _aggregate_award_rows(rows, key):
    buckets={}
    for r in rows:
        if r.get("disqualified"): continue
        if key=="department": names=r["participant"].get("departments",[]) or ["Unspecified"]
        else: names=[r["participant"].get("affiliation") or "Unspecified"]
        for name in names:
            b=buckets.setdefault(name,{"name":name,"solved":0,"penalty":0,"points":0.0,"entries":0})
            b["solved"]+=r.get("solved",0); b["penalty"]+=r.get("penalty",0); b["points"]+=float(r.get("points",0) or 0); b["entries"]+=1
    return list(buckets.values())


def compute_awards(conn, contest):
    core=_core(); rows,problems=core.standings(contest); entities={r["participant"]["id"]:r for r in rows if not r.get("disqualified")}
    awards=conn.execute("SELECT * FROM awards WHERE contest_id=? AND active=1 ORDER BY ordinal,id",(contest["id"],)).fetchall(); now=core.iso(core.utcnow()); results=[]
    conn.execute("DELETE FROM award_recipients WHERE award_id IN (SELECT id FROM awards WHERE contest_id=?)",(contest["id"],))
    for award in awards:
        rule=json.loads(award["rule_json"] or "{}")
        recips=[]; synthetic=[]
        if award["award_type"]=="rank":
            rank=int(rule.get("rank",1)); recips=[r for r in rows if r.get("rank")==rank and not r.get("disqualified")]
        elif award["award_type"]=="first_solve":
            pid=rule.get("problem_id"); recips=[r for r in rows if r["cells"].get(pid,{}).get("first_to_solve")]
        elif award["award_type"]=="department":
            dept=rule.get("department","").casefold(); recips=[r for r in rows if dept and any(d.casefold()==dept for d in r["participant"].get("departments",[]))][:1]
        elif award["award_type"]=="institution":
            inst=rule.get("institution","").casefold(); recips=[r for r in rows if inst and (r["participant"].get("affiliation") or "").casefold()==inst][:1]
        elif award["award_type"]=="year_level":
            year=rule.get("year_level","").casefold(); matched=[]
            for r in rows:
                ids=r["participant"]["account_ids"]; marks=",".join("?" for _ in ids)
                if ids and conn.execute(f"SELECT 1 FROM accounts WHERE id IN ({marks}) AND lower(year_level)=? LIMIT 1",[*ids,year]).fetchone(): matched.append(r)
            recips=matched[:1]
        elif award["award_type"] in {"department_aggregate","institution_aggregate"}:
            key="department" if award["award_type"]=="department_aggregate" else "institution"
            aggs=_aggregate_award_rows(rows,key)
            if contest["scoring_model"]=="icpc": aggs.sort(key=lambda x:(-x["solved"],x["penalty"],x["name"].casefold()))
            else: aggs.sort(key=lambda x:(-x["points"],-x["solved"],x["penalty"],x["name"].casefold()))
            rank=max(1,int(rule.get("rank",1)))
            if len(aggs)>=rank:
                a=aggs[rank-1]; synthetic=[{"id":f"{key}:{a['name']}","display_name":a["name"],"detail":f"{a['entries']} entries · {a['solved']} solved · {a['points']:.0f} points · penalty {a['penalty']}"}]
        elif award["award_type"]=="manual":
            eid=rule.get("entity_id"); recips=[entities[eid]] if eid in entities else []
        for r in recips:
            e=r["participant"]
            detail=(f"{r.get('points',0):.0f} points · {r.get('solved',0)} solved" if contest["scoring_model"]!="icpc" else f"Solved {r['solved']} / penalty {r['penalty']}")
            conn.execute("INSERT OR IGNORE INTO award_recipients(award_id,entity_id,entity_name,detail,computed_at) VALUES(?,?,?,?,?)",(award["id"],e["id"],e["display_name"],detail,now)); results.append((award["name"],e["display_name"]))
        for e in synthetic:
            conn.execute("INSERT OR IGNORE INTO award_recipients(award_id,entity_id,entity_name,detail,computed_at) VALUES(?,?,?,?,?)",(award["id"],e["id"],e["display_name"],e["detail"],now)); results.append((award["name"],e["display_name"]))
    return results


@bp.route("/director/awards", methods=["GET", "POST"])
@_staff_permission("awards")
def awards():
    core=_core(); conn=core.get_db(); contest=core.active_contest(conn)
    if request.method=="POST":
        action=request.form.get("action","create")
        if action=="compute":
            results=compute_awards(conn,contest); audit(conn,"awards.compute",contest_id=contest["id"],object_type="contest",object_id=contest["id"],detail=f"{len(results)} recipients"); conn.commit(); flash("Awards recomputed from the current finalized standings.","success")
        else:
            name=request.form.get("name","").strip(); typ=request.form.get("award_type","manual"); rule={}
            if typ=="rank": rule={"rank":int(request.form.get("rank","1"))}
            elif typ=="first_solve": rule={"problem_id":request.form.get("problem_id","")}
            elif typ=="department": rule={"department":request.form.get("department","").strip()}
            elif typ=="institution": rule={"institution":request.form.get("institution","").strip()}
            elif typ=="year_level": rule={"year_level":request.form.get("year_level","").strip()}
            elif typ in {"department_aggregate","institution_aggregate"}: rule={"rank":int(request.form.get("rank","1"))}
            elif typ=="manual": rule={"entity_id":request.form.get("entity_id","").strip()}
            if not name: flash("Award name is required.","danger")
            else:
                conn.execute("INSERT INTO awards(contest_id,name,award_type,rule_json,ordinal) VALUES(?,?,?,?,?)",(contest["id"],name,typ,json.dumps(rule),int(request.form.get("ordinal","1")))); audit(conn,"award.create",contest_id=contest["id"],object_type="award",detail=name); conn.commit(); flash("Award rule added.","success")
        return redirect(url_for("v1.awards"))
    awards_rows=conn.execute("SELECT * FROM awards WHERE contest_id=? ORDER BY ordinal,id",(contest["id"],)).fetchall(); recipients=conn.execute("SELECT r.*,a.name award_name FROM award_recipients r JOIN awards a ON a.id=r.award_id WHERE a.contest_id=? ORDER BY a.ordinal,a.id,r.entity_name",(contest["id"],)).fetchall(); problems=core.contest_problems(contest["id"],conn); standings_rows,_=core.standings(contest)
    balloons=conn.execute("""SELECT b.*,p.code,p.title,a.display_name FROM balloon_queue b JOIN problems p ON p.id=b.problem_id JOIN accounts a ON a.id=b.account_id WHERE b.contest_id=? ORDER BY CASE b.status WHEN 'PENDING' THEN 0 WHEN 'CLAIMED' THEN 1 ELSE 2 END,b.id""",(contest["id"],)).fetchall()
    first_solves=conn.execute("""SELECT b.*,p.code,p.title,a.display_name,s.submitted_at FROM balloon_queue b JOIN problems p ON p.id=b.problem_id JOIN accounts a ON a.id=b.account_id JOIN submissions s ON s.id=b.submission_id WHERE b.contest_id=? AND b.first_to_solve=1 ORDER BY p.ordinal""",(contest["id"],)).fetchall()
    return render_template("v1_awards.html",awards=awards_rows,recipients=recipients,problems=problems,standings=standings_rows,balloons=balloons,first_solves=first_solves)


@bp.post("/director/balloons/<int:bid>")
@_staff_permission("awards")
def balloon_update(bid):
    core=_core(); conn=core.get_db(); action=request.form.get("action","claim")
    if action=="deliver": conn.execute("UPDATE balloon_queue SET status='DELIVERED',delivered_at=?,claimed_by=COALESCE(claimed_by,?) WHERE id=?",(core.iso(core.utcnow()),core.current_user()["id"],bid))
    elif action=="reset": conn.execute("UPDATE balloon_queue SET status='PENDING',delivered_at=NULL,claimed_by=NULL WHERE id=?",(bid,))
    else: conn.execute("UPDATE balloon_queue SET status='CLAIMED',claimed_by=? WHERE id=?",(core.current_user()["id"],bid))
    conn.commit(); return redirect(url_for("v1.awards"))


def on_judgement(conn, submission, problem):
    core=_core()
    if submission["verdict"]!="AC": return
    # First accepted submission for this entity/problem.
    contest=core.contest_by_id(submission["contest_id"],conn); reg=conn.execute("SELECT * FROM registrations WHERE contest_id=? AND account_id=?",(submission["contest_id"],submission["account_id"])).fetchone(); team_name=(reg["team_name"] if reg else "") or ""
    prior=conn.execute("SELECT COUNT(*) FROM submissions WHERE contest_id=? AND problem_id=? AND verdict='AC' AND status='DONE' AND id<?",(submission["contest_id"],submission["problem_id"],submission["id"])).fetchone()[0]
    first=1 if prior==0 else 0
    conn.execute("INSERT OR IGNORE INTO balloon_queue(contest_id,submission_id,problem_id,account_id,team_name,first_to_solve,status,created_at) VALUES(?,?,?,?,?,?,?,?)",(submission["contest_id"],submission["id"],submission["problem_id"],submission["account_id"],team_name,first,"PENDING",core.iso(core.utcnow())))


# ---------- Analytics / aggregate standings ----------
@bp.get("/director/analytics")
@_staff_permission("analytics")
def analytics():
    core=_core(); conn=core.get_db(); contest=core.active_contest(conn); rows,problems=core.standings(contest)
    submissions=conn.execute("SELECT s.*,a.display_name,p.code,p.title FROM submissions s JOIN accounts a ON a.id=s.account_id JOIN problems p ON p.id=s.problem_id WHERE s.contest_id=? ORDER BY s.submitted_at,s.id",(contest["id"],)).fetchall()
    total=len(submissions); accepted=sum(1 for s in submissions if s["verdict"]=="AC")
    verdicts=conn.execute("SELECT COALESCE(verdict,status) label,COUNT(*) n FROM submissions WHERE contest_id=? GROUP BY COALESCE(verdict,status) ORDER BY n DESC",(contest["id"],)).fetchall()
    per_problem=conn.execute("""SELECT p.code,p.title,p.api_id,COUNT(s.id) submissions,SUM(CASE WHEN s.verdict='AC' THEN 1 ELSE 0 END) accepted,COUNT(DISTINCT CASE WHEN s.verdict='AC' THEN s.account_id END) solvers,MIN(CASE WHEN s.verdict='AC' THEN s.submitted_at END) first_accept FROM problems p LEFT JOIN submissions s ON s.problem_id=p.id WHERE p.contest_id=? GROUP BY p.id ORDER BY p.ordinal""",(contest["id"],)).fetchall()

    # Submission velocity, language use and solve progression.  Fifteen-minute buckets keep the UI readable.
    start, end, _ = core.contest_times(contest)
    now=core.utcnow(); effective_end=min(now,end) if end else now
    elapsed_minutes=max(1.0,((effective_end-start).total_seconds()/60.0) if start else 1.0)
    submissions_per_minute=total/elapsed_minutes
    timeline={}
    solve_progress=[]; solved_pairs=set(); cumulative=0
    language_counts={}
    for sub in submissions:
        language_counts[sub["language"]]=language_counts.get(sub["language"],0)+1
        dt=core.parse_dt(sub["submitted_at"])
        minute=max(0,int((dt-start).total_seconds()//60)) if dt and start else 0
        bucket=(minute//15)*15
        timeline[bucket]=timeline.get(bucket,0)+1
        if sub["verdict"]=="AC":
            key=(sub["account_id"],sub["problem_id"])
            if key not in solved_pairs:
                solved_pairs.add(key); cumulative+=1
                solve_progress.append({"minute":minute,"participant":sub["display_name"],"problem":sub["code"],"cumulative":cumulative})
    submission_timeline=[{"start":b,"end":b+14,"count":timeline[b]} for b in sorted(timeline)]
    language_usage=[]
    for key,count in sorted(language_counts.items(),key=lambda kv:(-kv[1],kv[0])):
        label=core.LANGUAGES[key].label if key in core.LANGUAGES else key
        language_usage.append({"key":key,"label":label,"count":count,"percent":(count/total*100 if total else 0)})

    attempted=[p for p in per_problem if p["submissions"]]
    def solve_rate(p): return (p["solvers"] or 0)/max(1,len([r for r in rows if not r.get("disqualified")]))
    easiest=max(attempted,key=lambda p:(solve_rate(p),p["solvers"],-(p["submissions"] or 0)),default=None)
    hardest=min(attempted,key=lambda p:(solve_rate(p),-(p["submissions"] or 0)),default=None)
    first_solves=conn.execute("""SELECT p.code,p.title,a.display_name,s.submitted_at FROM balloon_queue b JOIN problems p ON p.id=b.problem_id JOIN accounts a ON a.id=b.account_id JOIN submissions s ON s.id=b.submission_id WHERE b.contest_id=? AND b.first_to_solve=1 ORDER BY s.submitted_at,p.ordinal""",(contest["id"],)).fetchall()

    dept={}; inst={}
    for r in rows:
        if r.get("disqualified"): continue
        depts=r["participant"].get("departments",[]) or ["Unspecified"]
        for d in depts: dept.setdefault(d,{"name":d,"solved":0,"penalty":0,"points":0.0,"entries":0}); dept[d]["solved"]+=r["solved"]; dept[d]["penalty"]+=r["penalty"]; dept[d]["points"]+=float(r.get("points",0) or 0); dept[d]["entries"]+=1
        i=r["participant"].get("affiliation") or "Unspecified"; inst.setdefault(i,{"name":i,"solved":0,"penalty":0,"points":0.0,"entries":0}); inst[i]["solved"]+=r["solved"]; inst[i]["penalty"]+=r["penalty"]; inst[i]["points"]+=float(r.get("points",0) or 0); inst[i]["entries"]+=1
    if contest["scoring_model"]=="icpc":
        agg_key=lambda x:(-x["solved"],x["penalty"],x["name"].casefold())
    else:
        agg_key=lambda x:(-x["points"],-x["solved"],x["penalty"],x["name"].casefold())
    dept_rows=sorted(dept.values(),key=agg_key); inst_rows=sorted(inst.values(),key=agg_key)
    return render_template("v1_analytics.html",total=total,accepted=accepted,acceptance=(accepted/total*100 if total else 0),submissions_per_minute=submissions_per_minute,verdicts=verdicts,per_problem=per_problem,department_standings=dept_rows,institution_standings=inst_rows,language_usage=language_usage,submission_timeline=submission_timeline,solve_progress=solve_progress,easiest=easiest,hardest=hardest,first_solves=first_solves)


# ---------- Backup ----------
def create_backup(reason="manual", actor_id=None, contest_id=None) -> Path:
    core=_core(); backup_dir=core.DATA_DIR/"backups"; backup_dir.mkdir(exist_ok=True); stamp=datetime.now().strftime("%Y%m%d-%H%M%S"); target=backup_dir/f"inquisitor-{stamp}.db"; src=core.db_connect(); dst=sqlite3.connect(target)
    try: src.backup(dst)
    finally: src.close(); dst.close()
    conn=core.db_connect(); conn.execute("INSERT INTO backups(contest_id,filename,size_bytes,reason,created_by,created_at) VALUES(?,?,?,?,?,?)",(contest_id,target.name,target.stat().st_size,reason,actor_id,core.iso(core.utcnow()))); conn.commit(); conn.close(); return target


@bp.route("/director/backups", methods=["GET", "POST"])
@_staff_permission("backups")
def backups():
    core=_core(); conn=core.get_db(); contest=core.active_contest(conn)
    if request.method=="POST":
        path=create_backup("manual",core.current_user()["id"],contest["id"]); audit(conn,"backup.create",contest_id=contest["id"],object_type="backup",object_id=path.name); conn.commit(); flash(f"Backup created: {path.name}","success"); return redirect(url_for("v1.backups"))
    rows=conn.execute("SELECT * FROM backups ORDER BY id DESC LIMIT 200").fetchall(); return render_template("v1_backups.html",backups=rows)


@bp.get("/director/backups/<path:filename>")
@_staff_permission("backups")
def backup_download(filename):
    core=_core(); name=Path(filename).name; path=core.DATA_DIR/"backups"/name
    if not path.exists(): abort(404)
    return send_file(path,as_attachment=True,download_name=name,mimetype="application/octet-stream")


def start_maintenance_worker():
    core=_core(); interval=max(15,int(os.environ.get("INQUISITOR_BACKUP_INTERVAL_MINUTES","60")))
    def has_reason(contest_id, reason):
        conn=core.db_connect()
        try: return conn.execute("SELECT 1 FROM backups WHERE contest_id=? AND reason=? LIMIT 1",(contest_id,reason)).fetchone() is not None
        finally: conn.close()
    def loop():
        last=0.0
        while True:
            try:
                conn=core.db_connect(); contest=core.active_contest(conn); st=core.contest_state(contest) if contest else None; conn.close()
                if contest and st:
                    now=core.utcnow()
                    if st["start"] and 0 <= (st["start"]-now).total_seconds() <= 15*60 and not has_reason(contest["id"],"pre-contest"):
                        create_backup("pre-contest",None,contest["id"])
                    if contest["lifecycle_phase"]=="finalized" and not has_reason(contest["id"],"post-finalization"):
                        create_backup("post-finalization",None,contest["id"])
                    if st["phase"] in {"RUNNING","FROZEN","PAUSED"} and time.time()-last>=interval*60:
                        create_backup("automatic",None,contest["id"]); last=time.time()
                time.sleep(30)
            except Exception as exc:
                print("Maintenance worker error:",repr(exc),flush=True); time.sleep(60)
    threading.Thread(target=loop,name="inquisitor-maintenance",daemon=True).start()


# ---------- Network / server health ----------
@bp.get("/director/network")
@_staff_permission("network")
def network_status():
    core=_core(); conn=core.get_db(); contest=core.active_contest(conn); disk=shutil.disk_usage(core.DATA_DIR); comp=core.compiler_status(); queue=conn.execute("SELECT COUNT(*) FROM submissions WHERE contest_id=? AND status='PENDING'",(contest["id"],)).fetchone()[0]; judging=conn.execute("SELECT COUNT(*) FROM submissions WHERE contest_id=? AND status='JUDGING'",(contest["id"],)).fetchone()[0]
    health={"db":"OK","queue":queue,"judging":judging,"disk_free":disk.free,"disk_total":disk.total,"judge_backend":os.environ.get("INQUISITOR_JUDGE_BACKEND","local"),"host":os.environ.get("COMPUTERNAME") or os.environ.get("HOSTNAME") or "localhost"}
    return render_template("v1_network.html",health=health,compiler=comp)


@bp.get("/director/diagnostics")
@_staff_permission("network")
def diagnostics_page():
    import diagnostics
    core=_core(); conn=core.get_db()
    report=diagnostics.collect(core,conn)
    return render_template("v1_diagnostics.html",report=report,report_text=diagnostics.render_text(report))


@bp.get("/director/diagnostics.txt")
@_staff_permission("network")
def diagnostics_text():
    import diagnostics
    core=_core(); conn=core.get_db()
    resp=make_response(diagnostics.render_text(diagnostics.collect(core,conn))); resp.mimetype="text/plain"; resp.headers["Cache-Control"]="no-store"
    return resp


# ---------- API settings ----------
@bp.route("/director/api-settings", methods=["GET", "POST"])
@_staff_permission("api")
def api_settings():
    core=_core(); conn=core.get_db(); contest=core.active_contest(conn); token_plain=None
    if request.method=="POST":
        action=request.form.get("action","save")
        if action=="rotate_token":
            import secrets
            token_plain=secrets.token_urlsafe(32); token_hash=hashlib.sha256(token_plain.encode()).hexdigest(); conn.execute("UPDATE contests SET api_token_hash=? WHERE id=?",(token_hash,contest["id"])); audit(conn,"api.rotate_token",contest_id=contest["id"],object_type="contest",object_id=contest["id"]); conn.commit(); contest=core.active_contest(conn)
        else:
            conn.execute("UPDATE contests SET api_enabled=?,api_public=? WHERE id=?",(1 if request.form.get("api_enabled")=="on" else 0,1 if request.form.get("api_public")=="on" else 0,contest["id"])); audit(conn,"api.settings",contest_id=contest["id"],object_type="contest",object_id=contest["id"]); conn.commit(); flash("API settings updated.","success"); return redirect(url_for("v1.api_settings"))
    return render_template("v1_api_settings.html",token_plain=token_plain)


def api_token_authorized(contest) -> bool:
    if not contest["api_enabled"]: return False
    if contest["api_public"]: return True
    token=request.headers.get("X-Inquisitor-API-Key","") or request.args.get("api_key","")
    if not token or not contest["api_token_hash"]: return False
    return hashlib.sha256(token.encode()).hexdigest()==contest["api_token_hash"]


# ---------- Exports ----------
def _scoreboard_rows(contest):
    core=_core(); rows,problems=core.standings(contest); return rows,problems


@bp.get("/director/export/scoreboard.csv")
@_admin_required
def export_scoreboard_csv():
    core=_core(); contest=core.active_contest(); rows,problems=_scoreboard_rows(contest); out=io.StringIO(); w=csv.writer(out); w.writerow(["Rank","Name","Department","Institution","Solved","Penalty"]+[p["code"] for p in problems])
    for r in rows:
        e=r["participant"]; vals=[r["rank"] or "DQ",e["display_name"],"; ".join(e.get("departments",[])),e.get("affiliation",""),r["solved"],r["penalty"]]
        for p in problems:
            c=r["cells"][p["api_id"]]; vals.append(f"AC {c['minute']}" if c["status"]=="AC" else (f"WA {c['wrong']}" if c["status"]=="WA" else ""))
        w.writerow(vals)
    data=out.getvalue().encode("utf-8-sig"); return send_file(io.BytesIO(data),as_attachment=True,download_name=f"{contest['id']}-scoreboard.csv",mimetype="text/csv")


@bp.get("/director/export/scoreboard.xlsx")
@_admin_required
def export_scoreboard_xlsx():
    import xlsxwriter
    core=_core(); contest=core.active_contest(); rows,problems=_scoreboard_rows(contest); buf=io.BytesIO(); wb=xlsxwriter.Workbook(buf,{"in_memory":True}); ws=wb.add_worksheet("Final Standings"); head=wb.add_format({"bold":True,"bg_color":"#20252A","font_color":"#FFFFFF","border":1}); normal=wb.add_format({"border":1}); podium=[wb.add_format({"border":1,"bg_color":"#E8C96A"}),wb.add_format({"border":1,"bg_color":"#D2D7DC"}),wb.add_format({"border":1,"bg_color":"#C48A5A"})]
    headers=["Rank","Name","Department","Institution","Solved","Penalty"]+[p["code"] for p in problems]
    for col,h in enumerate(headers): ws.write(0,col,h,head)
    for ri,r in enumerate(rows,1):
        e=r["participant"]; fmt=podium[r["rank"]-1] if r.get("rank") in {1,2,3} else normal; vals=[r["rank"] or "DQ",e["display_name"],"; ".join(e.get("departments",[])),e.get("affiliation",""),r["solved"],r["penalty"]]
        for p in problems:
            c=r["cells"][p["api_id"]]; vals.append(f"AC {c['minute']}" if c["status"]=="AC" else (f"WA {c['wrong']}" if c["status"]=="WA" else ""))
        for ci,v in enumerate(vals): ws.write(ri,ci,v,fmt)
    ws.freeze_panes(1,0); ws.set_column(0,0,8); ws.set_column(1,3,28); ws.set_column(4,len(headers)-1,12); wb.close(); buf.seek(0); return send_file(buf,as_attachment=True,download_name=f"{contest['id']}-scoreboard.xlsx",mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


@bp.get("/director/export/scoreboard.pdf")
@_admin_required
def export_scoreboard_pdf():
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4, landscape
    from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer
    from reportlab.lib.styles import getSampleStyleSheet
    core=_core(); contest=core.active_contest(); rows,problems=_scoreboard_rows(contest); buf=io.BytesIO(); doc=SimpleDocTemplate(buf,pagesize=landscape(A4),rightMargin=24,leftMargin=24,topMargin=24,bottomMargin=24); styles=getSampleStyleSheet(); data=[["Rank","Contestant / Team","Department","Institution","Solved","Penalty"]+[p["code"] for p in problems]]
    for r in rows:
        e=r["participant"]; row=[r["rank"] or "DQ",e["display_name"],", ".join(e.get("departments",[])),e.get("affiliation",""),r["solved"],r["penalty"]]
        for p in problems:
            c=r["cells"][p["api_id"]]; row.append(f"+ {c['minute']}" if c["status"]=="AC" else (f"- {c['wrong']}" if c["status"]=="WA" else ""))
        data.append(row)
    table=Table(data,repeatRows=1); table.setStyle(TableStyle([("BACKGROUND",(0,0),(-1,0),colors.HexColor("#20252A")),("TEXTCOLOR",(0,0),(-1,0),colors.white),("GRID",(0,0),(-1,-1),.25,colors.HexColor("#B7BDC3")),("FONTNAME",(0,0),(-1,0),"Helvetica-Bold"),("FONTSIZE",(0,0),(-1,-1),7),("VALIGN",(0,0),(-1,-1),"MIDDLE")]))
    doc.build([Paragraph(contest["name"],styles["Title"]),Paragraph("Official final standings",styles["Heading2"]),Spacer(1,8),table]); buf.seek(0); return send_file(buf,as_attachment=True,download_name=f"{contest['id']}-scoreboard.pdf",mimetype="application/pdf")


@bp.get("/director/export/submissions.zip")
@_admin_required
def export_submissions_zip():
    core=_core(); conn=core.get_db(); contest=core.active_contest(conn); rows=conn.execute("SELECT s.*,a.username,a.display_name,p.code FROM submissions s JOIN accounts a ON a.id=s.account_id JOIN problems p ON p.id=s.problem_id WHERE s.contest_id=? ORDER BY s.id",(contest["id"],)).fetchall(); buf=io.BytesIO()
    ext={k:v.extension for k,v in core.LANGUAGES.items()}
    with zipfile.ZipFile(buf,"w",zipfile.ZIP_DEFLATED) as z:
        manifest=io.StringIO(); w=csv.writer(manifest); w.writerow(["submission_id","participant","problem","language","verdict","submitted_at"])
        for r in rows:
            path=f"{r['username']}/{r['code']}/{r['id']}{ext.get(r['language'],'.txt')}"; z.writestr(path,r["source"]); w.writerow([r["id"],r["display_name"],r["code"],r["language"],r["verdict"] or r["status"],r["submitted_at"]])
        z.writestr("manifest.csv",manifest.getvalue())
    buf.seek(0); return send_file(buf,as_attachment=True,download_name=f"{contest['id']}-submissions.zip",mimetype="application/zip")


@bp.get("/director/export/problem-stats.csv")
@_admin_required
def export_problem_stats_csv():
    core=_core(); conn=core.get_db(); contest=core.active_contest(conn)
    rows=conn.execute("""SELECT p.code,p.title,p.difficulty,p.tags,p.points,COUNT(s.id) submissions,SUM(CASE WHEN s.verdict='AC' THEN 1 ELSE 0 END) accepted,COUNT(DISTINCT CASE WHEN s.verdict='AC' THEN s.account_id END) solvers FROM problems p LEFT JOIN submissions s ON s.problem_id=p.id WHERE p.contest_id=? GROUP BY p.id ORDER BY p.ordinal""",(contest["id"],)).fetchall()
    out=io.StringIO(); w=csv.writer(out); w.writerow(["Code","Title","Difficulty","Tags","Points","Submissions","Accepted","Unique Solvers","Acceptance Rate"])
    for r in rows:
        rate=(r["accepted"] or 0)/(r["submissions"] or 1)*100; w.writerow([r["code"],r["title"],r["difficulty"],r["tags"],r["points"],r["submissions"],r["accepted"] or 0,r["solvers"],f"{rate:.2f}%"])
    return send_file(io.BytesIO(out.getvalue().encode("utf-8-sig")),as_attachment=True,download_name=f"{contest['id']}-problem-statistics.csv",mimetype="text/csv")


# ---------- Certificates ----------
def _certificate_pdf(contest, name: str, award_text: str) -> bytes:
    from reportlab.lib.pagesizes import A4, landscape
    from reportlab.pdfgen import canvas
    buf=io.BytesIO(); c=canvas.Canvas(buf,pagesize=landscape(A4)); w,h=landscape(A4); c.setFillColorRGB(.04,.05,.06); c.rect(0,0,w,h,fill=1,stroke=0); c.setStrokeColorRGB(.82,.68,.35); c.setLineWidth(2); c.rect(26,26,w-52,h-52,fill=0,stroke=1); c.setFillColorRGB(.96,.97,.98); c.setFont("Helvetica-Bold",26); c.drawCentredString(w/2,h-95,"CERTIFICATE OF RECOGNITION"); c.setFont("Helvetica",12); c.setFillColorRGB(.67,.70,.73); c.drawCentredString(w/2,h-125,contest["name"]); c.setFillColorRGB(.98,.98,.98); c.setFont("Helvetica-Bold",28); c.drawCentredString(w/2,h/2+10,name[:60]); c.setFont("Helvetica",13); c.setFillColorRGB(.78,.80,.82); c.drawCentredString(w/2,h/2-25,award_text[:100]); c.setFont("Helvetica",9); c.drawCentredString(w/2,50,"Issued by TACTICS x ACPC through Inquisitor"); c.showPage(); c.save(); return buf.getvalue()


@bp.get("/director/certificate/<int:recipient_id>.pdf")
@_admin_required
def certificate(recipient_id):
    core=_core(); conn=core.get_db(); row=conn.execute("SELECT r.*,a.name award_name,a.contest_id FROM award_recipients r JOIN awards a ON a.id=r.award_id WHERE r.id=?",(recipient_id,)).fetchone()
    if not row: abort(404)
    contest=core.contest_by_id(row["contest_id"],conn); data=_certificate_pdf(contest,row["entity_name"],row["award_name"]); return send_file(io.BytesIO(data),as_attachment=True,download_name=f"certificate-{re.sub(r'[^A-Za-z0-9]+','-',row['entity_name']).strip('-')}.pdf",mimetype="application/pdf")


@bp.get("/director/certificate/participant/<int:aid>.pdf")
@_staff_permission("credentials")
def participation_certificate(aid):
    core=_core(); conn=core.get_db(); contest=core.active_contest(conn)
    account=conn.execute("SELECT a.* FROM registrations r JOIN accounts a ON a.id=r.account_id WHERE r.contest_id=? AND a.id=?",(contest["id"],aid)).fetchone()
    if not account: abort(404)
    data=_certificate_pdf(contest,account["display_name"],"Certificate of Participation")
    audit(conn,"certificate.participation",contest_id=contest["id"],object_type="account",object_id=aid); conn.commit()
    return send_file(io.BytesIO(data),as_attachment=True,download_name=f"participation-{re.sub(r'[^A-Za-z0-9]+','-',account['display_name']).strip('-')}.pdf",mimetype="application/pdf")


@bp.get("/director/certificates/participation.zip")
@_staff_permission("credentials")
def participation_certificates_zip():
    core=_core(); conn=core.get_db(); contest=core.active_contest(conn)
    accounts=conn.execute("SELECT a.* FROM registrations r JOIN accounts a ON a.id=r.account_id WHERE r.contest_id=? AND COALESCE(r.disqualified,0)=0 AND COALESCE(r.registration_status,'REGISTERED') NOT IN ('WITHDRAWN','DNS') ORDER BY a.display_name",(contest["id"],)).fetchall()
    buf=io.BytesIO()
    with zipfile.ZipFile(buf,"w",zipfile.ZIP_DEFLATED) as z:
        for a in accounts:
            name=re.sub(r"[^A-Za-z0-9]+","-",a["display_name"]).strip("-") or str(a["id"])
            z.writestr(f"{name}.pdf",_certificate_pdf(contest,a["display_name"],"Certificate of Participation"))
    buf.seek(0); audit(conn,"certificate.participation_bulk",contest_id=contest["id"],object_type="contest",object_id=contest["id"],detail=f"{len(accounts)} certificates"); conn.commit()
    return send_file(buf,as_attachment=True,download_name=f"{contest['id']}-participation-certificates.zip",mimetype="application/zip")


# ---------- Clone / archive / finalize ----------
@bp.post("/director/contests/<cid>/clone")
@_admin_required
def clone_contest(cid):
    core=_core(); conn=core.get_db(); src=core.contest_by_id(cid,conn)
    if not src: abort(404)
    new_id=request.form.get("new_id","").strip(); new_name=request.form.get("new_name","").strip()
    if not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,35}",new_id or "") or not new_name: flash("Valid new contest ID and name are required.","danger"); return redirect(url_for("admin_contests"))
    try:
        cols=[r[1] for r in conn.execute("PRAGMA table_info(contests)") if r[1] not in {"id"}]; vals=[]
        for col in cols:
            if col=="name" or col=="formal_name": vals.append(new_name)
            elif col=="active": vals.append(0)
            elif col=="created_at": vals.append(core.iso(core.utcnow()))
            elif col=="cloned_from": vals.append(cid)
            elif col in {"scoreboard_revealed","archived"}: vals.append(0)
            elif col in {"scoreboard_thawed_at","finalized_at","paused_at"}: vals.append(None)
            elif col=="lifecycle_phase": vals.append("draft")
            elif col=="start_at": vals.append(None)
            else: vals.append(src[col])
        conn.execute(f"INSERT INTO contests(id,{','.join(cols)}) VALUES(? ,{','.join('?' for _ in cols)})",[new_id,*vals])
        probs=conn.execute("SELECT * FROM problems WHERE contest_id=? ORDER BY ordinal",(cid,)).fetchall()
        for p in probs:
            pcols=[r[1] for r in conn.execute("PRAGMA table_info(problems)") if r[1] not in {"id","contest_id"}]; cur=conn.execute(f"INSERT INTO problems(contest_id,{','.join(pcols)}) VALUES(? ,{','.join('?' for _ in pcols)})",[new_id,*[p[c] for c in pcols]]); newp=conn.execute("SELECT * FROM problems WHERE id=?",(cur.lastrowid,)).fetchone(); src_td=core.problem_tests_dir(p); dst_td=core.problem_tests_dir(newp)
            if src_td.exists(): shutil.copytree(src_td,dst_td,dirs_exist_ok=True)
            if (src_td.parent/"images").is_dir(): shutil.copytree(src_td.parent/"images",dst_td.parent/"images",dirs_exist_ok=True)
        audit(conn,"contest.clone",contest_id=new_id,object_type="contest",object_id=new_id,detail=f"Cloned from {cid}"); conn.commit(); flash("Contest cloned with problems and judge data. Contestants and submissions were not copied.","success")
    except Exception as exc:
        conn.rollback(); flash(f"Clone failed: {exc}","danger")
    return redirect(url_for("admin_contests"))


@bp.post("/director/contests/<cid>/lifecycle")
@_admin_required
def contest_lifecycle(cid):
    core=_core(); conn=core.get_db(); c=core.contest_by_id(cid,conn)
    if not c: abort(404)
    action=request.form.get("action","")
    if action=="finalize": conn.execute("UPDATE contests SET lifecycle_phase='finalized',finalized_at=?,scoreboard_revealed=1 WHERE id=?",(core.iso(core.utcnow()),cid)); msg="Contest finalized."
    elif action=="archive": conn.execute("UPDATE contests SET lifecycle_phase='archived',archived=1,active=0 WHERE id=?",(cid,)); msg="Contest archived and made read-only."
    elif action=="unarchive": conn.execute("UPDATE contests SET lifecycle_phase='draft',archived=0 WHERE id=?",(cid,)); msg="Contest returned to draft."
    else: abort(400)
    audit(conn,f"contest.{action}",contest_id=cid,object_type="contest",object_id=cid); conn.commit()
    if action=="finalize":
        try: create_backup("post-finalization",core.current_user()["id"],cid)
        except Exception as exc: flash(f"Contest finalized, but the automatic final backup failed: {exc}","warning")
    flash(msg,"success"); return redirect(url_for("admin_contests"))


# ---------- Progressive scoreboard resolution ----------
def build_reveal_queue(conn, contest):
    core=_core(); st=core.contest_state(contest); cutoff=st["freeze_at"]; frozen_rows,_=core.standings(contest,cutoff); conn.execute("DELETE FROM reveal_queue WHERE contest_id=?",(contest["id"],))
    order=0
    for r in sorted([x for x in frozen_rows if not x.get("disqualified")],key=lambda x:(x.get("rank") or 10**9),reverse=True):
        order+=1; ids=r["participant"]["account_ids"]; marks=','.join('?' for _ in ids); pending=conn.execute(f"SELECT COUNT(*) FROM submissions WHERE contest_id=? AND account_id IN ({marks}) AND submitted_at>?",[contest["id"],*ids,core.iso(cutoff)]).fetchone()[0]; conn.execute("INSERT INTO reveal_queue(contest_id,entity_id,entity_name,original_rank,pending_count,reveal_order) VALUES(?,?,?,?,?,?)",(contest["id"],r["participant"]["id"],r["participant"]["display_name"],r["rank"],pending,order))
    conn.commit()


@bp.route("/director/reveal", methods=["GET", "POST"])
@_staff_permission("awards")
def progressive_reveal():
    core=_core(); conn=core.get_db(); contest=core.active_contest(conn)
    if request.method=="POST":
        action=request.form.get("action","")
        if action=="prepare": build_reveal_queue(conn,contest); flash("Reveal queue prepared from the frozen scoreboard, bottom rank first.","success")
        elif action=="next":
            row=conn.execute("SELECT * FROM reveal_queue WHERE contest_id=? AND revealed=0 ORDER BY reveal_order LIMIT 1",(contest["id"],)).fetchone()
            if row: conn.execute("UPDATE reveal_queue SET revealed=1,revealed_at=? WHERE id=?",(core.iso(core.utcnow()),row["id"])); conn.commit(); flash(f"Resolved {row['entity_name']}.","success")
            else: flash("All queued entries are resolved.","success")
        elif action=="finish": conn.execute("UPDATE contests SET scoreboard_revealed=1,scoreboard_thawed_at=? WHERE id=?",(core.iso(core.utcnow()),contest["id"])); conn.commit(); flash("Final scoreboard revealed.","success")
        return redirect(url_for("v1.progressive_reveal"))
    queue=conn.execute("SELECT * FROM reveal_queue WHERE contest_id=? ORDER BY reveal_order",(contest["id"],)).fetchall(); rows,problems=core.standings(contest); return render_template("v1_reveal.html",queue=queue,rows=rows,problems=problems)


# ---------- Public presentation modes ----------
def _mixed_reveal_standings(conn, contest):
    core=_core(); st=core.contest_state(contest)
    frozen_rows,problems=core.standings(contest,st["freeze_at"])
    final_rows,_=core.standings(contest)
    queue=conn.execute("SELECT * FROM reveal_queue WHERE contest_id=? ORDER BY reveal_order",(contest["id"],)).fetchall()
    if not queue:
        return frozen_rows,problems,0,0
    revealed={q["entity_id"] for q in queue if q["revealed"]}
    final_by={r["participant"]["id"]:r for r in final_rows}
    mixed=[]
    for fr in frozen_rows:
        eid=fr["participant"]["id"]
        mixed.append(final_by.get(eid,fr) if eid in revealed else fr)
    # Entities with no frozen row (rare: late-added participant) remain final only after resolved.
    present={r["participant"]["id"] for r in mixed}
    for r in final_rows:
        eid=r["participant"]["id"]
        if eid not in present and eid in revealed: mixed.append(r)
    scoring=contest["scoring_model"]
    if scoring=="icpc": key=lambda r:(1 if r.get("disqualified") else 0,-r.get("solved",0),r.get("penalty",0),r.get("last_solve") if r.get("last_solve") is not None else 10**9,r["participant"]["display_name"].casefold())
    else: key=lambda r:(1 if r.get("disqualified") else 0,-float(r.get("points",0) or 0),-r.get("solved",0),r.get("penalty",0),r["participant"]["display_name"].casefold())
    mixed.sort(key=key)
    rank=0
    for r in mixed:
        if r.get("disqualified"): r["rank"]=None
        else: rank+=1; r["rank"]=rank
        r["resolution_revealed"]=r["participant"]["id"] in revealed
    return mixed,problems,len(revealed),len(queue)


@bp.get("/scoreboard/<mode>")
def scoreboard_mode(mode):
    if mode not in {"projector","fullscreen","awarding"}: abort(404)
    core=_core(); conn=core.get_db(); contest=core.active_contest(conn); st=core.contest_state(contest); frozen=st["phase"] in {"FROZEN","ENDED"} and not st["revealed"]
    revealed_count=total_queue=0
    if mode=="awarding" and frozen:
        rows,problems,revealed_count,total_queue=_mixed_reveal_standings(conn,contest)
    else:
        cutoff=st["freeze_at"] if frozen else None; rows,problems=core.standings(contest,cutoff)
    return render_template("v1_scoreboard_mode.html",mode=mode,rows=rows,problems=problems,frozen=frozen,revealed_count=revealed_count,total_queue=total_queue,contest=contest,cstate=st)


# ---------- Contest exports / public info / archive ----------
@bp.get("/contests")
def public_contest_archive():
    core=_core(); conn=core.get_db(); rows=conn.execute("SELECT * FROM contests WHERE archived=1 OR lifecycle_phase IN ('finalized','archived') ORDER BY COALESCE(start_at,created_at) DESC").fetchall(); return render_template("v1_archive.html",contests=rows)


@bp.get("/contest/<cid>")
def public_contest_page(cid):
    core=_core(); conn=core.get_db(); c=core.contest_by_id(cid,conn)
    if not c: abort(404)
    probs=core.contest_problems(cid,conn); anns=conn.execute("SELECT * FROM announcements WHERE contest_id=? AND audience='all' ORDER BY pinned DESC,id DESC LIMIT 20",(cid,)).fetchall()
    final_rows=[]; problem_stats=[]
    if c["lifecycle_phase"] in {"finalized","archived"} or c["archived"]:
        final_rows,_=core.standings(c)
        problem_stats=conn.execute("""SELECT p.code,p.title,COUNT(s.id) submissions,SUM(CASE WHEN s.verdict='AC' THEN 1 ELSE 0 END) accepted,COUNT(DISTINCT CASE WHEN s.verdict='AC' THEN s.account_id END) solvers FROM problems p LEFT JOIN submissions s ON s.problem_id=p.id WHERE p.contest_id=? GROUP BY p.id ORDER BY p.ordinal""",(cid,)).fetchall()
    return render_template("v1_public_contest.html",view_contest=c,view_state=core.contest_state(c),problems=probs,announcements=anns,scope_label=contest_scope_label(c["scope"]),final_rows=final_rows,problem_stats=problem_stats,supported_languages=[x.label for x in core.LANGUAGES.values()])


# ---------- Contest snapshots / stats JSON ----------
@bp.post("/director/snapshot")
@_admin_required
def snapshot():
    core=_core(); conn=core.get_db(); contest=core.active_contest(conn); rows,probs=core.standings(contest); payload={"contest":dict(contest),"rows":[{"rank":r["rank"],"name":r["participant"]["display_name"],"solved":r["solved"],"penalty":r["penalty"]} for r in rows],"created_at":core.iso(core.utcnow())}; conn.execute("INSERT INTO contest_snapshots(contest_id,snapshot_type,data_json,created_at) VALUES(?,?,?,?)",(contest["id"],request.form.get("snapshot_type","manual"),json.dumps(payload),core.iso(core.utcnow()))); conn.commit(); flash("Contest snapshot saved.","success"); return redirect(request.referrer or url_for("v1.analytics"))


# ---------- Helper for disqualification history ----------
def record_disqualification_history(conn, contest, account_id: int, action: str, reason: str, actor_id: int | None, evidence_notes: str = ""):
    core=_core(); reg=conn.execute("SELECT team_name FROM registrations WHERE contest_id=? AND account_id=?",(contest["id"],account_id)).fetchone(); conn.execute("INSERT INTO disqualification_history(contest_id,account_id,team_name,action,reason,evidence_notes,actor_id,created_at) VALUES(?,?,?,?,?,?,?,?)",(contest["id"],account_id,(reg["team_name"] if reg else "") or "",action,reason,evidence_notes,actor_id,core.iso(core.utcnow())))
    audit(conn,f"disqualification.{action.lower()}",contest_id=contest["id"],object_type="account",object_id=account_id,detail=reason)


# ---------- Team-size helper ----------
def team_size_ok(conn, contest, team_name: str, excluding_account: int | None = None) -> tuple[bool,int]:
    if not team_name.strip(): return True,0
    params=[contest["id"],team_name.strip()]; q="SELECT COUNT(*) FROM registrations WHERE contest_id=? AND lower(trim(team_name))=lower(trim(?))"
    if excluding_account is not None: q+=" AND account_id<>?"; params.append(excluding_account)
    n=conn.execute(q,params).fetchone()[0]; return n < max(1,int(contest["max_team_size"])), n
