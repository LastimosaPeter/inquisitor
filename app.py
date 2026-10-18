from __future__ import annotations

import base64
import hashlib
import hmac
import io
import json
import os
import re
import secrets
import sqlite3
import shutil
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from datetime import datetime, timedelta, timezone
from functools import wraps
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from flask import (
    Flask, abort, flash, g, jsonify, make_response, redirect, render_template,
    request, send_file, session, url_for
)
from werkzeug.security import check_password_hash, generate_password_hash
from werkzeug.middleware.proxy_fix import ProxyFix

from judge import LANGUAGES, compiler_status, detect_language_from_filename, judge_backend, judge_submission
import features_v1 as fx

BASE_DIR = Path(__file__).resolve().parent
# Vercel presentation mode uses the only writable area available to the
# serverless function. The database and generated judge/test files are
# intentionally ephemeral: this build is for UI/workflow demonstrations only.
VERCEL_DEMO = os.environ.get("VERCEL") == "1" or os.environ.get("INQUISITOR_VERCEL_DEMO", "0") == "1"
_default_data_dir = "/tmp/inquisitor-demo" if VERCEL_DEMO else str(BASE_DIR)
DATA_DIR = Path(os.environ.get("INQUISITOR_DATA_DIR", _default_data_dir)).resolve()
DATA_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = DATA_DIR / "inquisitor.db"
PROBLEMS_DIR = DATA_DIR / "problems"
PROBLEMS_DIR.mkdir(parents=True, exist_ok=True)
UTC = timezone.utc
try:
    PHT = ZoneInfo("Asia/Manila")
except ZoneInfoNotFoundError:
    # Windows does not ship the IANA time-zone database by default.
    # Asia/Manila is permanently UTC+08:00, so this fallback keeps the
    # local development server usable even before tzdata is installed.
    PHT = timezone(timedelta(hours=8), name="Asia/Manila")
ID_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]{0,35}$")
HEX_COLOR_RE = re.compile(r"^#[0-9A-Fa-f]{6}$")
LOGIN_ATTEMPTS: dict[str, list[float]] = {}
LOGIN_WINDOW_SECONDS = 10 * 60
LOGIN_MAX_FAILURES = 8

app = Flask(__name__)
if os.environ.get("INQUISITOR_TRUST_PROXY", "0") == "1":
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_port=1)
app.config.update(
    SECRET_KEY=os.environ.get("INQUISITOR_SECRET", "change-this-before-a-real-contest"),
    MAX_CONTENT_LENGTH=16 * 1024 * 1024,
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=os.environ.get("INQUISITOR_SECURE_COOKIES", "0") == "1",
)


def utcnow() -> datetime:
    return datetime.now(UTC)


def iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    return dt.astimezone(UTC).isoformat(timespec="milliseconds")


def parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def reltime(seconds: float) -> str:
    sign = "-" if seconds < 0 else ""
    total = int(abs(seconds))
    hours, rem = divmod(total, 3600)
    minutes, secs = divmod(rem, 60)
    return f"{sign}{hours}:{minutes:02d}:{secs:02d}"


def local_input_to_utc(value: str) -> str:
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=PHT)
    return iso(dt.astimezone(UTC))


def db_connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def get_db() -> sqlite3.Connection:
    if "db" not in g:
        g.db = db_connect()
    return g.db


@app.teardown_appcontext
def close_db(_exc):
    conn = g.pop("db", None)
    if conn is not None:
        conn.close()


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone() is not None


def _table_columns(conn: sqlite3.Connection, name: str) -> set[str]:
    if not _table_exists(conn, name):
        return set()
    return {row["name"] for row in conn.execute(f'PRAGMA table_info("{name}")')}


def _legacy_schema_present(conn: sqlite3.Connection) -> bool:
    """Detect the v0.1 database layout (or the mixed schema left by v0.2.0/0.2.1)."""
    checks = {
        "problems": "contest_id",
        "submissions": "contest_id",
        "clarifications": "contest_id",
    }
    return any(
        _table_exists(conn, table) and required not in _table_columns(conn, table)
        for table, required in checks.items()
    )


def _backup_database(conn: sqlite3.Connection) -> Path:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    path = DB_PATH.with_name(f"inquisitor-pre-v1.0.7-{stamp}.db")
    backup = sqlite3.connect(path)
    try:
        conn.backup(backup)
    finally:
        backup.close()
    return path


def _prepare_legacy_upgrade(conn: sqlite3.Connection) -> bool:
    """
    Rename incompatible v0.1 tables before creating the current schema.

    v0.2.0/0.2.1 could create some new tables and then fail while creating an
    index on the old submissions table.  Therefore this routine also removes
    only those partially-created v0.2 tables before rebuilding them cleanly.
    """
    if not _legacy_schema_present(conn):
        return False

    backup_path = _backup_database(conn)
    print(f"Legacy Inquisitor database detected. Backup created: {backup_path.name}")

    conn.execute("PRAGMA foreign_keys=OFF")
    conn.commit()

    # Tables that only belong to the new schema may have been created before a
    # previous startup failed. They contain no usable migrated data at that
    # point, so rebuild them after preserving the v0.1 tables below.
    for table in ("api_events", "registrations", "accounts", "contests"):
        if _table_exists(conn, table):
            conn.execute(f'DROP TABLE "{table}"')

    # Preserve the v0.1 content under explicit legacy names.  The old singular
    # contest table and teams table do not conflict with the current names, but
    # renaming them keeps the migration self-contained and prevents accidental
    # use later.
    for table in ("contest", "teams", "problems", "submissions", "clarifications"):
        legacy = f"legacy_v01_{table}"
        if _table_exists(conn, table):
            if _table_exists(conn, legacy):
                conn.execute(f'DROP TABLE "{legacy}"')
            conn.execute(f'ALTER TABLE "{table}" RENAME TO "{legacy}"')

    conn.commit()
    conn.execute("PRAGMA foreign_keys=ON")
    return True


def _slugify_problem(value: str, fallback: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", (value or "").lower()).strip("-")
    return slug or fallback.lower()


def _migrate_legacy_data(conn: sqlite3.Connection) -> None:
    if not _table_exists(conn, "legacy_v01_teams"):
        return

    now = utcnow()
    contest_id = "acpc-demo"
    old_contest = (
        conn.execute("SELECT * FROM legacy_v01_contest WHERE id=1").fetchone()
        if _table_exists(conn, "legacy_v01_contest") else None
    )
    if old_contest:
        start = parse_dt(old_contest["start_at"])
        end = parse_dt(old_contest["end_at"])
        duration = 120
        if start and end:
            duration = max(1, int((end - start).total_seconds() // 60))
        conn.execute(
            """INSERT OR REPLACE INTO contests(
                   id,name,formal_name,location,start_at,duration_minutes,
                   freeze_minutes,penalty_minutes,scoreboard_revealed,
                   scoreboard_thawed_at,active,created_at
               ) VALUES(?,?,?,?,?,?,?,?,?,?,1,?)""",
            (
                contest_id,
                old_contest["title"],
                old_contest["title"],
                "Ateneo de Naga University",
                old_contest["start_at"],
                duration,
                old_contest["freeze_minutes"],
                20,
                old_contest["scoreboard_revealed"],
                None,
                iso(now),
            ),
        )

    # Preserve old usernames/password hashes and IDs so historical submission
    # ownership remains intact.  v0.1 was team-based; v0.2+ is individual-first,
    # so each old non-admin team becomes one participant account.
    for row in conn.execute("SELECT * FROM legacy_v01_teams ORDER BY id"):
        role = "judge" if row["is_admin"] else "participant"
        conn.execute(
            """INSERT OR REPLACE INTO accounts(
                   id,username,password_hash,display_name,affiliation,role,created_at
               ) VALUES(?,?,?,?,?,?,?)""",
            (
                row["id"], row["username"], row["password_hash"], row["name"],
                row["school"], role, iso(now)
            ),
        )
        if role == "participant":
            conn.execute(
                "INSERT OR REPLACE INTO registrations(contest_id,account_id,team_api_id) VALUES(?,?,?)",
                (contest_id, row["id"], f"p-{row['id']}")
            )

    # Always provide a dedicated administrator account for the new Admin view.
    if not conn.execute("SELECT 1 FROM accounts WHERE username='admin'").fetchone():
        conn.execute(
            "INSERT INTO accounts(username,password_hash,display_name,affiliation,role,created_at) VALUES(?,?,?,?,?,?)",
            (
                "admin", generate_password_hash("inquisitor"), "Contest Director",
                "TACTICS x ACPC 2026", "admin", iso(now)
            ),
        )

    if _table_exists(conn, "legacy_v01_problems"):
        for row in conn.execute("SELECT * FROM legacy_v01_problems ORDER BY id"):
            api_id = _slugify_problem(row["title"], row["code"])
            conn.execute(
                """INSERT OR REPLACE INTO problems(
                       id,contest_id,api_id,code,title,statement,sample_input,sample_output,
                       time_limit_ms,memory_limit_mb,output_limit_mb,code_limit_kb,ordinal
                   ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    row["id"], contest_id, api_id, row["code"], row["title"], row["statement"],
                    "", "", row["time_limit_ms"], row["memory_limit_mb"], 8, 128, row["id"]
                ),
            )

    if _table_exists(conn, "legacy_v01_submissions"):
        for row in conn.execute("SELECT * FROM legacy_v01_submissions ORDER BY id"):
            judged_at = row["submitted_at"] if row["status"] == "DONE" else None
            conn.execute(
                """INSERT OR REPLACE INTO submissions(
                       id,contest_id,account_id,problem_id,language,source,entry_point,
                       submitted_at,judging_started_at,judged_at,status,verdict,runtime_ms,detail
                   ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    row["id"], contest_id, row["team_id"], row["problem_id"], row["language"],
                    row["source"], None, row["submitted_at"], None, judged_at, row["status"],
                    row["verdict"], row["runtime_ms"], row["detail"] or ""
                ),
            )

    if _table_exists(conn, "legacy_v01_clarifications"):
        for row in conn.execute("SELECT * FROM legacy_v01_clarifications ORDER BY id"):
            conn.execute(
                """INSERT OR REPLACE INTO clarifications(
                       id,contest_id,account_id,problem_id,question,answer,status,created_at,answered_at
                   ) VALUES(?,?,?,?,?,?,?,?,?)""",
                (
                    row["id"], contest_id, row["team_id"], row["problem_id"], row["question"],
                    row["answer"], row["status"], row["created_at"], row["answered_at"]
                ),
            )

    conn.commit()
    print("Legacy v0.1 data migrated to the individual-first v0.2 schema.")


def _ensure_v028_columns(conn: sqlite3.Connection) -> None:
    """Apply additive schema changes to any existing v0.2.x database."""
    contest_cols = _table_columns(conn, "contests")
    if "competition_mode" not in contest_cols:
        conn.execute("ALTER TABLE contests ADD COLUMN competition_mode TEXT NOT NULL DEFAULT 'individual'")
    if "signup_open" not in contest_cols:
        conn.execute("ALTER TABLE contests ADD COLUMN signup_open INTEGER NOT NULL DEFAULT 1")
    if "projector_autoscroll" not in contest_cols:
        conn.execute("ALTER TABLE contests ADD COLUMN projector_autoscroll INTEGER NOT NULL DEFAULT 0")
    reg_cols = _table_columns(conn, "registrations")
    if "team_name" not in reg_cols:
        conn.execute("ALTER TABLE registrations ADD COLUMN team_name TEXT NOT NULL DEFAULT ''")
    account_cols = _table_columns(conn, "accounts")
    if "department" not in account_cols:
        conn.execute("ALTER TABLE accounts ADD COLUMN department TEXT NOT NULL DEFAULT ''")
    if "email" not in account_cols:
        conn.execute("ALTER TABLE accounts ADD COLUMN email TEXT NOT NULL DEFAULT ''")
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS ix_accounts_email_unique ON accounts(email) WHERE email <> ''")
    problem_cols = _table_columns(conn, "problems")
    if "contributors" not in problem_cols:
        conn.execute("ALTER TABLE problems ADD COLUMN contributors TEXT NOT NULL DEFAULT ''")
    if "color" not in problem_cols:
        conn.execute("ALTER TABLE problems ADD COLUMN color TEXT NOT NULL DEFAULT '#5bc98b'")
    reg_cols = _table_columns(conn, "registrations")
    if "disqualified" not in reg_cols:
        conn.execute("ALTER TABLE registrations ADD COLUMN disqualified INTEGER NOT NULL DEFAULT 0")
    if "disqualification_reason" not in reg_cols:
        conn.execute("ALTER TABLE registrations ADD COLUMN disqualification_reason TEXT NOT NULL DEFAULT ''")
    if "disqualified_at" not in reg_cols:
        conn.execute("ALTER TABLE registrations ADD COLUMN disqualified_at TEXT")
    if "disqualified_by" not in reg_cols:
        conn.execute("ALTER TABLE registrations ADD COLUMN disqualified_by INTEGER")
    clar_cols = _table_columns(conn, "clarifications")
    if "request_type" not in clar_cols:
        conn.execute("ALTER TABLE clarifications ADD COLUMN request_type TEXT NOT NULL DEFAULT 'QUESTION'")
    if "broadcast" not in clar_cols:
        conn.execute("ALTER TABLE clarifications ADD COLUMN broadcast INTEGER NOT NULL DEFAULT 0")
    if "broadcast_at" not in clar_cols:
        conn.execute("ALTER TABLE clarifications ADD COLUMN broadcast_at TEXT")
    conn.commit()


def init_db():
    conn = db_connect()
    upgraded_legacy = _prepare_legacy_upgrade(conn)
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS contests (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            formal_name TEXT NOT NULL,
            location TEXT NOT NULL DEFAULT '',
            start_at TEXT,
            duration_minutes INTEGER NOT NULL DEFAULT 180,
            freeze_minutes INTEGER NOT NULL DEFAULT 10,
            penalty_minutes INTEGER NOT NULL DEFAULT 20,
            competition_mode TEXT NOT NULL DEFAULT 'individual' CHECK(competition_mode IN ('individual','team')),
            signup_open INTEGER NOT NULL DEFAULT 1,
            projector_autoscroll INTEGER NOT NULL DEFAULT 0,
            scoreboard_revealed INTEGER NOT NULL DEFAULT 0,
            scoreboard_thawed_at TEXT,
            active INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS accounts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT NOT NULL UNIQUE,
            email TEXT NOT NULL DEFAULT '',
            password_hash TEXT NOT NULL,
            display_name TEXT NOT NULL,
            affiliation TEXT NOT NULL DEFAULT '',
            department TEXT NOT NULL DEFAULT '',
            role TEXT NOT NULL CHECK(role IN ('participant','judge','admin')),
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS registrations (
            contest_id TEXT NOT NULL REFERENCES contests(id) ON DELETE CASCADE,
            account_id INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
            team_api_id TEXT NOT NULL,
            team_name TEXT NOT NULL DEFAULT '',
            disqualified INTEGER NOT NULL DEFAULT 0,
            disqualification_reason TEXT NOT NULL DEFAULT '',
            disqualified_at TEXT,
            disqualified_by INTEGER,
            PRIMARY KEY(contest_id, account_id),
            UNIQUE(contest_id, team_api_id)
        );

        CREATE TABLE IF NOT EXISTS problems (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            contest_id TEXT NOT NULL REFERENCES contests(id) ON DELETE CASCADE,
            api_id TEXT NOT NULL,
            code TEXT NOT NULL,
            title TEXT NOT NULL,
            statement TEXT NOT NULL,
            contributors TEXT NOT NULL DEFAULT '',
            color TEXT NOT NULL DEFAULT '#5bc98b',
            sample_input TEXT NOT NULL DEFAULT '',
            sample_output TEXT NOT NULL DEFAULT '',
            time_limit_ms INTEGER NOT NULL DEFAULT 2000,
            memory_limit_mb INTEGER NOT NULL DEFAULT 256,
            output_limit_mb INTEGER NOT NULL DEFAULT 8,
            code_limit_kb INTEGER NOT NULL DEFAULT 128,
            ordinal INTEGER NOT NULL DEFAULT 1,
            UNIQUE(contest_id, api_id),
            UNIQUE(contest_id, code)
        );

        CREATE TABLE IF NOT EXISTS submissions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            contest_id TEXT NOT NULL REFERENCES contests(id) ON DELETE CASCADE,
            account_id INTEGER NOT NULL REFERENCES accounts(id),
            problem_id INTEGER NOT NULL REFERENCES problems(id),
            language TEXT NOT NULL,
            source TEXT NOT NULL,
            entry_point TEXT,
            submitted_at TEXT NOT NULL,
            judging_started_at TEXT,
            judged_at TEXT,
            status TEXT NOT NULL DEFAULT 'PENDING',
            verdict TEXT,
            runtime_ms INTEGER,
            detail TEXT NOT NULL DEFAULT ''
        );

        CREATE INDEX IF NOT EXISTS ix_submissions_queue ON submissions(status, id);
        CREATE INDEX IF NOT EXISTS ix_submissions_contest_account ON submissions(contest_id, account_id, problem_id, id);

        CREATE TABLE IF NOT EXISTS clarifications (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            contest_id TEXT NOT NULL REFERENCES contests(id) ON DELETE CASCADE,
            account_id INTEGER NOT NULL REFERENCES accounts(id),
            problem_id INTEGER REFERENCES problems(id),
            question TEXT NOT NULL,
            answer TEXT,
            request_type TEXT NOT NULL DEFAULT 'QUESTION',
            broadcast INTEGER NOT NULL DEFAULT 0,
            status TEXT NOT NULL DEFAULT 'OPEN',
            created_at TEXT NOT NULL,
            answered_at TEXT,
            broadcast_at TEXT
        );

        CREATE TABLE IF NOT EXISTS api_events (
            seq INTEGER PRIMARY KEY AUTOINCREMENT,
            contest_id TEXT NOT NULL,
            type TEXT NOT NULL,
            object_id TEXT,
            data_json TEXT,
            created_at TEXT NOT NULL
        );
        """
    )

    _ensure_v028_columns(conn)
    fx.ensure_schema(conn)

    if upgraded_legacy:
        _migrate_legacy_data(conn)
        conn.execute("UPDATE contests SET lifecycle_phase='running',scope='cross_college',scoring_model='icpc' WHERE id='acpc-demo'")

    if conn.execute("SELECT COUNT(*) FROM contests").fetchone()[0] == 0:
        now = utcnow()
        conn.execute(
            """INSERT INTO contests(id,name,formal_name,location,start_at,duration_minutes,freeze_minutes,penalty_minutes,scoreboard_revealed,active,created_at,scope,scoring_model,lifecycle_phase,submissions_open)
               VALUES(?,?,?,?,?,?,?,?,0,1,?,?,?,?,1)""",
            (
                "acpc-demo", "Ateneo Computer Programming Competition 2026", "Ateneo Computer Programming Competition 2026",
                "Ateneo de Naga University", iso(now - timedelta(minutes=25)), 120, 10, 20, iso(now), "cross_college", "icpc", "running"
            ),
        )

    # Refresh only the historical demo defaults; never overwrite administrator-created contest names.
    conn.execute(
        """UPDATE contests SET name='Ateneo Computer Programming Competition 2026',
                   formal_name='Ateneo Computer Programming Competition 2026'
           WHERE id='acpc-demo' AND (
               name IN (
                   'ACPC Demo Contest',
                   'ACPC Annual Contest 2024',
                   'Ateneo Computer Programming Competition',
                   'ACPC Annual Programming Contest',
                   'TACTICS x ACPC 2026'
               )
               OR formal_name IN (
                   'ACPC Annual Programming Contest',
                   'TACTICS x ACPC 2026'
               )
           )"""
    )
    conn.commit()

    if conn.execute("SELECT COUNT(*) FROM accounts").fetchone()[0] == 0:
        if os.environ.get("INQUISITOR_DEMO_ACCOUNTS", "1") != "0":
            rows = [
                ("contestant", "inquisitor", "Alex Rivera", "Ateneo de Naga University", "participant"),
                ("mika", "mika", "Mika Santos", "Ateneo de Naga University", "participant"),
                ("cruz", "cruz", "John Cruz", "University of Nueva Caceres", "participant"),
                ("judge", "inquisitor", "Judge Console", "ACPC", "judge"),
                ("admin", "inquisitor", "Contest Director", "TACTICS x ACPC 2026", "admin"),
            ]
            conn.executemany(
                "INSERT INTO accounts(username,password_hash,display_name,affiliation,role,created_at) VALUES(?,?,?,?,?,?)",
                [(u, generate_password_hash(pw), n, a, r, iso(utcnow())) for u,pw,n,a,r in rows],
            )
        else:
            username=os.environ.get("INQUISITOR_BOOTSTRAP_ADMIN_USER", "").strip()
            password=os.environ.get("INQUISITOR_BOOTSTRAP_ADMIN_PASSWORD", "")
            display=os.environ.get("INQUISITOR_BOOTSTRAP_ADMIN_NAME", "Contest Director").strip() or "Contest Director"
            if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.-]{2,31}", username or "") or len(password) < 12:
                raise RuntimeError("Secure bootstrap required: set INQUISITOR_BOOTSTRAP_ADMIN_USER and INQUISITOR_BOOTSTRAP_ADMIN_PASSWORD (12+ characters) when demo accounts are disabled.")
            conn.execute(
                "INSERT INTO accounts(username,password_hash,display_name,affiliation,role,created_at) VALUES(?,?,?,?,?,?)",
                (username,generate_password_hash(password),display,"TACTICS x ACPC","admin",iso(utcnow()))
            )

    # Local builds intentionally keep three predictable demo credentials so a
    # copied database cannot leave the documented accounts unusable. Disable
    # this behavior for a real event with INQUISITOR_DEMO_ACCOUNTS=0.
    if os.environ.get("INQUISITOR_DEMO_ACCOUNTS", "1") != "0":
        demo_accounts = [
            ("contestant", "inquisitor", "Alex Rivera", "Ateneo de Naga University", "participant"),
            ("judge", "inquisitor", "Judge Console", "TACTICS x ACPC 2026", "judge"),
            ("admin", "inquisitor", "Contest Director", "TACTICS x ACPC 2026", "admin"),
        ]
        for username, password, display_name, affiliation, role in demo_accounts:
            existing = conn.execute("SELECT id FROM accounts WHERE username=?", (username,)).fetchone()
            password_hash = generate_password_hash(password)
            if existing:
                conn.execute(
                    "UPDATE accounts SET password_hash=?,display_name=?,affiliation=?,role=? WHERE id=?",
                    (password_hash, display_name, affiliation, role, existing["id"])
                )
            else:
                conn.execute(
                    "INSERT INTO accounts(username,password_hash,display_name,affiliation,role,created_at) VALUES(?,?,?,?,?,?)",
                    (username, password_hash, display_name, affiliation, role, iso(utcnow()))
                )
        conn.execute("UPDATE accounts SET department='Department of Computer Science' WHERE username='contestant' AND (department IS NULL OR department='')")

    active_row = conn.execute("SELECT id FROM contests WHERE active=1 LIMIT 1").fetchone()
    if not active_row:
        first = conn.execute("SELECT id FROM contests ORDER BY created_at LIMIT 1").fetchone()
        if not first:
            raise RuntimeError("Inquisitor requires at least one contest.")
        conn.execute("UPDATE contests SET active=1 WHERE id=?", (first["id"],))
        active_row = first
    contest_id = active_row["id"]
    registrations_were_empty = conn.execute(
        "SELECT COUNT(*) FROM registrations WHERE contest_id=?", (contest_id,)
    ).fetchone()[0] == 0

    if os.environ.get("INQUISITOR_DEMO_ACCOUNTS", "1") != "0":
        demo_participant = conn.execute("SELECT id FROM accounts WHERE username='contestant'").fetchone()
        if demo_participant and not conn.execute(
            "SELECT 1 FROM registrations WHERE contest_id=? AND account_id=?",
            (contest_id, demo_participant["id"])
        ).fetchone():
            conn.execute(
                "INSERT INTO registrations(contest_id,account_id,team_api_id,team_name) VALUES(?,?,?,?)",
                (contest_id, demo_participant["id"], f"p-{demo_participant['id']}", "")
            )

    if registrations_were_empty:
        for acct in conn.execute("SELECT * FROM accounts WHERE role='participant' ORDER BY id"):
            conn.execute(
                "INSERT OR IGNORE INTO registrations(contest_id,account_id,team_api_id) VALUES(?,?,?)",
                (contest_id, acct["id"], f"p-{acct['id']}")
            )

    if conn.execute("SELECT COUNT(*) FROM problems WHERE contest_id=?", (contest_id,)).fetchone()[0] == 0:
        demo = [
            ("array-restoration", "A", "Array Restoration", "Given n integers, print them in reverse order.", "#d9b45f", "5\n1 2 3 4 5\n", "5 4 3 2 1\n", 1500, 256, 1),
            ("orbital-checkpoints", "B", "Orbital Checkpoints", "Count how many values are divisible by k.", "#7aa2d6", "5 3\n3 4 6 8 9\n", "3\n", 1500, 256, 2),
            ("signal-sum", "C", "Signal Sum", "Read n signed integers and print their sum.", "#70b98a", "5\n1 -2 3 4 5\n", "11\n", 1500, 256, 3),
            ("light-delay", "D", "Light Delay", "Given a distance in kilometers, compute one-way light travel time in milliseconds using 299792.458 km/s. Print exactly 3 decimal places.", "#b38bd1", "299792.458\n", "1000.000\n", 1500, 256, 4),
            ("prime-relay", "E", "Prime Relay", "Print PRIME if n is prime; otherwise print COMPOSITE.", "#cf7f72", "49\n", "COMPOSITE\n", 2000, 256, 5),
        ]
        conn.executemany(
            """INSERT INTO problems(contest_id,api_id,code,title,statement,color,sample_input,sample_output,time_limit_ms,memory_limit_mb,ordinal)
               VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
            [(contest_id, *r) for r in demo]
        )

    conn.commit()
    conn.close()
    seed_demo_tests()


def seed_demo_tests():
    tests = {
        "array-restoration": [("5\n1 2 3 4 5\n", "5 4 3 2 1\n"), ("1\n42\n", "42\n"), ("6\n-2 0 7 7 9 11\n", "11 9 7 7 0 -2\n")],
        "orbital-checkpoints": [("5 3\n3 4 6 8 9\n", "3\n"), ("4 5\n1 2 3 4\n", "0\n"), ("6 2\n2 4 6 7 9 10\n", "4\n")],
        "signal-sum": [("5\n1 -2 3 4 5\n", "11\n"), ("3\n-10 -20 5\n", "-25\n"), ("1\n2000000000\n", "2000000000\n")],
        "light-delay": [("299792.458\n", "1000.000\n"), ("149896.229\n", "500.000\n"), ("384400\n", "1282.220\n")],
        "prime-relay": [("2\n", "PRIME\n"), ("49\n", "COMPOSITE\n"), ("2147483647\n", "PRIME\n")],
    }
    root = PROBLEMS_DIR / "acpc-demo"
    for pid, cases in tests.items():
        td = root / pid / "tests"
        td.mkdir(parents=True, exist_ok=True)
        if any(td.glob("input*.txt")):
            continue
        for i, (inp, out) in enumerate(cases, start=1):
            (td / f"input{i:02d}.txt").write_text(inp, encoding="utf-8")
            (td / f"output{i:02d}.txt").write_text(out, encoding="utf-8")


def current_user():
    uid = session.get("user_id")
    if not uid:
        return None
    return get_db().execute("SELECT * FROM accounts WHERE id=?", (uid,)).fetchone()


def role_required(*roles):
    def deco(fn):
        @wraps(fn)
        def wrapper(*args, **kwargs):
            user = current_user()
            if not user:
                return redirect(url_for("login", next=request.path))
            if user["role"] not in roles:
                abort(403)
            return fn(*args, **kwargs)
        return wrapper
    return deco


participant_required = role_required("participant")
judge_required = role_required("judge", "admin")
admin_required = role_required("admin")


def active_contest(conn=None):
    conn = conn or get_db()
    return conn.execute("SELECT * FROM contests WHERE active=1 ORDER BY created_at DESC LIMIT 1").fetchone() or conn.execute("SELECT * FROM contests ORDER BY created_at DESC LIMIT 1").fetchone()


def contest_by_id(cid: str, conn=None):
    conn = conn or get_db()
    return conn.execute("SELECT * FROM contests WHERE id=?", (cid,)).fetchone()


def contest_times(contest):
    start = parse_dt(contest["start_at"])
    if start is None:
        return None, None, None
    end = start + timedelta(minutes=contest["duration_minutes"])
    freeze_at = end - timedelta(minutes=contest["freeze_minutes"])
    return start, end, freeze_at


def contest_state(contest):
    now = utcnow()
    start, end, freeze_at = contest_times(contest)
    lifecycle = (contest["lifecycle_phase"] if "lifecycle_phase" in contest.keys() else "") or ""
    lifecycle = lifecycle.lower()
    if lifecycle == "paused":
        phase = "PAUSED"
        remaining = max(0, int((end - now).total_seconds())) if end else contest["duration_minutes"] * 60
    elif lifecycle == "draft":
        phase = "DRAFT"
        remaining = contest["duration_minutes"] * 60
    elif lifecycle == "registration":
        phase = "REGISTRATION"
        remaining = max(0, int((start - now).total_seconds())) if start and now < start else contest["duration_minutes"] * 60
    elif lifecycle == "registration_closed":
        phase = "REGISTRATION CLOSED"
        remaining = max(0, int((start - now).total_seconds())) if start and now < start else contest["duration_minutes"] * 60
    elif lifecycle == "practice" or ("practice_mode" in contest.keys() and contest["practice_mode"]):
        phase = "PRACTICE"
        remaining = max(0, int((end - now).total_seconds())) if end else contest["duration_minutes"] * 60
    elif lifecycle == "ended":
        phase = "ENDED"
        remaining = 0
    elif lifecycle == "finalized":
        phase = "FINALIZED"
        remaining = 0
    elif lifecycle == "archived" or ("archived" in contest.keys() and contest["archived"]):
        phase = "ARCHIVED"
        remaining = 0
    elif not start:
        phase = "SCHEDULED"
        remaining = contest["duration_minutes"] * 60
    elif now < start:
        phase = "UPCOMING"
        remaining = int((start - now).total_seconds())
    elif now >= end:
        phase = "ENDED"
        remaining = 0
    elif freeze_at and now >= freeze_at:
        phase = "FROZEN"
        remaining = max(0, int((end - now).total_seconds()))
    else:
        phase = "RUNNING"
        remaining = max(0, int((end - now).total_seconds()))
    return {
        "phase": phase, "start": start, "end": end, "freeze_at": freeze_at,
        "remaining": remaining, "revealed": bool(contest["scoreboard_revealed"])
    }

def participant_registered(contest_id: str, account_id: int, conn=None):
    conn = conn or get_db()
    return conn.execute("SELECT * FROM registrations WHERE contest_id=? AND account_id=?", (contest_id, account_id)).fetchone()


def registration_scope(contest, account_id: int, conn=None):
    """Return registration rows affected by team/individual administrative actions."""
    conn = conn or get_db()
    reg = participant_registered(contest["id"], account_id, conn)
    if not reg:
        return []
    if contest["competition_mode"] == "team" and (reg["team_name"] or "").strip():
        return conn.execute(
            """SELECT * FROM registrations
               WHERE contest_id=? AND lower(trim(team_name))=lower(trim(?))
               ORDER BY account_id""",
            (contest["id"], reg["team_name"]),
        ).fetchall()
    return [reg]


def set_disqualification(contest, account_id: int, *, disqualified: bool, reason: str = "", evidence_notes: str = "", actor_id: int | None = None, conn=None):
    conn = conn or get_db()
    scope = registration_scope(contest, account_id, conn)
    if not scope:
        return 0
    ids = [r["account_id"] for r in scope]
    marks = ",".join("?" for _ in ids)
    if disqualified:
        reason = reason.strip()[:600]
        if not reason:
            raise ValueError("A disqualification reason is required.")
        conn.execute(
            f"""UPDATE registrations
                 SET disqualified=1,disqualification_reason=?,disqualified_at=?,disqualified_by=?,registration_status='DISQUALIFIED'
                 WHERE contest_id=? AND account_id IN ({marks})""",
            (reason, iso(utcnow()), actor_id, contest["id"], *ids),
        )
        for aid in ids:
            fx.record_disqualification_history(conn, contest, aid, "DISQUALIFY", reason, actor_id, evidence_notes=evidence_notes)
    else:
        conn.execute(
            f"""UPDATE registrations
                 SET disqualified=0,disqualification_reason='',disqualified_at=NULL,disqualified_by=NULL,
                     registration_status=CASE WHEN registration_status='DISQUALIFIED' THEN 'ACTIVE' ELSE registration_status END
                 WHERE contest_id=? AND account_id IN ({marks})""",
            (contest["id"], *ids),
        )
        for aid in ids:
            fx.record_disqualification_history(conn, contest, aid, "REINSTATE", "Reinstated by contest official.", actor_id)
    return len(ids)


def contest_problems(contest_id: str, conn=None):
    conn = conn or get_db()
    return conn.execute("SELECT * FROM problems WHERE contest_id=? ORDER BY ordinal, code", (contest_id,)).fetchall()


def problem_tests_dir(problem) -> Path:
    return PROBLEMS_DIR / problem["contest_id"] / problem["api_id"] / "tests"


def count_tests(problem) -> int:
    return len(list(problem_tests_dir(problem).glob("input*.txt")))


def _team_api_id_from_name(team_name: str) -> str:
    normalized = " ".join(team_name.strip().split()).casefold()
    slug = re.sub(r"[^a-z0-9]+", "-", normalized).strip("-")[:24] or "team"
    digest = hashlib.sha1(normalized.encode("utf-8")).hexdigest()[:8]
    return f"team-{slug}-{digest}"


def _score_entities(contest, conn):
    members = conn.execute(
        """SELECT a.*, r.team_api_id, COALESCE(r.team_name,'') team_name,
                  COALESCE(r.disqualified,0) disqualified,
                  COALESCE(r.disqualification_reason,'') disqualification_reason
           FROM registrations r JOIN accounts a ON a.id=r.account_id
           WHERE r.contest_id=? ORDER BY a.display_name""", (contest["id"],)
    ).fetchall()
    if contest["competition_mode"] != "team":
        return [
            {
                "id": m["team_api_id"], "display_name": m["display_name"],
                "affiliation": m["affiliation"], "department": m["department"], "departments": [m["department"]] if m["department"] else [], "team_api_id": m["team_api_id"],
                "account_ids": [m["id"]], "members": [m["display_name"]],
                "disqualified": bool(m["disqualified"]),
                "disqualification_reason": m["disqualification_reason"] or ""
            } for m in members
        ]

    groups = {}
    for m in members:
        raw_name = (m["team_name"] or "").strip()
        if raw_name:
            key = " ".join(raw_name.split()).casefold()
            display = " ".join(raw_name.split())
            tid = _team_api_id_from_name(display)
        else:
            # An unassigned contestant remains isolated instead of accidentally
            # merging with every other blank team assignment.
            key = f"__unassigned_{m['id']}"
            display = f"Unassigned — {m['display_name']}"
            tid = m["team_api_id"]
        group = groups.setdefault(key, {
            "id": tid, "display_name": display, "team_api_id": tid,
            "account_ids": [], "members": [], "affiliations": [], "departments": [],
            "disqualified": False, "disqualification_reasons": []
        })
        group["account_ids"].append(m["id"])
        group["members"].append(m["display_name"])
        if m["disqualified"]:
            group["disqualified"] = True
            if m["disqualification_reason"]:
                group["disqualification_reasons"].append(m["disqualification_reason"])
        if m["affiliation"]:
            group["affiliations"].append(m["affiliation"])
        if m["department"]:
            group["departments"].append(m["department"])
    entities = []
    for g in groups.values():
        uniq_aff = list(dict.fromkeys(g.pop("affiliations")))
        g["affiliation"] = uniq_aff[0] if len(uniq_aff) == 1 else ("Multiple affiliations" if uniq_aff else "")
        uniq_dept = list(dict.fromkeys(d for d in g.get("departments", []) if d))
        g["departments"] = uniq_dept
        g["department"] = uniq_dept[0] if len(uniq_dept) == 1 else ("Multiple departments" if uniq_dept else "")
        reasons = list(dict.fromkeys(g.pop("disqualification_reasons")))
        g["disqualification_reason"] = reasons[0] if len(reasons) == 1 else ("Team disqualified." if reasons else "")
        entities.append(g)
    return entities


def standings(contest, cutoff: datetime | None = None):
    conn = get_db()
    start, _end, _freeze_at = contest_times(contest)
    if not start:
        start = utcnow()
    problems = contest_problems(contest["id"], conn)
    entities = _score_entities(contest, conn)
    scoring_model = contest["scoring_model"] if "scoring_model" in contest.keys() else "icpc"
    rows = []
    for entity in entities:
        solved = 0
        penalty = 0
        points = 0.0
        last_solve_minute = None
        cells = {}
        ids = entity["account_ids"]
        marks = ",".join("?" for _ in ids)
        for p in problems:
            query = f"SELECT * FROM submissions WHERE contest_id=? AND account_id IN ({marks}) AND problem_id=?"
            params = [contest["id"], *ids, p["id"]]
            if cutoff:
                query += " AND submitted_at<=?"
                params.append(iso(cutoff))
            query += " ORDER BY submitted_at, id"
            subs = conn.execute(query, params).fetchall()
            wrong = 0
            pending = 0
            judged = 0
            accepted = None
            for sub in subs:
                if sub["status"] != "DONE":
                    pending += 1
                    continue
                judged += 1
                if sub["verdict"] == "AC":
                    accepted = sub
                    break
                if sub["verdict"] in {"WA", "TLE", "RTE", "OLE", "MLE"}:
                    wrong += 1
            problem_points = float(p["points"] if "points" in p.keys() else 100)
            if accepted:
                minute = max(0, int((parse_dt(accepted["submitted_at"]) - start).total_seconds() // 60))
                solved += 1
                points += problem_points
                penalty += minute + contest["penalty_minutes"] * wrong
                last_solve_minute = max(last_solve_minute or 0, minute)
                cells[p["api_id"]] = {"status":"AC","wrong":wrong,"minute":minute,"judged":judged,"pending":pending,"accepted_at":accepted["submitted_at"],"accepted_submission_id":accepted["id"],"first_to_solve":False,"points":problem_points}
            elif wrong:
                cells[p["api_id"]] = {"status":"WA","wrong":wrong,"minute":None,"judged":judged,"pending":pending,"first_to_solve":False,"points":0}
            else:
                cells[p["api_id"]] = {"status":"NONE","wrong":0,"minute":None,"judged":judged,"pending":pending,"first_to_solve":False,"points":0}
        rows.append({"participant": entity, "solved": solved, "points": points, "score_value": points if scoring_model in {"points","custom"} else solved, "penalty": penalty, "last_solve": last_solve_minute, "cells": cells, "disqualified": bool(entity.get("disqualified"))})

    for p in problems:
        candidates = []
        for row in rows:
            if row["disqualified"]:
                continue
            cell = row["cells"].get(p["api_id"], {})
            if cell.get("status") == "AC" and cell.get("accepted_at"):
                candidates.append((parse_dt(cell["accepted_at"]), cell.get("accepted_submission_id", 10**18), row))
        if candidates:
            _when, _sid, first_row = min(candidates, key=lambda item: (item[0], item[1]))
            first_row["cells"][p["api_id"]]["first_to_solve"] = True

    if scoring_model in {"points", "custom"}:
        rows.sort(key=lambda r: (r["disqualified"], -r["points"], r["penalty"], r["last_solve"] if r["last_solve"] is not None else 10**9, r["participant"]["display_name"].lower()))
    else:
        rows.sort(key=lambda r: (r["disqualified"], -r["solved"], r["penalty"], r["last_solve"] if r["last_solve"] is not None else 10**9, r["participant"]["display_name"].lower()))
    previous_key = None
    previous_rank = 0
    eligible_index = 0
    for row in rows:
        if row["disqualified"]:
            row["rank"] = None
            continue
        eligible_index += 1
        key = (row["points"], row["penalty"], row["last_solve"]) if scoring_model in {"points","custom"} else (row["solved"], row["penalty"], row["last_solve"])
        if key != previous_key:
            previous_rank = eligible_index
            previous_key = key
        row["rank"] = previous_rank
    return rows, problems

def safe_extract_tests_zip(file_storage, target: Path):
    data = file_storage.read()
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        names = [n for n in zf.namelist() if not n.endswith("/")]
        for name in names:
            p = Path(name)
            if p.is_absolute() or ".." in p.parts or len(p.parts) != 1:
                raise ValueError("Test ZIP must contain files at the root only.")
        inputs = sorted(n for n in names if re.fullmatch(r"input\d+\.txt", n, re.I))
        outputs = sorted(n for n in names if re.fullmatch(r"output\d+\.txt", n, re.I))
        if not inputs or len(inputs) != len(outputs):
            raise ValueError("ZIP needs matching inputNN.txt and outputNN.txt files.")
        target.mkdir(parents=True, exist_ok=True)
        for old in target.iterdir():
            if old.is_file():
                old.unlink()
        for name in names:
            if re.fullmatch(r"(?:input|output)\d+\.txt", name, re.I):
                (target / name.lower()).write_bytes(zf.read(name))


def append_event(conn, contest_id: str, type_name: str, object_id: str | None, data):
    conn.execute(
        "INSERT INTO api_events(contest_id,type,object_id,data_json,created_at) VALUES(?,?,?,?,?)",
        (contest_id, type_name, object_id, json.dumps(data, separators=(",", ":")), iso(utcnow()))
    )


@app.template_filter("localdt")
def localdt(value):
    dt = parse_dt(value) if isinstance(value, str) else value
    if not dt:
        return "Not scheduled"
    return dt.astimezone(PHT).strftime("%b %d, %Y · %I:%M %p")


def google_login_config():
    """Google sign-in settings, or None when it is not configured (needs INQUISITOR_GOOGLE_CLIENT_ID and _CLIENT_SECRET).

    Only Google accounts whose address AND hosted-domain claim are on an allowed domain can register or sign in
    (INQUISITOR_GOOGLE_DOMAINS, comma separated; default adnu.edu.ph and gbox.adnu.edu.ph).
    """
    client_id = os.environ.get("INQUISITOR_GOOGLE_CLIENT_ID", "").strip()
    client_secret = os.environ.get("INQUISITOR_GOOGLE_CLIENT_SECRET", "").strip()
    if not client_id or not client_secret:
        return None
    raw = os.environ.get("INQUISITOR_GOOGLE_DOMAINS", "adnu.edu.ph,gbox.adnu.edu.ph")
    return {
        "client_id": client_id, "client_secret": client_secret,
        "domains": [d.strip().lower().lstrip("@") for d in raw.split(",") if d.strip()],
        # endpoints can be overridden so the flow can be tested against a fake Google
        "auth_url": os.environ.get("INQUISITOR_GOOGLE_AUTH_URL", "https://accounts.google.com/o/oauth2/v2/auth"),
        "token_url": os.environ.get("INQUISITOR_GOOGLE_TOKEN_URL", "https://oauth2.googleapis.com/token"),
    }


@app.context_processor
def inject_globals():
    me = current_user()
    contest = active_contest()
    if "csrf_token" not in session:
        session["csrf_token"] = secrets.token_urlsafe(24)
    staff_role = fx.staff_display_role(contest["id"], me["id"]) if me and contest and me["role"] in {"judge","admin"} else None
    return {
        "me": me,
        "contest": contest,
        "cstate": contest_state(contest) if contest else None,
        "languages": LANGUAGES,
        "compiler_status": compiler_status(),
        "google_login": google_login_config(),
        "demo_mode": os.environ.get("INQUISITOR_DEMO_ACCOUNTS", "1") != "0",
        "version": "v1.0.8",
        "release_name": "Galahad",
        "csrf_token": session["csrf_token"],
        "staff_role": staff_role,
        "staff_role_label": fx.STAFF_ROLES.get(staff_role, "Contest Director" if me and me["role"]=="admin" else "Judge"),
    }


@app.before_request
def security_and_api_gate():
    # CSRF protection for browser forms. API clients authenticate separately.
    if request.method in {"POST", "PUT", "PATCH", "DELETE"} and not request.path.startswith("/api/"):
        expected = session.get("csrf_token")
        supplied = request.form.get("csrf_token") or request.headers.get("X-CSRF-Token")
        if expected and supplied != expected:
            abort(400, "Invalid or missing CSRF token.")
    if request.path.startswith("/api/contests/"):
        m = re.match(r"^/api/contests/([^/]+)", request.path)
        if m:
            c = contest_by_id(m.group(1))
            if c and not fx.api_token_authorized(c):
                me = current_user()
                basic_ok = False
                auth = request.authorization
                if auth and auth.username:
                    a = get_db().execute("SELECT * FROM accounts WHERE username=?", (auth.username.lower(),)).fetchone()
                    basic_ok = bool(a and check_password_hash(a["password_hash"], auth.password or ""))
                if not me and not basic_ok:
                    return jsonify(code=403, message="Contest API is disabled or requires an API key/authenticated account."), 403


@app.after_request
def security_headers(response):
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
    response.headers.setdefault("Referrer-Policy", "same-origin")
    response.headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
    if os.environ.get("INQUISITOR_PRODUCTION", "0") == "1":
        response.headers.setdefault("Content-Security-Policy", "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'; connect-src 'self'")
        if app.config.get("SESSION_COOKIE_SECURE"):
            response.headers.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
    return response


@app.get("/healthz")
def healthz():
    try:
        conn=db_connect(); conn.execute("SELECT 1").fetchone(); conn.close()
        return jsonify({"status":"ok","version":"1.0.8","mode":"vercel-demo" if VERCEL_DEMO else "server"})
    except Exception:
        return jsonify({"status":"error"}),503


def staff_landing_endpoint(me) -> str:
    """The first page a staff member's role gives access to (so a problem setter does not land on a 403)."""
    contest = active_contest()
    role = (fx.staff_display_role(contest["id"], me["id"]) if contest else None) or "judge"
    perms = fx.ROLE_PERMISSIONS.get(role, set())
    for perm, endpoint in (("judge", "judge_console"), ("problems", "admin_problems"), ("participants", "admin_participants"), ("announcements", "v1.public_announcements")):
        if "all" in perms or perm in perms:
            return endpoint
    return "leaderboard"


@app.route("/")
def index():
    me = current_user()
    if not me:
        return redirect(url_for("login") if google_login_config() else url_for("leaderboard"))
    if me["role"] == "admin":
        return redirect(url_for("admin_dashboard"))
    if me["role"] == "judge":
        return redirect(url_for(staff_landing_endpoint(me)))
    return redirect(url_for("dashboard"))


def _password_login_view(template: str):
    """Username + password sign-in (rate limited). Used by /login when Google sign-in is not configured, and by the staff sign-in page."""
    if request.method == "POST":
        client=(request.remote_addr or "unknown")[:80]; now_ts=time.time()
        attempts=[t for t in LOGIN_ATTEMPTS.get(client,[]) if now_ts-t<LOGIN_WINDOW_SECONDS]
        LOGIN_ATTEMPTS[client]=attempts
        if len(attempts)>=LOGIN_MAX_FAILURES:
            flash("Too many failed sign-in attempts. Try again in a few minutes.", "danger")
            return render_template(template, staff_open=True), 429
        username = request.form.get("username", "").strip().lower()
        password = request.form.get("password", "")
        user = get_db().execute("SELECT * FROM accounts WHERE username=? OR email=?", (username, username)).fetchone()
        if user and check_password_hash(user["password_hash"], password):
            LOGIN_ATTEMPTS.pop(client,None)
            session.clear()
            session["user_id"] = user["id"]
            flash(f"Welcome, {user['display_name']}.", "success")
            if user["role"]=="participant":
                c=active_contest(get_db()); reg=participant_registered(c["id"],user["id"],get_db()) if c else None
                if reg and reg["temporary_password"]: return redirect(url_for("change_password"))
            return redirect(url_for("index"))
        attempts.append(now_ts); LOGIN_ATTEMPTS[client]=attempts
        flash("Invalid username or password.", "danger")
        return render_template(template, staff_open=True)
    return render_template(template)


@app.route("/login", methods=["GET", "POST"])
def login():
    if google_login_config():
        # Students use Google. The password form is not offered here, and the endpoint refuses password attempts.
        if request.method == "POST":
            abort(404)
        return render_template("login.html")
    return _password_login_view("login.html")


def staff_login_path() -> str:
    """Hidden address of the director/judge password sign-in (INQUISITOR_STAFF_LOGIN_PATH, or derived from the secret key)."""
    custom = os.environ.get("INQUISITOR_STAFF_LOGIN_PATH", "").strip()
    if custom:
        return "/" + custom.strip("/")
    digest = hmac.new(app.config["SECRET_KEY"].encode(), b"inquisitor-staff-login", hashlib.sha256).hexdigest()[:24]
    return "/staff-" + digest


def staff_login():
    resp = make_response(_password_login_view("login_staff.html"))
    resp.headers["X-Robots-Tag"] = "noindex, nofollow"
    return resp


app.add_url_rule(staff_login_path(), "staff_login", staff_login, methods=["GET", "POST"])


# ---- Google sign-in (ADNU accounts) ----------------------------------------------------------------
# Authorization-code flow with PKCE. The ID token is received straight from Google's token endpoint over TLS
# (OpenID Connect Core 3.1.3.7), so we validate its claims; only accounts on the allowed domains get in.
GOOGLE_ISSUERS = {"https://accounts.google.com", "accounts.google.com"}
GOOGLE_STATE_TTL = 600
GOOGLE_PENDING_TTL = 1800
GOOGLE_DOMAIN_ERROR = "That Google account is not an ADNU account. Sign in with your {domains} email."


def registration_is_closed(contest) -> bool:
    now = utcnow()
    reg_start = parse_dt(contest["registration_start"]) if contest["registration_start"] else None
    reg_end = parse_dt(contest["registration_end"]) if contest["registration_end"] else None
    return bool((not contest["signup_open"]) or (reg_start and now < reg_start) or (reg_end and now > reg_end)
                or contest["lifecycle_phase"] in {"registration_closed", "running", "ended", "finalized", "archived"})


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _google_redirect_uri() -> str:
    base = os.environ.get("INQUISITOR_PUBLIC_URL", "").strip().rstrip("/") or request.url_root.rstrip("/")
    return base + "/auth/google/callback"


def _domains_text(cfg) -> str:
    return " or ".join("@" + d for d in cfg["domains"])


def _google_fail(message: str):
    flash(message, "danger")
    return redirect(url_for("login"))


def _google_exchange_code(cfg, code: str, verifier: str) -> dict:
    body = urllib.parse.urlencode({
        "code": code, "client_id": cfg["client_id"], "client_secret": cfg["client_secret"],
        "redirect_uri": _google_redirect_uri(), "grant_type": "authorization_code", "code_verifier": verifier,
    }).encode()
    req = urllib.request.Request(cfg["token_url"], data=body, headers={"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _jwt_claims(token: str) -> dict:
    parts = (token or "").split(".")
    if len(parts) != 3:
        raise ValueError("not a JWT")
    return json.loads(base64.urlsafe_b64decode(parts[1] + "=" * (-len(parts[1]) % 4)))


def _check_google_claims(cfg, claims: dict, nonce: str) -> str | None:
    """Return None when the ID token is acceptable, otherwise a message for the student."""
    if claims.get("iss") not in GOOGLE_ISSUERS:
        return "Google sign-in could not be verified. Please try again."
    aud = claims.get("aud")
    if cfg["client_id"] not in (aud if isinstance(aud, list) else [aud]):
        return "Google sign-in could not be verified. Please try again."
    try:
        if float(claims.get("exp", 0)) < time.time():
            return "Google sign-in could not be verified. Please try again."
    except (TypeError, ValueError):
        return "Google sign-in could not be verified. Please try again."
    if not nonce or not hmac.compare_digest(str(claims.get("nonce", "")), nonce):
        return "Google sign-in could not be verified. Please try again."
    email = str(claims.get("email", "")).strip().lower()
    if not claims.get("sub") or not email or claims.get("email_verified") not in (True, "true"):
        return "Google sign-in could not be verified. Please try again."
    domain = email.rpartition("@")[2]
    hosted = str(claims.get("hd", "")).strip().lower()
    # Both the address and Google's hosted-domain claim must be ADNU: a personal Gmail account has no hd claim.
    if domain not in cfg["domains"] or hosted not in cfg["domains"]:
        return GOOGLE_DOMAIN_ERROR.format(domains=_domains_text(cfg))
    return None


def _unique_username(conn, email: str) -> str:
    base = re.sub(r"[^a-z0-9._-]", "", email.split("@")[0].lower()).strip("._-")[:28] or "student"
    if len(base) < 3:
        base = (base + "student")[:12]
    name, n = base, 1
    while conn.execute("SELECT 1 FROM accounts WHERE username=?", (name,)).fetchone():
        n += 1
        name = f"{base}{n}"
    return name


@app.get("/auth/google")
def google_login():
    cfg = google_login_config()
    if not cfg:
        abort(404)
    if current_user():
        return redirect(url_for("index"))
    state, nonce, verifier = secrets.token_urlsafe(24), secrets.token_urlsafe(24), secrets.token_urlsafe(48)
    session["google_oauth"] = {"state": state, "nonce": nonce, "verifier": verifier, "at": time.time()}
    params = {
        "client_id": cfg["client_id"], "redirect_uri": _google_redirect_uri(), "response_type": "code", "scope": "openid email profile",
        "state": state, "nonce": nonce, "code_challenge": _b64url(hashlib.sha256(verifier.encode()).digest()), "code_challenge_method": "S256",
        "prompt": "select_account", "hd": "*",
    }
    return redirect(cfg["auth_url"] + "?" + urllib.parse.urlencode(params))


@app.get("/auth/google/callback")
def google_callback():
    cfg = google_login_config()
    if not cfg:
        abort(404)
    saved = session.pop("google_oauth", None)          # single use: a replayed callback finds nothing
    if request.args.get("error"):
        return _google_fail("Google sign-in was cancelled.")
    given = request.args.get("state", "")
    if not saved or time.time() - saved.get("at", 0) > GOOGLE_STATE_TTL or not given or not hmac.compare_digest(saved["state"], given):
        return _google_fail("That sign-in link expired. Please try again.")
    code = request.args.get("code", "")
    if not code:
        return _google_fail("Google sign-in did not complete. Please try again.")
    try:
        tokens = _google_exchange_code(cfg, code, saved["verifier"])
        claims = _jwt_claims(tokens.get("id_token", ""))
    except Exception as exc:                            # network, bad response, malformed token
        print("Google sign-in failed:", repr(exc), flush=True)
        return _google_fail("Could not complete sign-in with Google. Please try again.")
    problem = _check_google_claims(cfg, claims, saved["nonce"])
    if problem:
        return _google_fail(problem)
    email, sub = str(claims["email"]).strip().lower(), str(claims["sub"])
    name = str(claims.get("name") or email.split("@")[0]).strip()[:120]
    conn = get_db(); contest = active_contest(conn)
    account = conn.execute("SELECT * FROM accounts WHERE google_sub=?", (sub,)).fetchone()
    if not account:
        account = conn.execute("SELECT * FROM accounts WHERE lower(email)=? OR lower(alt_email)=? ORDER BY id LIMIT 1", (email, email)).fetchone()
    if account and account["role"] in ("admin", "judge"):
        # Directors and judges: a Google account that is verified to own the address on file signs in directly
        # (the registration window does not apply to staff). The name is taken from Google the first time.
        first_link = not account["google_sub"]
        conn.execute("UPDATE accounts SET google_sub=COALESCE(google_sub,?), display_name=CASE WHEN ? THEN ? ELSE display_name END WHERE id=?", (sub, 1 if first_link else 0, name, account["id"]))
        session.clear(); session["user_id"] = account["id"]
        fx.audit(conn, "auth.google_staff", contest_id=contest["id"] if contest else None, object_type="account", object_id=account["id"], detail=f"{email} signed in with Google", actor=account)
        conn.commit()
        flash("Welcome, " + (name if first_link else account["display_name"]) + ".", "success")
        return redirect(url_for("index"))
    if account and account["role"] != "participant":
        return _google_fail("This account cannot sign in with Google.")
    if account:
        if not account["google_sub"]:
            conn.execute("UPDATE accounts SET google_sub=? WHERE id=?", (sub, account["id"])); conn.commit()
        registered = participant_registered(contest["id"], account["id"], conn) if contest else True
        if not registered and contest and not registration_is_closed(contest):
            session.clear()
            session["google_pending"] = {"sub": sub, "email": email, "name": account["display_name"] or name, "account_id": account["id"], "at": time.time()}
            return redirect(url_for("google_complete"))
        session.clear(); session["user_id"] = account["id"]
        flash(f"Welcome, {account['display_name']}.", "success")
        return redirect(url_for("index"))
    if not contest:
        return _google_fail("No contest is open for registration yet.")
    if registration_is_closed(contest):
        return _google_fail("Registration is closed. Only students who registered earlier can sign in.")
    session.clear()
    session["google_pending"] = {"sub": sub, "email": email, "name": name, "at": time.time()}
    return redirect(url_for("google_complete"))


@app.route("/auth/google/complete", methods=["GET", "POST"])
def google_complete():
    cfg = google_login_config()
    if not cfg:
        abort(404)
    pending = session.get("google_pending")
    if not pending or time.time() - pending.get("at", 0) > GOOGLE_PENDING_TTL:
        session.pop("google_pending", None)
        return _google_fail("Your sign-in expired. Please sign in with Google again.")
    conn = get_db(); contest = active_contest(conn)
    if not contest or registration_is_closed(contest):
        session.pop("google_pending", None)
        return _google_fail("Registration is closed. Only students who registered earlier can sign in.")
    institutions = conn.execute("SELECT * FROM institutions WHERE active=1 ORDER BY name").fetchall()
    if request.method == "POST":
        f = request.form
        display_name = f.get("display_name", "").strip(); department = f.get("department", "").strip(); college = f.get("college", "").strip()
        year_level = f.get("year_level", "").strip(); affiliation = f.get("affiliation", "").strip(); team_name = f.get("team_name", "").strip() if contest["competition_mode"] == "team" else ""
        try:
            if not display_name:
                raise ValueError("Full name is required.")
            if not department:
                raise ValueError("Department / program is required.")
            errors = fx.eligibility_errors(contest, department=department, college=college, institution=affiliation, year_level=year_level)
            if errors:
                raise ValueError(" ".join(errors))
            if contest["competition_mode"] == "team" and team_name:
                ok, _count = fx.team_size_ok(conn, contest, team_name)
                if not ok:
                    raise ValueError(f"Team '{team_name}' already reached the maximum of {contest['max_team_size']} members.")
            inst = conn.execute("SELECT id FROM institutions WHERE lower(name)=lower(?) OR lower(short_name)=lower(?)", (affiliation, affiliation)).fetchone() if affiliation else None
            iid = inst["id"] if inst else None
            approval = "PENDING" if contest["requires_approval"] else "APPROVED"
            aid = pending.get("account_id")
            if aid:
                conn.execute("UPDATE accounts SET display_name=?,affiliation=?,department=?,college=?,year_level=?,institution_id=? WHERE id=? AND role='participant'",
                             (display_name, affiliation, department, college, year_level, iid, aid))
            else:
                cur = conn.execute(
                    "INSERT INTO accounts(username,email,password_hash,display_name,affiliation,department,college,year_level,institution_id,role,google_sub,created_at) VALUES(?,?,?,?,?,?,?,?,?, 'participant', ?, ?)",
                    (_unique_username(conn, pending["email"]), pending["email"], generate_password_hash(secrets.token_hex(32)), display_name, affiliation, department, college, year_level, iid, pending["sub"], iso(utcnow())))
                aid = cur.lastrowid
            conn.execute("INSERT OR IGNORE INTO registrations(contest_id,account_id,team_api_id,team_name,approval_status,registration_status,coach_name,adviser_name) VALUES(?,?,?,?,?,?,?,?)",
                         (contest["id"], aid, f"p-{aid}", team_name, approval, "REGISTERED", "", ""))
            fx.audit(conn, "registration.google", contest_id=contest["id"], object_type="account", object_id=aid, detail=f"{display_name}; {pending['email']}; approval={approval}",
                     actor={"id": aid, "display_name": display_name})
            conn.commit()
            session.clear(); session["user_id"] = aid
            flash("Registration submitted and awaiting approval." if approval == "PENDING" else "You are registered. Good luck!", "success")
            return redirect(url_for("dashboard"))
        except ValueError as exc:
            conn.rollback(); flash(str(exc), "danger")
    return render_template("google_complete.html", pending=pending, institutions=institutions)


@app.get("/auth/google/cancel")
def google_cancel():
    session.pop("google_pending", None)
    return redirect(url_for("login"))


@app.route("/signup", methods=["GET", "POST"])
def signup():
    if current_user():
        return redirect(url_for("index"))
    if google_login_config() and os.environ.get("INQUISITOR_PASSWORD_SIGNUP", "0") != "1":
        flash("Students register by signing in with their ADNU Google account.", "success")
        return redirect(url_for("login"))
    conn = get_db(); contest = active_contest(conn)
    if not contest:
        flash("No active contest is available for registration.", "danger")
        return redirect(url_for("login"))
    now=utcnow(); reg_start=parse_dt(contest["registration_start"]) if contest["registration_start"] else None; reg_end=parse_dt(contest["registration_end"]) if contest["registration_end"] else None
    registration_closed = (not bool(contest["signup_open"])) or (reg_start and now < reg_start) or (reg_end and now > reg_end) or contest["lifecycle_phase"] in {"registration_closed","running","ended","finalized","archived"}
    institutions=conn.execute("SELECT * FROM institutions WHERE active=1 ORDER BY name").fetchall()
    if registration_closed:
        return render_template("signup.html", registration_closed=True, contest=contest, institutions=institutions)

    if request.method == "POST":
        username=request.form.get("username","").strip().lower(); email=request.form.get("email","").strip().lower(); password=request.form.get("password",""); confirm=request.form.get("confirm_password",""); display_name=request.form.get("display_name","").strip(); affiliation=request.form.get("affiliation","").strip(); department=request.form.get("department","").strip(); college=request.form.get("college","").strip(); year_level=request.form.get("year_level","").strip(); team_name=request.form.get("team_name","").strip() if contest["competition_mode"]=="team" else ""; coach=request.form.get("coach_name","").strip(); adviser=request.form.get("adviser_name","").strip()
        try:
            if not re.fullmatch(r"[a-z0-9][a-z0-9_.-]{2,31}", username): raise ValueError("Username must be 3-32 characters using lowercase letters, numbers, ., _, or -.")
            if email and not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email): raise ValueError("Enter a valid email address.")
            if len(password)<10: raise ValueError("Password must contain at least 10 characters.")
            if password!=confirm: raise ValueError("Passwords do not match.")
            if not display_name: raise ValueError("Full name is required.")
            if not department: raise ValueError("Department / program is required.")
            errors=fx.eligibility_errors(contest,department=department,college=college,institution=affiliation,year_level=year_level)
            if errors: raise ValueError(" ".join(errors))
            if contest["competition_mode"]=="team" and team_name:
                ok,count=fx.team_size_ok(conn,contest,team_name)
                if not ok: raise ValueError(f"Team '{team_name}' already reached the maximum of {contest['max_team_size']} members.")
            iid=None
            if affiliation:
                inst=conn.execute("SELECT id FROM institutions WHERE lower(name)=lower(?) OR lower(short_name)=lower(?)",(affiliation,affiliation)).fetchone(); iid=inst["id"] if inst else None
            cur=conn.execute("INSERT INTO accounts(username,email,password_hash,display_name,affiliation,department,college,year_level,institution_id,role,created_at) VALUES(?,?,?,?,?,?,?,?,?, 'participant', ?)",(username,email,generate_password_hash(password),display_name,affiliation,department,college,year_level,iid,iso(utcnow())))
            aid=cur.lastrowid; approval="PENDING" if contest["requires_approval"] else "APPROVED"
            conn.execute("INSERT INTO registrations(contest_id,account_id,team_api_id,team_name,approval_status,registration_status,coach_name,adviser_name) VALUES(?,?,?,?,?,?,?,?)",(contest["id"],aid,f"p-{aid}",team_name,approval,"REGISTERED",coach,adviser))
            fx.audit(conn,"registration.self",contest_id=contest["id"],object_type="account",object_id=aid,detail=f"{display_name}; approval={approval}")
            conn.commit(); session.clear(); session["user_id"]=aid
            flash("Registration submitted and awaiting approval." if approval=="PENDING" else "Your contestant account is ready.","success")
            return redirect(url_for("dashboard"))
        except sqlite3.IntegrityError:
            conn.rollback(); flash("That username or email is already in use.","danger")
        except ValueError as exc:
            conn.rollback(); flash(str(exc),"danger")
    return render_template("signup.html", registration_closed=False, contest=contest, institutions=institutions)

@app.route("/change-password", methods=["GET","POST"])
def change_password():
    me=current_user()
    if not me: return redirect(url_for("login"))
    if request.method=="POST":
        password=request.form.get("password",""); confirm=request.form.get("confirm_password","")
        if len(password)<10: flash("Choose a password with at least 10 characters.","danger")
        elif password!=confirm: flash("Passwords do not match.","danger")
        else:
            conn=get_db(); conn.execute("UPDATE accounts SET password_hash=? WHERE id=?",(generate_password_hash(password),me["id"]))
            c=active_contest(conn)
            if c: conn.execute("UPDATE registrations SET temporary_password=0 WHERE contest_id=? AND account_id=?",(c["id"],me["id"]))
            fx.audit(conn,"account.password_change",contest_id=c["id"] if c else None,object_type="account",object_id=me["id"]); conn.commit(); flash("Password updated.","success"); return redirect(url_for("index"))
    return render_template("change_password.html")


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("leaderboard"))


@app.route("/dashboard")
@participant_required
def dashboard():
    me = current_user()
    contest = active_contest()
    reg = participant_registered(contest["id"], me["id"])
    problems = get_db().execute("SELECT * FROM problems WHERE contest_id=? AND enabled=1 ORDER BY ordinal,code",(contest["id"],)).fetchall()
    board, _ = standings(contest)
    mine = next((r for r in board if me["id"] in r["participant"].get("account_ids", [])), None)
    recent = get_db().execute(
        """SELECT s.*, p.code, p.title FROM submissions s JOIN problems p ON p.id=s.problem_id
           WHERE s.contest_id=? AND s.account_id=? ORDER BY s.id DESC LIMIT 8""",
        (contest["id"], me["id"])
    ).fetchall()
    return render_template("dashboard.html", reg=reg, problems=problems, mine=mine, recent=recent)


@app.route("/problems")
@participant_required
def problems():
    contest = active_contest()
    rows=get_db().execute("SELECT * FROM problems WHERE contest_id=? AND enabled=1 ORDER BY ordinal,code",(contest["id"],)).fetchall()
    return render_template("problems.html", problems=rows)


@app.route("/problem/<code>")
@participant_required
def problem(code):
    contest = active_contest()
    p = get_db().execute("SELECT * FROM problems WHERE contest_id=? AND code=? AND enabled=1", (contest["id"], code.upper())).fetchone()
    if not p:
        abort(404)
    return render_template("problem.html", p=p)


@app.route("/submit", methods=["GET", "POST"])
@participant_required
def submit():
    me=current_user(); contest=active_contest(); conn=get_db(); reg=participant_registered(contest["id"],me["id"],conn)
    if not reg:
        flash("You are not registered for the active contest.","danger"); return redirect(url_for("dashboard"))
    if reg["approval_status"] != "APPROVED":
        flash("Your registration has not yet been approved by contest staff.","danger"); return redirect(url_for("dashboard"))
    if reg["registration_status"] in {"WITHDRAWN","DNS","DISQUALIFIED"} or reg["disqualified"]:
        reason=reg["disqualification_reason"] or f"Contest status: {reg['registration_status']}"
        flash(f"Submissions are disabled: {reason}","danger"); return redirect(url_for("dashboard"))
    if contest["require_checkin"] and reg["registration_status"] not in {"CHECKED_IN","ACTIVE"}:
        flash("Contest check-in is required before submissions are enabled.","danger"); return redirect(url_for("dashboard"))
    problems=conn.execute("SELECT * FROM problems WHERE contest_id=? AND enabled=1 ORDER BY ordinal,code",(contest["id"],)).fetchall()
    if request.method=="GET":
        return render_template("submit.html",problems=problems,selected=request.args.get("problem",problems[0]["code"] if problems else ""))
    st=contest_state(contest)
    if not contest["submissions_open"] or st["phase"] not in {"RUNNING","FROZEN","PRACTICE"}:
        flash("Submissions are not open right now.","danger"); return redirect(url_for("submit"))
    code=request.form.get("problem","").upper(); selected_language=request.form.get("language",""); language=selected_language
    p=conn.execute("SELECT * FROM problems WHERE contest_id=? AND code=? AND enabled=1",(contest["id"],code)).fetchone(); source=request.form.get("source",""); uploaded=request.files.get("source_file"); detected_language=None; mismatch=False
    if uploaded and uploaded.filename:
        detected_language=detect_language_from_filename(uploaded.filename)
        if detected_language:
            mismatch=bool(selected_language and selected_language != detected_language); language=detected_language
        source=uploaded.read().decode("utf-8",errors="replace")
    if not p or language not in LANGUAGES:
        flash("Invalid problem or language. Use a supported source file or select a language.","danger"); return redirect(url_for("submit"))
    if not source.strip(): flash("Paste code or upload a source file.","danger"); return redirect(url_for("submit",problem=code))
    if len(source.encode("utf-8")) > p["code_limit_kb"]*1024: flash("Source code exceeds this problem's code limit.","danger"); return redirect(url_for("submit",problem=code))
    entry_point={"java":"Main","kotlin":"MainKt","python3":"main.py","ruby":"main.rb"}.get(language)
    detail=""
    if mismatch: detail=f"Language mismatch: form selected {selected_language}; file extension detected {language}. Inquisitor used the detected language."
    cur=conn.execute("INSERT INTO submissions(contest_id,account_id,problem_id,language,source,entry_point,submitted_at,status,detail) VALUES(?,?,?,?,?,?,?,'PENDING',?)",(contest["id"],me["id"],p["id"],language,source,entry_point,iso(utcnow()),detail)); sid=cur.lastrowid
    append_event(conn,contest["id"],"submissions",str(sid),api_submission(conn.execute("SELECT * FROM submissions WHERE id=?",(sid,)).fetchone(),contest,conn)); fx.audit(conn,"submission.create",contest_id=contest["id"],object_type="submission",object_id=sid,detail=f"{p['code']} / {language}"); conn.commit()
    if mismatch: flash(f"Submission #{sid} queued. The uploaded file extension indicates {LANGUAGES[language].label}, so Inquisitor used that language instead of {LANGUAGES.get(selected_language,LANGUAGES[language]).label}.","success")
    elif detected_language: flash(f"Submission #{sid} entered the judging queue. Language detected as {LANGUAGES[language].label}.","success")
    else: flash(f"Submission #{sid} entered the judging queue.","success")
    return redirect(url_for("submissions"))

@app.route("/submissions")
@role_required("participant", "judge", "admin")
def submissions():
    me = current_user()
    contest = active_contest()
    conn = get_db()
    if me["role"] in {"judge", "admin"}:
        rows = conn.execute(
            """SELECT s.*, a.display_name participant_name, p.code, p.title FROM submissions s
               JOIN accounts a ON a.id=s.account_id JOIN problems p ON p.id=s.problem_id
               WHERE s.contest_id=? ORDER BY s.id DESC LIMIT 300""", (contest["id"],)
        ).fetchall()
    else:
        rows = conn.execute(
            """SELECT s.*, a.display_name participant_name, p.code, p.title FROM submissions s
               JOIN accounts a ON a.id=s.account_id JOIN problems p ON p.id=s.problem_id
               WHERE s.contest_id=? AND s.account_id=? ORDER BY s.id DESC LIMIT 300""", (contest["id"], me["id"])
        ).fetchall()
    return render_template("submissions.html", rows=rows)


@app.route("/clarifications", methods=["GET", "POST"])
@role_required("participant", "judge", "admin")
def clarifications():
    me = current_user()
    contest = active_contest()
    conn = get_db()
    if request.method == "POST":
        if me["role"] != "participant":
            abort(403)
        reg = participant_registered(contest["id"], me["id"], conn)
        if not reg:
            flash("You are not registered for the active contest.", "danger")
            return redirect(url_for("clarifications"))
        problem_code = request.form.get("problem", "")
        p = conn.execute("SELECT * FROM problems WHERE contest_id=? AND code=?", (contest["id"], problem_code)).fetchone() if problem_code else None
        question = request.form.get("question", "").strip()
        request_type = request.form.get("request_type", "QUESTION").upper()
        if request_type not in {"QUESTION", "AMBIGUITY", "ERROR"}:
            request_type = "QUESTION"
        if request_type in {"AMBIGUITY", "ERROR"} and not p:
            flash("Select a problem when claiming ambiguity or an error in a problem statement.", "danger")
        elif not question:
            flash("Clarification request cannot be empty.", "danger")
        else:
            cur = conn.execute(
                """INSERT INTO clarifications(contest_id,account_id,problem_id,question,request_type,created_at)
                   VALUES(?,?,?,?,?,?)""",
                (contest["id"], me["id"], p["id"] if p else None, question, request_type, iso(utcnow()))
            )
            cid = cur.lastrowid
            row = conn.execute("SELECT * FROM clarifications WHERE id=?", (cid,)).fetchone()
            append_event(conn, contest["id"], "clarifications", f"q-{cid}", api_clarification_question(row, contest, conn))
            conn.commit()
            flash("Clarification request sent to the judges.", "success")
            return redirect(url_for("clarifications"))
    probs = contest_problems(contest["id"], conn)
    broadcasts = []
    if me["role"] in {"judge", "admin"}:
        rows = conn.execute(
            """SELECT c.*, a.display_name participant_name, p.code FROM clarifications c
               JOIN accounts a ON a.id=c.account_id LEFT JOIN problems p ON p.id=c.problem_id
               WHERE c.contest_id=? ORDER BY CASE c.status WHEN 'OPEN' THEN 0 ELSE 1 END, c.id DESC""", (contest["id"],)
        ).fetchall()
    else:
        rows = conn.execute(
            """SELECT c.*, a.display_name participant_name, p.code FROM clarifications c
               JOIN accounts a ON a.id=c.account_id LEFT JOIN problems p ON p.id=c.problem_id
               WHERE c.contest_id=? AND c.account_id=? ORDER BY c.id DESC""", (contest["id"], me["id"])
        ).fetchall()
        broadcasts = conn.execute(
            """SELECT c.*, p.code FROM clarifications c
               LEFT JOIN problems p ON p.id=c.problem_id
               WHERE c.contest_id=? AND c.broadcast=1 AND c.answer IS NOT NULL AND c.account_id<>?
               ORDER BY COALESCE(c.broadcast_at,c.answered_at) DESC, c.id DESC""", (contest["id"], me["id"])
        ).fetchall()
    return render_template("clarifications.html", rows=rows, broadcasts=broadcasts, problems=probs)


@app.route("/leaderboard")
def leaderboard():
    cid = request.args.get("contest")
    contest = contest_by_id(cid) if cid else active_contest()
    if not contest:
        abort(404)
    st = contest_state(contest)
    frozen = st["phase"] in {"FROZEN", "ENDED"} and not st["revealed"]
    cutoff = st["freeze_at"] if frozen else None
    rows, problems = standings(contest, cutoff)
    return render_template("leaderboard.html", rows=rows, problems=problems, frozen=frozen, contest=contest, cstate=st)


@app.route("/contest-info")
def contest_info():
    contest = active_contest()
    return render_template("contest_info.html", contest=contest)


# ---------------- Judge ----------------
@app.route("/judge")
@fx._staff_permission("judge")
def judge_console():
    contest = active_contest()
    if not contest:
        flash("Create or activate a contest before opening the judge console.", "danger")
        me = current_user()
        return redirect(url_for("admin_contests") if me and me["role"] == "admin" else url_for("contest_info"))
    conn = get_db()
    stats = {
        "participants": conn.execute("SELECT COUNT(*) FROM registrations WHERE contest_id=?", (contest["id"],)).fetchone()[0],
        "pending": conn.execute("SELECT COUNT(*) FROM submissions WHERE contest_id=? AND status!='DONE'", (contest["id"],)).fetchone()[0],
        "clarifications": conn.execute("SELECT COUNT(*) FROM clarifications WHERE contest_id=? AND status='OPEN'", (contest["id"],)).fetchone()[0],
        "submissions": conn.execute("SELECT COUNT(*) FROM submissions WHERE contest_id=?", (contest["id"],)).fetchone()[0],
    }
    me = current_user(); cutoff = _claim_cutoff(); page_size = 40
    show = request.args.get("show", "all"); prob = request.args.get("problem", "").strip().upper(); q = request.args.get("q", "").strip()
    try: page = max(1, int(request.args.get("page", "1")))
    except ValueError: page = 1
    where = ["s.contest_id=?"]; args = [contest["id"]]
    if show == "unreviewed": where.append("s.reviewed_at IS NULL AND s.status='DONE'")
    elif show == "claimed": where.append("s.reviewed_at IS NULL AND s.claimed_by IS NOT NULL AND s.claimed_at>=?"); args.append(cutoff)
    elif show == "mine": where.append("s.claimed_by=?"); args.append(me["id"])
    elif show == "overridden": where.append("s.manual_override=1")
    elif show == "pending": where.append("s.status!='DONE'")
    else: show = "all"
    if prob: where.append("p.code=?"); args.append(prob)
    if q: where.append("(a.display_name LIKE ? OR a.username LIKE ?)"); args += [f"%{q}%", f"%{q}%"]
    base = "FROM submissions s JOIN accounts a ON a.id=s.account_id JOIN problems p ON p.id=s.problem_id WHERE " + " AND ".join(where)
    total_rows = conn.execute("SELECT COUNT(*) " + base, args).fetchone()[0]
    queue = conn.execute(
        "SELECT s.*, a.display_name participant_name, p.code, p.title, (SELECT display_name FROM accounts WHERE id=s.claimed_by) claimed_by_name, "
        "(SELECT display_name FROM accounts WHERE id=s.reviewed_by) reviewed_by_name " + base +
        " ORDER BY CASE s.status WHEN 'JUDGING' THEN 0 WHEN 'PENDING' THEN 1 ELSE 2 END, s.id DESC LIMIT ? OFFSET ?",
        args + [page_size, (page - 1) * page_size]).fetchall()
    problem_codes = [r["code"] for r in conn.execute("SELECT code FROM problems WHERE contest_id=? ORDER BY ordinal,code", (contest["id"],))]
    my_claim = active_claim(conn, me["id"])
    clar = conn.execute(
        """SELECT c.*, a.display_name participant_name, p.code FROM clarifications c
           JOIN accounts a ON a.id=c.account_id LEFT JOIN problems p ON p.id=c.problem_id
           WHERE c.contest_id=? ORDER BY CASE c.status WHEN 'OPEN' THEN 0 ELSE 1 END, c.id DESC LIMIT 20""",
        (contest["id"],)
    ).fetchall()
    participants = conn.execute(
        """SELECT a.id,a.display_name,a.affiliation,COALESCE(r.team_name,'') team_name,
                  COALESCE(r.disqualified,0) disqualified,COALESCE(r.disqualification_reason,'') disqualification_reason
           FROM registrations r JOIN accounts a ON a.id=r.account_id
           WHERE r.contest_id=? ORDER BY a.display_name""", (contest["id"],)
    ).fetchall()
    return render_template("judge.html", stats=stats, queue=queue, cutoff=cutoff, my_claim=my_claim, show=show, prob=prob, q=q, page=page, pages=max(1, -(-total_rows // page_size)), total_rows=total_rows, problem_codes=problem_codes, clar=clar, participants=participants, judge_languages=LANGUAGES, judge_compiler_status=compiler_status())


@app.post("/judge/clarification/<int:cid>/answer")
@fx._staff_permission("clarifications")
def judge_answer(cid):
    answer = request.form.get("answer", "").strip()
    scope = request.form.get("scope", "private")
    conn = get_db()
    c = conn.execute("SELECT * FROM clarifications WHERE id=?", (cid,)).fetchone()
    if not c:
        abort(404)
    if not answer:
        flash("Answer cannot be empty.", "danger")
    else:
        now = iso(utcnow())
        broadcast = 1 if scope == "broadcast" else 0
        conn.execute(
            """UPDATE clarifications
               SET answer=?,status='ANSWERED',answered_at=?,broadcast=?,broadcast_at=?
               WHERE id=?""",
            (answer, now, broadcast, now if broadcast else None, cid),
        )
        updated = conn.execute("SELECT * FROM clarifications WHERE id=?", (cid,)).fetchone()
        contest = contest_by_id(updated["contest_id"], conn)
        append_event(conn, updated["contest_id"], "clarifications", f"a-{cid}", api_clarification_answer(updated, contest, conn))
        conn.commit()
        if broadcast:
            flash("Official clarification broadcast to all contestants/teams.", "success")
        else:
            flash("Private reply sent to the contestant.", "success")
    return redirect(url_for("judge_console"))


@app.post("/judge/rejudge/<int:sid>")
@fx._staff_permission("rejudge")
def rejudge(sid):
    conn = get_db()
    conn.execute("UPDATE submissions SET status='PENDING',verdict=NULL,runtime_ms=NULL,judging_started_at=NULL,judged_at=NULL,detail='Queued for rejudge.',manual_override=0,auto_verdict=NULL WHERE id=?", (sid,))
    fx.audit(conn, "judge.rejudge", contest_id=active_contest(conn)["id"], object_type="submission", object_id=str(sid), detail="Rejudge requested; any manual verdict on this submission was cleared.")
    conn.commit()
    flash(f"Submission #{sid} queued for rejudge.", "success")
    return redirect(url_for("judge_console"))

# ---- Manual code review -------------------------------------------------
# Auto-judge decides the verdict from program output.  A judge may additionally review the source code:
# claim one submission (locked to that judge, one active claim at a time), read it, record a note, and
# optionally override the verdict.  A manual verdict is never overwritten by the auto-judge.
REVIEW_VERDICTS = ("AC", "WA", "TLE", "RTE", "CE", "MLE", "OLE", "PE")


def review_claim_minutes() -> int:
    try:
        return max(1, int(os.environ.get("INQUISITOR_REVIEW_CLAIM_MINUTES", "20")))
    except ValueError:
        return 20


def _claim_cutoff() -> str:
    return iso(utcnow() - timedelta(minutes=review_claim_minutes()))


def active_claim(conn, account_id):
    """The submission this judge is currently reviewing, or None (claims expire after a period of inactivity)."""
    return conn.execute(
        """SELECT s.*, p.code, a.display_name participant_name FROM submissions s
           JOIN problems p ON p.id=s.problem_id JOIN accounts a ON a.id=s.account_id
           WHERE s.claimed_by=? AND s.reviewed_at IS NULL AND s.claimed_at>=? ORDER BY s.claimed_at DESC LIMIT 1""",
        (account_id, _claim_cutoff()),
    ).fetchone()


def _review_state(sub, cutoff):
    if sub["reviewed_at"]:
        return "REVIEWED"
    if sub["claimed_by"] and sub["claimed_at"] and sub["claimed_at"] >= cutoff:
        return "CLAIMED"
    return "OPEN"


@app.get("/judge/submission/<int:sid>")
@fx._staff_permission("judge")
def review_submission(sid):
    conn = get_db(); me = current_user()
    sub = conn.execute(
        """SELECT s.*, p.code, p.title, a.display_name participant_name, a.affiliation participant_affiliation,
                  cb.display_name claimed_by_name, rb.display_name reviewed_by_name
           FROM submissions s JOIN problems p ON p.id=s.problem_id JOIN accounts a ON a.id=s.account_id
           LEFT JOIN accounts cb ON cb.id=s.claimed_by LEFT JOIN accounts rb ON rb.id=s.reviewed_by
           WHERE s.id=?""", (sid,)).fetchone()
    if not sub:
        abort(404)
    state = _review_state(sub, _claim_cutoff())
    mine = state == "CLAIMED" and sub["claimed_by"] == me["id"]
    if mine:  # reading the code counts as activity: keep the claim alive
        conn.execute("UPDATE submissions SET claimed_at=? WHERE id=? AND claimed_by=? AND reviewed_at IS NULL", (iso(utcnow()), sid, me["id"]))
        conn.commit()
    other = active_claim(conn, me["id"])
    return render_template("judge_review.html", sub=sub, state=state, mine=mine, my_other_claim=(other if other and other["id"] != sid else None),
                           verdicts=REVIEW_VERDICTS, language=LANGUAGES.get(sub["language"]), claim_minutes=review_claim_minutes(), can_force=fx.staff_can("analytics"))


@app.post("/judge/submission/<int:sid>/claim")
@fx._staff_permission("judge")
def review_claim(sid):
    conn = get_db(); me = current_user(); now = iso(utcnow()); cutoff = _claim_cutoff()
    sub = conn.execute("SELECT * FROM submissions WHERE id=?", (sid,)).fetchone()
    if not sub:
        abort(404)
    if sub["status"] != "DONE":
        flash("This submission is still being auto-judged. Review it once a verdict is in.", "danger")
        return redirect(url_for("review_submission", sid=sid))
    # One atomic statement: the submission must be unreviewed and unclaimed (or its claim expired), and this judge
    # must not hold another live claim.  Two judges clicking at once cannot both win.
    cur = conn.execute(
        """UPDATE submissions SET claimed_by=:me, claimed_at=:now
           WHERE id=:sid AND reviewed_at IS NULL AND (claimed_by IS NULL OR claimed_by=:me OR claimed_at<:cutoff)
             AND NOT EXISTS (SELECT 1 FROM submissions o WHERE o.claimed_by=:me AND o.reviewed_at IS NULL
                             AND o.claimed_at>=:cutoff AND o.id!=:sid)""",
        {"me": me["id"], "now": now, "sid": sid, "cutoff": cutoff})
    if cur.rowcount == 1:
        fx.audit(conn, "review.claim", contest_id=sub["contest_id"], object_type="submission", object_id=str(sid), detail=f"Claimed by {me['display_name']}")
        conn.commit(); flash(f"You claimed submission #{sid}. It is locked to you until you finish or release it.", "success")
    else:
        conn.rollback()
        fresh = conn.execute("SELECT s.*, a.display_name claimed_by_name FROM submissions s LEFT JOIN accounts a ON a.id=s.claimed_by WHERE s.id=?", (sid,)).fetchone()
        mine = active_claim(conn, me["id"])
        if fresh["reviewed_at"]:
            flash("This submission has already been reviewed.", "danger")
        elif mine and mine["id"] != sid:
            flash(f"Finish or release submission #{mine['id']} before claiming another.", "danger")
        else:
            flash(f"Submission #{sid} is being reviewed by {fresh['claimed_by_name'] or 'another judge'}.", "danger")
    return redirect(url_for("review_submission", sid=sid))


@app.post("/judge/submission/<int:sid>/release")
@fx._staff_permission("judge")
def review_release(sid):
    conn = get_db(); me = current_user()
    sub = conn.execute("SELECT * FROM submissions WHERE id=?", (sid,)).fetchone()
    if not sub:
        abort(404)
    forced = sub["claimed_by"] != me["id"]
    if forced and not fx.staff_can("analytics"):
        flash("Only the judge holding this claim, a Chief Judge or the Contest Director can release it.", "danger")
        return redirect(url_for("review_submission", sid=sid))
    cur = conn.execute("UPDATE submissions SET claimed_by=NULL, claimed_at=NULL WHERE id=? AND reviewed_at IS NULL", (sid,))
    if cur.rowcount == 1:
        fx.audit(conn, "review.release", contest_id=sub["contest_id"], object_type="submission", object_id=str(sid), detail=("Force-released by " if forced else "Released by ") + me["display_name"])
        conn.commit(); flash(f"Submission #{sid} released.", "success")
    return redirect(url_for("judge_console"))


@app.post("/judge/submission/<int:sid>/review")
@fx._staff_permission("judge")
def review_complete(sid):
    conn = get_db(); me = current_user()
    note = request.form.get("note", "").strip(); verdict = request.form.get("verdict", "").strip().upper(); runtime_raw = request.form.get("runtime_ms", "").strip()
    sub = conn.execute("SELECT * FROM submissions WHERE id=?", (sid,)).fetchone()
    if not sub:
        abort(404)
    if sub["claimed_by"] != me["id"] or sub["reviewed_at"]:
        flash("Claim this submission before reviewing it.", "danger")
        return redirect(url_for("review_submission", sid=sid))
    if verdict and verdict not in REVIEW_VERDICTS:
        flash("Invalid verdict.", "danger"); return redirect(url_for("review_submission", sid=sid))
    overriding = bool(verdict) and verdict != sub["verdict"]
    if overriding and not note:
        flash("Add a note explaining why you are overriding the auto-judge verdict.", "danger"); return redirect(url_for("review_submission", sid=sid))
    runtime_ms = sub["runtime_ms"]
    if overriding and runtime_raw:
        try: runtime_ms = max(0, int(runtime_raw))
        except ValueError: flash("Runtime must be a non-negative integer.", "danger"); return redirect(url_for("review_submission", sid=sid))
    now = iso(utcnow())
    if overriding:
        detail = f"Manual verdict by {me['display_name']}: {note}"
        cur = conn.execute(
            """UPDATE submissions SET verdict=?, auto_verdict=COALESCE(auto_verdict, verdict), manual_override=1, runtime_ms=?, detail=?,
                      reviewed_by=?, reviewed_at=?, review_note=? WHERE id=? AND claimed_by=? AND reviewed_at IS NULL""",
            (verdict, runtime_ms, detail, me["id"], now, note, sid, me["id"]))
    else:
        cur = conn.execute("UPDATE submissions SET reviewed_by=?, reviewed_at=?, review_note=? WHERE id=? AND claimed_by=? AND reviewed_at IS NULL", (me["id"], now, note, sid, me["id"]))
    if cur.rowcount != 1:
        conn.rollback(); flash("This review could not be saved (the claim changed). Reload and try again.", "danger"); return redirect(url_for("review_submission", sid=sid))
    if overriding:
        done = conn.execute("SELECT * FROM submissions WHERE id=?", (sid,)).fetchone()
        problem = conn.execute("SELECT * FROM problems WHERE id=?", (sub["problem_id"],)).fetchone()
        append_event(conn, sub["contest_id"], "judgements", f"j-{sid}", api_judgement(done)); fx.on_judgement(conn, done, problem)
    fx.audit(conn, "review.override" if overriding else "review.complete", contest_id=sub["contest_id"], object_type="submission", object_id=str(sid),
             detail=(f"{sub['verdict']} -> {verdict}. " if overriding else "Code reviewed, verdict kept. ") + (note or "No note."))
    conn.commit()
    flash(f"Verdict changed to {verdict} and review saved." if overriding else "Review saved. The auto-judge verdict stands.", "success")
    return redirect(url_for("judge_console"))


@app.post("/judge/manual/<int:sid>")
@fx._staff_permission("judge")
def manual_judge(sid):
    # Retired: verdict changes now go through the claim -> review flow so they are locked to one judge and audited.
    flash("Open the submission and claim it to review the code or change its verdict.", "danger")
    return redirect(url_for("review_submission", sid=sid))


@app.post("/judge/rejudge-all")
@fx._staff_permission("rejudge")
def rejudge_all():
    contest = active_contest()
    conn = get_db()
    cur = conn.execute("UPDATE submissions SET status='PENDING',verdict=NULL,runtime_ms=NULL,judging_started_at=NULL,judged_at=NULL,detail='Queued for rejudge.' WHERE contest_id=? AND manual_override=0", (contest["id"],))
    kept = conn.execute("SELECT COUNT(*) FROM submissions WHERE contest_id=? AND manual_override=1", (contest["id"],)).fetchone()[0]
    fx.audit(conn, "judge.rejudge_all", contest_id=contest["id"], object_type="contest", object_id=contest["id"], detail=f"{cur.rowcount} queued; {kept} manual verdict(s) kept.")
    conn.commit()
    flash(f"All auto-judged submissions were queued for rejudge. {kept} manually-overridden verdict(s) were kept.", "success")
    return redirect(url_for("judge_console"))


@app.post("/judge/contest")
@fx._staff_permission("emergency")
def judge_contest():
    action = request.form.get("action")
    conn = get_db()
    c = active_contest(conn)
    now = utcnow()
    if action == "demo_freeze":
        conn.execute("UPDATE contests SET start_at=?,duration_minutes=130,freeze_minutes=10,scoreboard_revealed=0,scoreboard_thawed_at=NULL WHERE id=?", (iso(now - timedelta(minutes=120)), c["id"]))
        flash("10-minute scoreboard freeze activated.", "success")
    elif action == "reveal":
        conn.execute("UPDATE contests SET scoreboard_revealed=1,scoreboard_thawed_at=? WHERE id=?", (iso(now), c["id"]))
        flash("Final scoreboard revealed.", "success")
    elif action == "hide":
        conn.execute("UPDATE contests SET scoreboard_revealed=0,scoreboard_thawed_at=NULL WHERE id=?", (c["id"],))
        flash("Automatic freeze visibility restored.", "success")
    elif action == "restart_2h":
        conn.execute("UPDATE contests SET start_at=?,duration_minutes=120,scoreboard_revealed=0,scoreboard_thawed_at=NULL WHERE id=?", (iso(now), c["id"]))
        flash("Contest restarted for two hours.", "success")
    conn.commit()
    return redirect(url_for("judge_console"))


# ---------------- Admin ----------------
@app.route("/admin")
@admin_required
def admin_dashboard():
    conn = get_db()
    active = active_contest(conn)
    stats = {
        "contests": conn.execute("SELECT COUNT(*) FROM contests").fetchone()[0],
        "problems": conn.execute("SELECT COUNT(*) FROM problems").fetchone()[0],
        "participants": conn.execute("SELECT COUNT(*) FROM accounts WHERE role='participant'").fetchone()[0],
        "submissions": conn.execute("SELECT COUNT(*) FROM submissions").fetchone()[0],
    }
    contests = conn.execute("SELECT * FROM contests ORDER BY active DESC, created_at DESC").fetchall()
    return render_template("admin_dashboard.html", stats=stats, contests=contests, active=active)


@app.route("/admin/contests", methods=["GET", "POST"])
@admin_required
def admin_contests():
    conn = get_db()
    if request.method == "POST":
        cid = request.form.get("id", "").strip()
        name = request.form.get("name", "").strip()
        formal = request.form.get("formal_name", "").strip() or name
        location = request.form.get("location", "").strip()
        start_raw = request.form.get("start_at", "").strip()
        try:
            if not ID_RE.fullmatch(cid):
                raise ValueError("Contest ID must be 1-36 characters using letters, numbers, _, -, or .")
            if not name:
                raise ValueError("Contest name is required.")
            duration = max(1, min(2880, int(request.form.get("duration_minutes", "300"))))
            freeze = max(0, min(duration, int(request.form.get("freeze_minutes", "10"))))
            penalty = max(0, min(240, int(request.form.get("penalty_minutes", "20"))))
            competition_mode = request.form.get("competition_mode", "individual")
            if competition_mode not in {"individual", "team"}:
                raise ValueError("Competition mode must be Individual or Team.")
            scope = request.form.get("scope", "department")
            if scope not in dict(fx.CONTEST_SCOPES):
                raise ValueError("Invalid contest scope.")
            scoring_model = request.form.get("scoring_model", "icpc")
            if scoring_model not in dict(fx.SCORING_MODELS):
                raise ValueError("Invalid scoring model.")
            lifecycle_phase = request.form.get("lifecycle_phase", "draft")
            if lifecycle_phase not in dict(fx.LIFECYCLE_PHASES):
                raise ValueError("Invalid contest phase.")
            signup_open = 1 if request.form.get("signup_open") == "on" else 0
            start_at = local_input_to_utc(start_raw) if start_raw else None
            reg_start = local_input_to_utc(request.form.get("registration_start", "").strip()) if request.form.get("registration_start", "").strip() else None
            reg_end = local_input_to_utc(request.form.get("registration_end", "").strip()) if request.form.get("registration_end", "").strip() else None
            make_active = request.form.get("make_active") == "on"
            max_team_size = max(1, min(20, int(request.form.get("max_team_size", "3"))))
            # Staff lists (ADNU emails): one box per role; first role wins if an email is listed twice.
            staff_entries: dict[str, str] = {}
            for field, role in (("staff_chief_judges", "chief_judge"), ("staff_judges", "judge"), ("staff_problem_setters", "problem_setter"), ("staff_registration", "registration_staff")):
                for email in fx.parse_email_list(request.form.get(field, "")):
                    staff_entries.setdefault(email, role)
            allowed_domains = (google_login_config() or {}).get("domains") or ["adnu.edu.ph", "gbox.adnu.edu.ph"]
            bad_emails = [e for e in staff_entries if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", e) or e.rpartition("@")[2] not in allowed_domains]
            if bad_emails:
                raise ValueError("These are not ADNU email addresses (" + ", ".join("@" + d for d in allowed_domains) + "): " + ", ".join(bad_emails[:8]) + ". Nothing was saved.")
            if make_active:
                conn.execute("UPDATE contests SET active=0")
            conn.execute(
                """INSERT INTO contests(
                    id,name,formal_name,location,start_at,duration_minutes,freeze_minutes,penalty_minutes,
                    competition_mode,signup_open,projector_autoscroll,active,created_at,scope,scoring_model,lifecycle_phase,
                    registration_start,registration_end,eligibility_note,allowed_departments,allowed_colleges,
                    allowed_institutions,allowed_year_levels,max_team_size,requires_approval,require_checkin,custom_scoring_note,
                    submissions_open,practice_mode,registration_instruction
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (cid,name,formal,location,start_at,duration,freeze,penalty,competition_mode,signup_open,1 if request.form.get('projector_autoscroll')=='on' else 0,1 if make_active else 0,iso(utcnow()),
                 scope,scoring_model,lifecycle_phase,reg_start,reg_end,request.form.get("eligibility_note","").strip(),
                 request.form.get("allowed_departments","").strip(),request.form.get("allowed_colleges","").strip(),
                 request.form.get("allowed_institutions","").strip(),request.form.get("allowed_year_levels","").strip(),max_team_size,
                 1 if request.form.get("requires_approval")=="on" else 0,1 if request.form.get("require_checkin")=="on" else 0,request.form.get("custom_scoring_note","").strip(),
                 1 if request.form.get("submissions_open","on")=="on" else 0,1 if lifecycle_phase=="practice" else 0,
                 request.form.get("registration_instruction","").strip())
            )
            conn.execute("UPDATE contests SET rules_text=?,organizer_text=? WHERE id=?",(request.form.get("rules_text","").strip(),request.form.get("organizer_text","TACTICS x Department of Computer Science, Ateneo de Naga University").strip(),cid))
            contest = contest_by_id(cid, conn)
            append_event(conn, cid, "contest", None, api_contest(contest))
            fx.audit(conn,"contest.create",contest_id=cid,object_type="contest",object_id=cid,detail=name)
            staff_done = {"created": 0, "updated": 0, "director": 0}
            for email, role in staff_entries.items():
                staff_done[fx.whitelist_staff(conn, sys.modules[__name__], cid, email, role)] += 1
            conn.commit()
            extra = f" {len(staff_entries) - staff_done['director']} staff added by email." if staff_entries else ""
            flash(f"Contest '{name}' created.{extra}", "success")
            return redirect(url_for("admin_contests"))
        except (ValueError, sqlite3.IntegrityError) as exc:
            conn.rollback(); flash(str(exc), "danger")
    contests = conn.execute("SELECT * FROM contests ORDER BY active DESC, archived, created_at DESC").fetchall()
    return render_template("admin_contests.html", contests=contests, contest_scopes=fx.CONTEST_SCOPES, scoring_models=fx.SCORING_MODELS, lifecycle_phases=fx.LIFECYCLE_PHASES)


@app.post("/admin/contests/<cid>/activate")
@admin_required
def admin_activate_contest(cid):
    conn = get_db()
    if not contest_by_id(cid, conn): abort(404)
    conn.execute("UPDATE contests SET active=0")
    conn.execute("UPDATE contests SET active=1 WHERE id=?", (cid,))
    fx.audit(conn,"contest.activate",contest_id=cid,object_type="contest",object_id=cid)
    conn.commit(); flash("Active contest changed.", "success")
    return redirect(request.referrer or url_for("admin_contests"))


@app.post("/admin/contests/<cid>/update")
@admin_required
def admin_update_contest(cid):
    conn = get_db(); c = contest_by_id(cid, conn)
    if not c: abort(404)
    try:
        name = request.form.get("name", c["name"]).strip() or c["name"]
        formal = request.form.get("formal_name", "").strip() or name
        location = request.form.get("location", c["location"]).strip()
        start_raw = request.form.get("start_at", "").strip(); start_at = local_input_to_utc(start_raw) if start_raw else c["start_at"]
        duration = max(1, min(2880, int(request.form.get("duration_minutes", c["duration_minutes"]))))
        freeze = max(0, min(duration, int(request.form.get("freeze_minutes", c["freeze_minutes"]))))
        penalty = max(0, min(240, int(request.form.get("penalty_minutes", c["penalty_minutes"]))))
        competition_mode = request.form.get("competition_mode", c["competition_mode"]); scope=request.form.get("scope",c["scope"]); scoring=request.form.get("scoring_model",c["scoring_model"]); phase=request.form.get("lifecycle_phase",c["lifecycle_phase"])
        if competition_mode not in {"individual","team"} or scope not in dict(fx.CONTEST_SCOPES) or scoring not in dict(fx.SCORING_MODELS) or phase not in dict(fx.LIFECYCLE_PHASES): raise ValueError("Invalid contest setting.")
        reg_start_raw=request.form.get("registration_start","").strip(); reg_end_raw=request.form.get("registration_end","").strip()
        reg_start=local_input_to_utc(reg_start_raw) if reg_start_raw else c["registration_start"]; reg_end=local_input_to_utc(reg_end_raw) if reg_end_raw else c["registration_end"]
        conn.execute("""UPDATE contests SET name=?,formal_name=?,location=?,start_at=?,duration_minutes=?,freeze_minutes=?,penalty_minutes=?,competition_mode=?,signup_open=?,projector_autoscroll=?,scope=?,scoring_model=?,lifecycle_phase=?,registration_start=?,registration_end=?,eligibility_note=?,allowed_departments=?,allowed_colleges=?,allowed_institutions=?,allowed_year_levels=?,max_team_size=?,requires_approval=?,require_checkin=?,custom_scoring_note=?,submissions_open=?,practice_mode=?,registration_instruction=? WHERE id=?""",
            (name,formal,location,start_at,duration,freeze,penalty,competition_mode,1 if request.form.get("signup_open")=="on" else 0,1 if request.form.get("projector_autoscroll")=="on" else 0,scope,scoring,phase,reg_start,reg_end,request.form.get("eligibility_note",c["eligibility_note"]).strip(),request.form.get("allowed_departments",c["allowed_departments"]).strip(),request.form.get("allowed_colleges",c["allowed_colleges"]).strip(),request.form.get("allowed_institutions",c["allowed_institutions"]).strip(),request.form.get("allowed_year_levels",c["allowed_year_levels"]).strip(),max(1,min(20,int(request.form.get("max_team_size",c["max_team_size"])))),1 if request.form.get("requires_approval")=="on" else 0,1 if request.form.get("require_checkin")=="on" else 0,request.form.get("custom_scoring_note",c["custom_scoring_note"]).strip(),1 if request.form.get("submissions_open")=="on" else 0,1 if phase=="practice" else 0,request.form.get("registration_instruction",c["registration_instruction"]).strip(),cid))
        conn.execute("UPDATE contests SET rules_text=?,organizer_text=? WHERE id=?",(request.form.get("rules_text",c["rules_text"]).strip(),request.form.get("organizer_text",c["organizer_text"]).strip(),cid))
        updated=contest_by_id(cid,conn); append_event(conn,cid,"contest",None,api_contest(updated)); fx.audit(conn,"contest.update",contest_id=cid,object_type="contest",object_id=cid,detail=name); conn.commit(); flash("Contest settings updated.","success")
    except (ValueError,sqlite3.IntegrityError) as exc:
        conn.rollback(); flash(str(exc),"danger")
    return redirect(url_for("admin_contests"))

@app.route("/admin/problems", methods=["GET", "POST"])
@fx._staff_permission("problems")
def admin_problems():
    conn = get_db(); contest = active_contest(conn)
    if request.method == "POST":
        try:
            api_id=request.form.get("api_id","").strip().lower(); code=request.form.get("code","").strip().upper(); title=request.form.get("title","").strip(); statement=request.form.get("statement","").strip(); contributors=request.form.get("contributors","").strip(); color=request.form.get("color","#5bc98b").strip()
            if not HEX_COLOR_RE.fullmatch(color): raise ValueError("Problem color must be a six-digit hex color.")
            if not ID_RE.fullmatch(api_id): raise ValueError("Problem API ID is invalid.")
            if not code or len(code)>8: raise ValueError("Problem label/code is required and must be short.")
            if not title or not statement: raise ValueError("Title and statement are required.")
            ordinal=int(request.form.get("ordinal","1")); time_limit_ms=max(100,min(60000,int(request.form.get("time_limit_ms","2000")))); memory_limit_mb=max(16,min(16384,int(request.form.get("memory_limit_mb","256"))))
            difficulty=request.form.get("difficulty","Medium"); difficulty=difficulty if difficulty in {"Easy","Medium","Hard","Expert"} else "Medium"
            tags=request.form.get("tags","").strip(); source_name=request.form.get("source","").strip(); internal_notes=request.form.get("internal_notes","").strip(); points=max(0,float(request.form.get("points","100"))); enabled=1 if request.form.get("enabled","on")=="on" else 0
            reference_solution=request.form.get("reference_solution",""); reference_language=request.form.get("reference_language","") if reference_solution.strip() else ""; validator_source=request.form.get("validator_source","")
            if reference_language and reference_language not in LANGUAGES: raise ValueError("Reference solution language is unsupported.")
            sample_input=request.form.get("sample_input",""); sample_output=request.form.get("sample_output","")
            cur=conn.execute("""INSERT INTO problems(contest_id,api_id,code,title,statement,contributors,color,sample_input,sample_output,time_limit_ms,memory_limit_mb,ordinal,difficulty,tags,source,internal_notes,points,enabled,reference_solution,reference_language,validator_source)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",(contest["id"],api_id,code,title,statement,contributors,color.lower(),sample_input,sample_output,time_limit_ms,memory_limit_mb,ordinal,difficulty,tags,source_name,internal_notes,points,enabled,reference_solution,reference_language,validator_source))
            p=conn.execute("SELECT * FROM problems WHERE id=?",(cur.lastrowid,)).fetchone(); tests=request.files.get("tests_zip")
            if tests and tests.filename: safe_extract_tests_zip(tests,problem_tests_dir(p))
            elif sample_input or sample_output:
                td=problem_tests_dir(p); td.mkdir(parents=True,exist_ok=True); (td/"input01.txt").write_text(sample_input,encoding="utf-8"); (td/"output01.txt").write_text(sample_output,encoding="utf-8")
            else: raise ValueError("Provide a tests ZIP or at least one sample input/output pair.")
            append_event(conn,contest["id"],"problems",p["api_id"],api_problem(p)); fx.audit(conn,"problem.create",contest_id=contest["id"],object_type="problem",object_id=p["id"],detail=f"{code} {title}"); conn.commit(); flash(f"Problem {code} added to {contest['name']}.","success"); return redirect(url_for("admin_problems"))
        except (ValueError,sqlite3.IntegrityError,zipfile.BadZipFile) as exc:
            conn.rollback(); flash(str(exc),"danger")
    problems=contest_problems(contest["id"],conn)
    validations={r["problem_id"]:r for r in conn.execute("SELECT pv.* FROM problem_validations pv JOIN (SELECT problem_id,MAX(id) mid FROM problem_validations GROUP BY problem_id) x ON x.mid=pv.id").fetchall()}
    return render_template("admin_problems.html",problems=problems,count_tests=count_tests,validations=validations)


@app.post("/admin/problems/<int:pid>/update")
@fx._staff_permission("problems")
def admin_update_problem(pid):
    conn=get_db(); p=conn.execute("SELECT * FROM problems WHERE id=?",(pid,)).fetchone()
    if not p: abort(404)
    try:
        title=request.form.get("title",p["title"]).strip() or p["title"]; statement=request.form.get("statement",p["statement"]).strip() or p["statement"]; contributors=request.form.get("contributors",p["contributors"]).strip(); color=request.form.get("color",p["color"]).strip()
        if not HEX_COLOR_RE.fullmatch(color): raise ValueError("Problem color must be a six-digit hex color.")
        sample_input=request.form.get("sample_input",p["sample_input"]); sample_output=request.form.get("sample_output",p["sample_output"]); time_limit_ms=max(100,min(60000,int(request.form.get("time_limit_ms",p["time_limit_ms"])))); memory_limit_mb=max(16,min(16384,int(request.form.get("memory_limit_mb",p["memory_limit_mb"])))); ordinal=int(request.form.get("ordinal",p["ordinal"])); difficulty=request.form.get("difficulty",p["difficulty"]); tags=request.form.get("tags",p["tags"]).strip(); source_name=request.form.get("source",p["source"]).strip(); internal_notes=request.form.get("internal_notes",p["internal_notes"]).strip(); points=max(0,float(request.form.get("points",p["points"]))); enabled=1 if request.form.get("enabled")=="on" else 0; reference_solution=request.form.get("reference_solution",p["reference_solution"]); reference_language=request.form.get("reference_language",p["reference_language"]); validator_source=request.form.get("validator_source",p["validator_source"])
        if reference_language and reference_language not in LANGUAGES: raise ValueError("Reference solution language is unsupported.")
        conn.execute("""UPDATE problems SET title=?,statement=?,contributors=?,color=?,sample_input=?,sample_output=?,time_limit_ms=?,memory_limit_mb=?,ordinal=?,difficulty=?,tags=?,source=?,internal_notes=?,points=?,enabled=?,reference_solution=?,reference_language=?,validator_source=? WHERE id=?""",(title,statement,contributors,color.lower(),sample_input,sample_output,time_limit_ms,memory_limit_mb,ordinal,difficulty,tags,source_name,internal_notes,points,enabled,reference_solution,reference_language,validator_source,pid))
        tests=request.files.get("tests_zip")
        if tests and tests.filename: safe_extract_tests_zip(tests,problem_tests_dir(p))
        updated=conn.execute("SELECT * FROM problems WHERE id=?",(pid,)).fetchone(); append_event(conn,p["contest_id"],"problems",p["api_id"],api_problem(updated)); fx.audit(conn,"problem.update",contest_id=p["contest_id"],object_type="problem",object_id=pid,detail=p["code"]); conn.commit(); flash(f"Problem {p['code']} updated.","success")
    except (ValueError,zipfile.BadZipFile) as exc:
        conn.rollback(); flash(str(exc),"danger")
    return redirect(url_for("admin_problems"))

@app.post("/admin/problems/<int:pid>/delete")
@fx._staff_permission("problems")
def admin_delete_problem(pid):
    conn = get_db()
    p = conn.execute("SELECT * FROM problems WHERE id=?", (pid,)).fetchone()
    if not p:
        abort(404)
    if conn.execute("SELECT COUNT(*) FROM submissions WHERE problem_id=?", (pid,)).fetchone()[0]:
        flash("Problem cannot be deleted after submissions exist.", "danger")
        return redirect(url_for("admin_problems"))
    conn.execute("DELETE FROM problems WHERE id=?", (pid,))
    append_event(conn, p["contest_id"], "problems", p["api_id"], None)
    conn.commit()
    flash(f"Problem {p['code']} deleted.", "success")
    return redirect(url_for("admin_problems"))


@app.route("/admin/participants", methods=["GET", "POST"])
@fx._staff_permission("participants")
def admin_participants():
    conn=get_db(); contest=active_contest(conn)
    if request.method=="POST":
        try:
            username=request.form.get("username","").strip().lower(); email=request.form.get("email","").strip().lower(); password=request.form.get("password","").strip(); display_name=request.form.get("display_name","").strip(); affiliation=request.form.get("affiliation","").strip(); department=request.form.get("department","").strip(); college=request.form.get("college","").strip(); year_level=request.form.get("year_level","").strip(); team_name=request.form.get("team_name","").strip() if contest["competition_mode"]=="team" else ""
            if not re.fullmatch(r"[a-z0-9][a-z0-9_.-]{2,31}",username): raise ValueError("Username must be 3-32 characters using lowercase letters, numbers, ., _, or -.")
            if email and not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+",email): raise ValueError("Enter a valid email address.")
            if len(password)<6: raise ValueError("Initial password must contain at least 6 characters.")
            if not display_name: raise ValueError("Participant name is required.")
            if not department: raise ValueError("Department / program is required.")
            if team_name:
                ok,_=fx.team_size_ok(conn,contest,team_name)
                if not ok: raise ValueError(f"Team already has the maximum of {contest['max_team_size']} members.")
            iid=None
            if affiliation:
                inst=conn.execute("SELECT id FROM institutions WHERE lower(name)=lower(?) OR lower(short_name)=lower(?)",(affiliation,affiliation)).fetchone(); iid=inst["id"] if inst else None
            cur=conn.execute("INSERT INTO accounts(username,email,password_hash,display_name,affiliation,department,college,year_level,institution_id,role,created_at) VALUES(?,?,?,?,?,?,?,?,?, 'participant', ?)",(username,email,generate_password_hash(password),display_name,affiliation,department,college,year_level,iid,iso(utcnow()))); aid=cur.lastrowid
            conn.execute("INSERT INTO registrations(contest_id,account_id,team_api_id,team_name,approval_status,registration_status,seat,workstation,coach_name,adviser_name,temporary_password) VALUES(?,?,?,?,?,?,?,?,?,?,1)",(contest["id"],aid,f"p-{aid}",team_name,"APPROVED","REGISTERED",request.form.get("seat","").strip(),request.form.get("workstation","").strip(),request.form.get("coach_name","").strip(),request.form.get("adviser_name","").strip()))
            fx.audit(conn,"participant.create",contest_id=contest["id"],object_type="account",object_id=aid,detail=display_name); conn.commit(); flash("Participant account created and registered.","success"); return redirect(url_for("admin_participants"))
        except sqlite3.IntegrityError:
            conn.rollback(); flash("That username or email already exists.","danger")
        except ValueError as exc:
            conn.rollback(); flash(str(exc),"danger")
    participants=conn.execute("""SELECT a.*, CASE WHEN r.account_id IS NULL THEN 0 ELSE 1 END registered,
              COALESCE(r.team_name,'') team_name,COALESCE(r.disqualified,0) disqualified,
              COALESCE(r.disqualification_reason,'') disqualification_reason,
              COALESCE(r.registration_status,'') registration_status,COALESCE(r.approval_status,'') approval_status,
              COALESCE(r.seat,'') seat,COALESCE(r.workstation,'') workstation,COALESCE(r.coach_name,'') coach_name,
              COALESCE(r.adviser_name,'') adviser_name,COALESCE(r.notes,'') registration_notes,COALESCE(r.temporary_password,0) temporary_password
       FROM accounts a LEFT JOIN registrations r ON r.account_id=a.id AND r.contest_id=?
       WHERE a.role='participant' ORDER BY a.display_name""",(contest["id"],)).fetchall()
    institutions=conn.execute("SELECT * FROM institutions WHERE active=1 ORDER BY name").fetchall()
    return render_template("admin_participants.html",participants=participants,contest=contest,registration_statuses=fx.REGISTRATION_STATUSES,institutions=institutions)

@app.post("/admin/participants/<int:aid>/toggle")
@fx._staff_permission("participants")
def admin_toggle_registration(aid):
    conn = get_db()
    contest = active_contest(conn)
    acct = conn.execute("SELECT * FROM accounts WHERE id=? AND role='participant'", (aid,)).fetchone()
    if not acct:
        abort(404)
    reg = participant_registered(contest["id"], aid, conn)
    if reg:
        if conn.execute("SELECT COUNT(*) FROM submissions WHERE contest_id=? AND account_id=?", (contest["id"], aid)).fetchone()[0]:
            flash("Cannot unregister a participant who already has submissions.", "danger")
        else:
            conn.execute("DELETE FROM registrations WHERE contest_id=? AND account_id=?", (contest["id"], aid))
            conn.commit()
            flash("Participant unregistered from active contest.", "success")
    else:
        conn.execute("INSERT INTO registrations(contest_id,account_id,team_api_id) VALUES(?,?,?)", (contest["id"], aid, f"p-{aid}"))
        conn.commit()
        flash("Participant registered for active contest.", "success")
    return redirect(url_for("admin_participants"))


@app.post("/admin/participants/<int:aid>/profile")
@fx._staff_permission("participants")
def admin_update_participant_profile(aid):
    conn=get_db(); contest=active_contest(conn); acct=conn.execute("SELECT * FROM accounts WHERE id=? AND role='participant'",(aid,)).fetchone()
    if not acct: abort(404)
    display_name=request.form.get("display_name",acct["display_name"]).strip() or acct["display_name"]
    username=request.form.get("username",acct["username"]).strip().lower()
    email=request.form.get("email",acct["email"] or "").strip().lower()
    department=request.form.get("department",acct["department"]).strip(); affiliation=request.form.get("affiliation",acct["affiliation"]).strip(); college=request.form.get("college",acct["college"]).strip(); year_level=request.form.get("year_level",acct["year_level"]).strip()
    if not re.fullmatch(r"[a-z0-9][a-z0-9_.-]{2,31}",username): flash("Username must be 3-32 characters using lowercase letters, numbers, ., _, or -.","danger"); return redirect(url_for("admin_participants"))
    if email and not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+",email): flash("Enter a valid email address.","danger"); return redirect(url_for("admin_participants"))
    if not department: flash("Department / program is required.","danger"); return redirect(url_for("admin_participants"))
    iid=None
    if affiliation:
        inst=conn.execute("SELECT id FROM institutions WHERE lower(name)=lower(?) OR lower(short_name)=lower(?)",(affiliation,affiliation)).fetchone(); iid=inst["id"] if inst else None
    try:
        conn.execute("UPDATE accounts SET display_name=?,username=?,email=?,department=?,affiliation=?,college=?,year_level=?,institution_id=? WHERE id=?",(display_name,username,email,department,affiliation,college,year_level,iid,aid)); fx.audit(conn,"participant.profile",contest_id=contest["id"],object_type="account",object_id=aid,detail=display_name); conn.commit(); flash("Contestant details updated.","success")
    except sqlite3.IntegrityError:
        conn.rollback(); flash("That username or email is already used by another account.","danger")
    return redirect(url_for("admin_participants"))


@app.post("/admin/participants/<int:aid>/team")
@fx._staff_permission("participants")
def admin_set_participant_team(aid):
    conn = get_db()
    contest = active_contest(conn)
    if contest["competition_mode"] != "team":
        flash("Team assignment is only available when the active contest uses Team leaderboard mode.", "danger")
        return redirect(url_for("admin_participants"))
    acct = conn.execute("SELECT * FROM accounts WHERE id=? AND role='participant'", (aid,)).fetchone()
    if not acct:
        abort(404)
    reg = participant_registered(contest["id"], aid, conn)
    if not reg:
        flash("Register the participant before assigning a team.", "danger")
        return redirect(url_for("admin_participants"))
    team_name = request.form.get("team_name", "").strip()[:120]
    if team_name:
        ok, current = fx.team_size_ok(conn, contest, team_name, excluding_account=aid)
        if not ok:
            flash(f"That team already has the maximum of {contest['max_team_size']} members.", "danger")
            return redirect(url_for("admin_participants"))
    conn.execute("UPDATE registrations SET team_name=? WHERE contest_id=? AND account_id=?", (team_name, contest["id"], aid))
    fx.audit(conn,"team.assign",contest_id=contest["id"],object_type="account",object_id=aid,detail=team_name)
    conn.commit()
    flash("Team assignment updated.", "success")
    return redirect(url_for("admin_participants"))


@app.post("/admin/participants/<int:aid>/disqualification")
@fx._staff_permission("dq")
def admin_disqualification(aid):
    conn = get_db()
    contest = active_contest(conn)
    acct = conn.execute("SELECT * FROM accounts WHERE id=? AND role='participant'", (aid,)).fetchone()
    if not acct:
        abort(404)
    action = request.form.get("action", "disqualify")
    try:
        if action == "reinstate":
            affected = set_disqualification(contest, aid, disqualified=False, actor_id=current_user()["id"], conn=conn)
            conn.commit()
            flash(f"Reinstated {'team' if contest['competition_mode']=='team' and affected>1 else 'contestant'}.", "success")
        else:
            reason = request.form.get("reason", "").strip()
            evidence = request.form.get("evidence_notes", "").strip()
            affected = set_disqualification(contest, aid, disqualified=True, reason=reason, evidence_notes=evidence, actor_id=current_user()["id"], conn=conn)
            conn.commit()
            flash(f"Disqualified {'team' if contest['competition_mode']=='team' and affected>1 else 'contestant'}. Submissions are now blocked.", "success")
    except ValueError as exc:
        conn.rollback()
        flash(str(exc), "danger")
    return redirect(url_for("admin_participants"))


@app.post("/judge/disqualification")
@judge_required
def judge_disqualification():
    conn = get_db()
    contest = active_contest(conn)
    try:
        aid = int(request.form.get("account_id", "0"))
    except ValueError:
        aid = 0
    acct = conn.execute("SELECT * FROM accounts WHERE id=? AND role='participant'", (aid,)).fetchone()
    if not acct or not participant_registered(contest["id"], aid, conn):
        flash("Select a registered contestant.", "danger")
        return redirect(url_for("judge_console"))
    action = request.form.get("action", "disqualify")
    try:
        if action == "reinstate":
            affected = set_disqualification(contest, aid, disqualified=False, actor_id=current_user()["id"], conn=conn)
            conn.commit()
            flash(f"Reinstated {'team' if contest['competition_mode']=='team' and affected>1 else 'contestant'}.", "success")
        else:
            reason = request.form.get("reason", "").strip()
            evidence = request.form.get("evidence_notes", "").strip()
            affected = set_disqualification(contest, aid, disqualified=True, reason=reason, evidence_notes=evidence, actor_id=current_user()["id"], conn=conn)
            conn.commit()
            flash(f"Disqualification recorded for {'team' if contest['competition_mode']=='team' and affected>1 else 'contestant'}.", "success")
    except ValueError as exc:
        conn.rollback()
        flash(str(exc), "danger")
    return redirect(url_for("judge_console"))


@app.route("/admin/api")
@fx._staff_permission("api")
def admin_api():
    contest = active_contest()
    return render_template("admin_api.html", contest=contest)


# ---------------- ICPC Contest API draft-aligned subset ----------------
JUDGEMENT_TYPES = [
    {"id":"AC","name":"Accepted","penalty":False,"solved":True},
    {"id":"WA","name":"Wrong Answer","penalty":True,"solved":False},
    {"id":"TLE","name":"Time Limit Exceeded","penalty":True,"solved":False},
    {"id":"RTE","name":"Run-Time Error","penalty":True,"solved":False},
    {"id":"CE","name":"Compile Error","penalty":False,"solved":False},
    {"id":"JE","name":"Judging Error","penalty":False,"solved":False},
]


def api_contest(c):
    obj = {
        "id": c["id"], "name": c["name"], "formal_name": c["formal_name"],
        "start_time": c["start_at"], "duration": reltime(c["duration_minutes"] * 60),
        "scoreboard_freeze_duration": reltime(c["freeze_minutes"] * 60),
        "scoreboard_type": "pass-fail", "penalty_time": reltime(c["penalty_minutes"] * 60),
        "logo": [{"href":"/api/logo","filename":"inquisitor-mark.png","mime":"image/png","width":880,"height":880,"tags":["dark"]}],
    }
    return obj


def api_language(key):
    lang = LANGUAGES[key]
    extensions = [lang.extension.lstrip("."), *[x.lstrip(".") for x in lang.aliases]]
    obj = {
        "id": key,
        "name": lang.label,
        "entry_point_required": key in {"java", "kotlin", "python3", "ruby"},
        "extensions": extensions,
    }
    if key == "java":
        obj["entry_point_name"] = "Main class"
        obj["compiler"] = {"command": "javac"}
        obj["runner"] = {"command": "java"}
    elif key == "kotlin":
        obj["entry_point_name"] = "Main class"
        obj["compiler"] = {"command": "kotlinc"}
        obj["runner"] = {"command": "java"}
    elif key == "python3":
        obj["entry_point_name"] = "Main file"
        obj["runner"] = {"command": "python3"}
    elif key == "ruby":
        obj["entry_point_name"] = "Main file"
        obj["runner"] = {"command": "ruby"}
    elif key == "rust":
        obj["compiler"] = {"command": "rustc", "args": "-O {files}"}
    elif key == "cpp":
        obj["compiler"] = {"command": "g++", "args": "-std=c++17 -O2 {files}"}
    elif key == "c":
        obj["compiler"] = {"command": "gcc", "args": "-std=c17 -O2 {files}"}
    return obj


def api_problem(p):
    return {
        "id": p["api_id"], "label": p["code"], "name": p["title"], "ordinal": p["ordinal"],
        "time_limit": p["time_limit_ms"] / 1000.0, "memory_limit": p["memory_limit_mb"],
        "output_limit": p["output_limit_mb"], "code_limit": p["code_limit_kb"], "test_data_count": count_tests(p)
    }


def team_id_for_account(contest_id, account_id, conn):
    reg = conn.execute("SELECT team_api_id, COALESCE(team_name,'') team_name FROM registrations WHERE contest_id=? AND account_id=?", (contest_id, account_id)).fetchone()
    if not reg:
        return None
    contest = contest_by_id(contest_id, conn)
    if contest and contest["competition_mode"] == "team" and reg["team_name"].strip():
        return _team_api_id_from_name(reg["team_name"])
    return reg["team_api_id"]


def api_team(a, contest_id, conn):
    reg = conn.execute("SELECT team_api_id, COALESCE(team_name,'') team_name FROM registrations WHERE contest_id=? AND account_id=?", (contest_id, a["id"])).fetchone()
    contest = contest_by_id(contest_id, conn)
    if contest and contest["competition_mode"] == "team" and reg and reg["team_name"].strip():
        name = " ".join(reg["team_name"].split())
        tid = _team_api_id_from_name(name)
        members = conn.execute(
            """SELECT a.* FROM registrations r JOIN accounts a ON a.id=r.account_id
               WHERE r.contest_id=? AND lower(trim(r.team_name))=lower(trim(?)) ORDER BY a.display_name""",
            (contest_id, reg["team_name"])
        ).fetchall()
        affiliations = [m["affiliation"] for m in members if m["affiliation"]]
        affiliation = affiliations[0] if affiliations and all(x == affiliations[0] for x in affiliations) else ""
        obj = {"id": tid, "label": tid, "name": name}
        if affiliation: obj["affiliation"] = affiliation
        orgs={_organization_id_for_account(m,conn) for m in members}; orgs.discard(None)
        if len(orgs)==1: obj["organization_id"]=next(iter(orgs))
        departments=sorted({m["department"].strip() for m in members if "department" in m.keys() and m["department"].strip()})
        if departments: obj["group_ids"]=[_api_group_id(d) for d in departments]
        return obj
    tid = reg["team_api_id"] if reg else None
    obj={"id": tid, "label": a["username"], "name": a["display_name"]}
    org=_organization_id_for_account(a,conn)
    if org: obj["organization_id"]=org
    if "department" in a.keys() and a["department"].strip(): obj["group_ids"]=[_api_group_id(a["department"].strip())]
    return obj


def api_person(a, contest_id, conn):
    tid = team_id_for_account(contest_id, a["id"], conn)
    return {"id": f"person-{a['id']}", "name": a["display_name"], "roles":[{"type":"contestant","team_id":tid}]}


def _api_group_id(name: str) -> str:
    slug=re.sub(r"[^a-z0-9]+","-",(name or "").casefold()).strip("-")[:32] or "unspecified"
    digest=hashlib.sha1((name or "unspecified").encode("utf-8")).hexdigest()[:8]
    return f"dept-{slug}-{digest}"


def api_organization(row):
    obj={"id":f"org-{row['id']}","name":row['name'],"formal_name":row['name']}
    if row['country']: obj['country']=row['country']
    return obj


def _organization_id_for_account(a, conn):
    if 'institution_id' in a.keys() and a['institution_id']:
        row=conn.execute("SELECT id FROM institutions WHERE id=?",(a['institution_id'],)).fetchone()
        if row: return f"org-{row['id']}"
    if a['affiliation']:
        row=conn.execute("SELECT id FROM institutions WHERE lower(name)=lower(?) OR lower(short_name)=lower(?)",(a['affiliation'],a['affiliation'])).fetchone()
        if row: return f"org-{row['id']}"
    return None


def api_group(name: str):
    return {"id":_api_group_id(name),"name":name,"type":"department"}


def api_account(a, contest_id, conn):
    obj = {"id":f"account-{a['id']}","username":a["username"],"name":a["display_name"],"type":"team" if a["role"]=="participant" else a["role"]}
    if a["role"] == "participant":
        obj["team_id"] = team_id_for_account(contest_id, a["id"], conn)
        obj["person_id"] = f"person-{a['id']}"
    return obj


def api_submission(s, contest, conn):
    start, _end, _freeze = contest_times(contest)
    when = parse_dt(s["submitted_at"])
    ctime = reltime((when - start).total_seconds()) if start else "0:00:00"
    p = conn.execute("SELECT * FROM problems WHERE id=?", (s["problem_id"],)).fetchone()
    obj = {
        "id": str(s["id"]), "language_id": s["language"], "problem_id": p["api_id"],
        "team_id": team_id_for_account(contest["id"], s["account_id"], conn),
        "account_id": f"account-{s['account_id']}", "time": s["submitted_at"], "contest_time": ctime,
        "files": [{"href":f"contests/{contest['id']}/submissions/{s['id']}/files","filename":"files.zip","mime":"application/zip"}]
    }
    if s["entry_point"]:
        obj["entry_point"] = s["entry_point"]
    return obj


def api_judgement(s):
    if not s["judging_started_at"]:
        return None
    obj = {"id":f"j-{s['id']}","submission_id":str(s["id"]),"current":True,"start_time":s["judging_started_at"]}
    if s["status"] == "DONE" and s["verdict"]:
        obj["judgement_type_id"] = s["verdict"] if s["verdict"] in {x["id"] for x in JUDGEMENT_TYPES} else "JE"
        obj["end_time"] = s["judged_at"] or s["judging_started_at"]
        if s["runtime_ms"] is not None:
            obj["max_run_time"] = round(s["runtime_ms"] / 1000.0, 3)
    return obj


def api_state(contest):
    st = contest_state(contest)
    started = iso(st["start"]) if st["start"] and utcnow() >= st["start"] else None
    ended = iso(st["end"]) if st["end"] and utcnow() >= st["end"] else None
    frozen = iso(st["freeze_at"]) if st["freeze_at"] and utcnow() >= st["freeze_at"] else None
    thawed = contest["scoreboard_thawed_at"] if contest["scoreboard_revealed"] and frozen else None
    return {"started":started,"frozen":frozen,"ended":ended,"thawed":thawed,"finalized":None,"end_of_updates":None}


def api_clarification_question(c, contest, conn):
    start, _end, _freeze = contest_times(contest)
    when = parse_dt(c["created_at"])
    p = conn.execute("SELECT * FROM problems WHERE id=?", (c["problem_id"],)).fetchone() if c["problem_id"] else None
    obj = {
        "id":f"q-{c['id']}", "from_team_id":team_id_for_account(contest["id"], c["account_id"], conn),
        "text":c["question"], "time":c["created_at"],
        "contest_time":reltime((when-start).total_seconds()) if start else "0:00:00"
    }
    if p: obj["problem_id"] = p["api_id"]
    return obj

def api_clarification_answer(c, contest, conn):
    if not c["answer"] or not c["answered_at"]:
        return None
    start, _end, _freeze = contest_times(contest)
    when = parse_dt(c["answered_at"])
    p = conn.execute("SELECT * FROM problems WHERE id=?", (c["problem_id"],)).fetchone() if c["problem_id"] else None
    obj = {
        "id": f"a-{c['id']}", "from_team_id": None,
        "text": c["answer"], "time": c["answered_at"],
        "contest_time": reltime((when-start).total_seconds()) if start else "0:00:00"
    }
    if c["broadcast"]:
        obj["to_team_ids"] = None
        obj["to_group_ids"] = None
        obj["reply_to_id"] = None
    else:
        obj["to_team_ids"] = [team_id_for_account(contest["id"], c["account_id"], conn)]
        obj["to_group_ids"] = None
        obj["reply_to_id"] = f"q-{c['id']}"
    if p: obj["problem_id"] = p["api_id"]
    return obj


def api_auth_account(conn):
    auth = request.authorization
    if auth and auth.type and auth.type.lower() == "basic" and auth.username:
        a = conn.execute("SELECT * FROM accounts WHERE username=?", (auth.username.lower(),)).fetchone()
        if a and check_password_hash(a["password_hash"], auth.password or ""):
            return a
    return current_user()


def api_error(code, message):
    return jsonify(code=code, message=message), code


def api_access(contest, account, conn):
    public = [
        ("contest", ["id","name","formal_name","start_time","duration","scoreboard_freeze_duration","scoreboard_type","penalty_time","logo"]),
        ("judgement-types", ["id","name","penalty","solved"]),
        ("languages", ["id","name","entry_point_required","entry_point_name","extensions","compiler","runner"]),
        ("problems", ["id","label","name","ordinal","time_limit","memory_limit","output_limit","code_limit","test_data_count"]),
        ("groups", ["id","name","type"]), ("organizations", ["id","name","formal_name","country"]),
        ("teams", ["id","label","name","organization_id","group_ids"]), ("persons", ["id","name","roles"]),
        ("state", ["started","frozen","ended","thawed","finalized","end_of_updates"]),
        ("scoreboard", ["time","contest_time","state","rows"]),
    ]
    caps = []
    endpoints = list(public)
    if account:
        endpoints.append(("account", ["id","username","name","type","team_id","person_id"]))
        if account["role"] == "participant" and participant_registered(contest["id"], account["id"], conn):
            caps += ["team_submit", "post_clar"]
            endpoints += [("submissions", ["id","language_id","problem_id","team_id","account_id","time","contest_time","entry_point","files"]), ("judgements", ["id","submission_id","judgement_type_id","current","start_time","end_time","max_run_time"]), ("clarifications", ["id","from_team_id","to_team_ids","reply_to_id","problem_id","text","time","contest_time"]), ("awards", ["id","citation","team_ids"])]
        elif account["role"] in {"judge","admin"}:
            if account["role"] == "admin":
                caps += ["contest_start", "contest_thaw"]
            endpoints += [("accounts", ["id","username","name","type","team_id","person_id"]), ("submissions", ["id","language_id","problem_id","team_id","account_id","time","contest_time","entry_point","files"]), ("judgements", ["id","submission_id","judgement_type_id","current","start_time","end_time","max_run_time"]), ("clarifications", ["id","from_team_id","to_team_ids","reply_to_id","problem_id","text","time","contest_time"]), ("awards", ["id","citation","team_ids"]), ("event-feed", ["type","id","data","token"])]
    return {"capabilities":caps,"endpoints":[{"type":t,"properties":p} for t,p in endpoints]}


@app.after_request
def api_headers(resp):
    if request.path.startswith("/api/"):
        resp.headers["Access-Control-Allow-Origin"] = "*"
        resp.headers["Cache-Control"] = "no-store"
    return resp


@app.get("/api/")
def api_root():
    return jsonify({"version":"draft","version_url":"https://ccs-specs.icpc.io/draft/contest_api","provider":{"name":"Inquisitor","version":"1.0.8","logo":[{"href":"/api/logo","filename":"inquisitor-logo.png","mime":"image/png","width":540,"height":1080,"tags":["dark"]}]}})


@app.get("/api/logo")
def api_logo():
    return send_file(BASE_DIR / "static" / "inquisitor_logo_white.png", mimetype="image/png")


@app.get("/api/contests")
def api_contests_list():
    return jsonify([api_contest(c) for c in get_db().execute("SELECT * FROM contests ORDER BY created_at")])


@app.route("/api/contests/<cid>", methods=["GET","PATCH"])
def api_contest_one(cid):
    conn = get_db(); c = contest_by_id(cid, conn)
    if not c: return api_error(404, "Contest not found")
    if request.method == "GET": return jsonify(api_contest(c))
    a = api_auth_account(conn)
    if not a or a["role"] != "admin": return api_error(403, "contest_start capability required")
    data = request.get_json(silent=True) or {}
    if data.get("id") != cid: return api_error(409, "id must match URL")
    if "start_time" in data:
        conn.execute("UPDATE contests SET start_at=? WHERE id=?", (data["start_time"], cid))
    if "scoreboard_thaw_time" in data:
        conn.execute("UPDATE contests SET scoreboard_revealed=1, scoreboard_thawed_at=? WHERE id=?", (iso(utcnow()), cid))
    updated = contest_by_id(cid, conn)
    append_event(conn, cid, "contest", None, api_contest(updated)); conn.commit()
    return jsonify(api_contest(updated))


@app.get("/api/contests/<cid>/access")
def api_access_route(cid):
    conn = get_db(); c = contest_by_id(cid, conn)
    if not c: return api_error(404, "Contest not found")
    return jsonify(api_access(c, api_auth_account(conn), conn))


@app.get("/api/contests/<cid>/judgement-types")
def api_judgement_types(cid):
    if not contest_by_id(cid): return api_error(404, "Contest not found")
    return jsonify(JUDGEMENT_TYPES)


@app.get("/api/contests/<cid>/judgement-types/<jid>")
def api_judgement_type_one(cid, jid):
    if not contest_by_id(cid): return api_error(404, "Contest not found")
    obj=next((x for x in JUDGEMENT_TYPES if x["id"]==jid),None)
    if not obj:return api_error(404,"Judgement type not found")
    return jsonify(obj)


@app.get("/api/contests/<cid>/languages")
def api_languages(cid):
    if not contest_by_id(cid): return api_error(404, "Contest not found")
    return jsonify([api_language(k) for k in LANGUAGES])


@app.get("/api/contests/<cid>/languages/<lid>")
def api_language_one(cid, lid):
    if not contest_by_id(cid): return api_error(404, "Contest not found")
    if lid not in LANGUAGES: return api_error(404, "Language not found")
    return jsonify(api_language(lid))


@app.get("/api/contests/<cid>/problems")
def api_problems(cid):
    if not contest_by_id(cid): return api_error(404, "Contest not found")
    return jsonify([api_problem(p) for p in contest_problems(cid)])


@app.get("/api/contests/<cid>/problems/<pid>")
def api_problem_one(cid, pid):
    p = get_db().execute("SELECT * FROM problems WHERE contest_id=? AND api_id=?", (cid,pid)).fetchone()
    if not p: return api_error(404, "Problem not found")
    return jsonify(api_problem(p))


@app.get("/api/contests/<cid>/organizations")
def api_organizations(cid):
    conn=get_db(); c=contest_by_id(cid,conn)
    if not c: return api_error(404,"Contest not found")
    ids={r["institution_id"] for r in conn.execute("SELECT a.institution_id FROM registrations r JOIN accounts a ON a.id=r.account_id WHERE r.contest_id=? AND a.institution_id IS NOT NULL",(cid,))}
    rows=conn.execute(f"SELECT * FROM institutions WHERE id IN ({','.join('?' for _ in ids)}) ORDER BY name",list(ids)).fetchall() if ids else []
    return jsonify([api_organization(r) for r in rows])


@app.get("/api/contests/<cid>/organizations/<oid>")
def api_organization_one(cid,oid):
    if not contest_by_id(cid): return api_error(404,"Contest not found")
    m=re.fullmatch(r"org-(\d+)",oid)
    if not m: return api_error(404,"Organization not found")
    row=get_db().execute("SELECT * FROM institutions WHERE id=?",(int(m.group(1)),)).fetchone()
    if not row: return api_error(404,"Organization not found")
    return jsonify(api_organization(row))


@app.get("/api/contests/<cid>/groups")
def api_groups(cid):
    conn=get_db(); c=contest_by_id(cid,conn)
    if not c: return api_error(404,"Contest not found")
    names=sorted({r["department"].strip() for r in conn.execute("SELECT a.department FROM registrations r JOIN accounts a ON a.id=r.account_id WHERE r.contest_id=?",(cid,)) if r["department"].strip()}, key=str.casefold)
    return jsonify([api_group(n) for n in names])


@app.get("/api/contests/<cid>/groups/<gid>")
def api_group_one(cid,gid):
    conn=get_db(); c=contest_by_id(cid,conn)
    if not c: return api_error(404,"Contest not found")
    names={_api_group_id(r["department"].strip()):r["department"].strip() for r in conn.execute("SELECT a.department FROM registrations r JOIN accounts a ON a.id=r.account_id WHERE r.contest_id=?",(cid,)) if r["department"].strip()}
    if gid not in names: return api_error(404,"Group not found")
    return jsonify(api_group(names[gid]))


@app.get("/api/contests/<cid>/accounts")
def api_accounts(cid):
    conn=get_db(); c=contest_by_id(cid,conn); a=api_auth_account(conn)
    if not c: return api_error(404,"Contest not found")
    if not a or a["role"] not in {"judge","admin"}: return api_error(403,"Accounts endpoint is restricted")
    participants=conn.execute("SELECT a.* FROM registrations r JOIN accounts a ON a.id=r.account_id WHERE r.contest_id=? ORDER BY a.id",(cid,)).fetchall()
    staff=conn.execute("SELECT DISTINCT a.* FROM staff_assignments s JOIN accounts a ON a.id=s.account_id WHERE s.contest_id=? ORDER BY a.id",(cid,)).fetchall()
    seen={};
    for row in [*participants,*staff]: seen[row["id"]]=api_account(row,cid,conn)
    return jsonify(list(seen.values()))


@app.get("/api/contests/<cid>/accounts/<aid>")
def api_account_one(cid,aid):
    conn=get_db(); c=contest_by_id(cid,conn); me=api_auth_account(conn)
    if not c: return api_error(404,"Contest not found")
    if not me or me["role"] not in {"judge","admin"}: return api_error(403,"Accounts endpoint is restricted")
    m=re.fullmatch(r"account-(\d+)",aid)
    if not m: return api_error(404,"Account not found")
    row=conn.execute("SELECT * FROM accounts WHERE id=?",(int(m.group(1)),)).fetchone()
    if not row: return api_error(404,"Account not found")
    return jsonify(api_account(row,cid,conn))


@app.get("/api/contests/<cid>/teams")
def api_teams(cid):
    conn=get_db(); c=contest_by_id(cid,conn)
    if not c: return api_error(404,"Contest not found")
    rows=conn.execute("SELECT a.* FROM registrations r JOIN accounts a ON a.id=r.account_id WHERE r.contest_id=? ORDER BY a.display_name",(cid,)).fetchall()
    teams = {}
    for a in rows:
        obj = api_team(a, cid, conn)
        teams[obj["id"]] = obj
    return jsonify(list(teams.values()))


@app.get("/api/contests/<cid>/teams/<tid>")
def api_team_one(cid, tid):
    conn=get_db(); c=contest_by_id(cid,conn)
    if not c: return api_error(404,"Contest not found")
    rows=conn.execute("SELECT a.* FROM registrations r JOIN accounts a ON a.id=r.account_id WHERE r.contest_id=? ORDER BY a.display_name",(cid,)).fetchall()
    for a in rows:
        obj = api_team(a, cid, conn)
        if obj["id"] == tid:
            return jsonify(obj)
    return api_error(404,"Team not found")


@app.get("/api/contests/<cid>/persons")
def api_persons(cid):
    conn=get_db(); c=contest_by_id(cid,conn)
    if not c: return api_error(404,"Contest not found")
    rows=conn.execute("SELECT a.* FROM registrations r JOIN accounts a ON a.id=r.account_id WHERE r.contest_id=? ORDER BY a.display_name",(cid,)).fetchall()
    return jsonify([api_person(a,cid,conn) for a in rows])


@app.get("/api/contests/<cid>/persons/<pid>")
def api_person_one(cid, pid):
    conn=get_db(); c=contest_by_id(cid,conn)
    if not c:return api_error(404,"Contest not found")
    m=re.fullmatch(r"person-(\d+)",pid)
    if not m:return api_error(404,"Person not found")
    a=conn.execute("SELECT a.* FROM registrations r JOIN accounts a ON a.id=r.account_id WHERE r.contest_id=? AND a.id=?",(cid,int(m.group(1)))).fetchone()
    if not a:return api_error(404,"Person not found")
    return jsonify(api_person(a,cid,conn))


@app.get("/api/contests/<cid>/account")
def api_current_account_route(cid):
    conn=get_db(); c=contest_by_id(cid,conn)
    if not c: return api_error(404,"Contest not found")
    a=api_auth_account(conn)
    if not a: return api_error(401,"Authentication required")
    return jsonify(api_account(a,cid,conn))


@app.get("/api/contests/<cid>/state")
def api_state_route(cid):
    c=contest_by_id(cid)
    if not c: return api_error(404,"Contest not found")
    return jsonify(api_state(c))


@app.route("/api/contests/<cid>/submissions", methods=["GET","POST"])
def api_submissions(cid):
    conn=get_db(); c=contest_by_id(cid,conn)
    if not c: return api_error(404,"Contest not found")
    a=api_auth_account(conn)
    if request.method == "POST":
        reg = participant_registered(cid, a["id"], conn) if a and a["role"] == "participant" else None
        if not a or a["role"] != "participant" or not reg: return api_error(403,"team_submit capability required")
        if reg["disqualified"]: return api_error(403,"Disqualified contestants/teams cannot submit")
        data=request.get_json(silent=True) or {}
        pid=data.get("problem_id"); lid=data.get("language_id"); files=data.get("files") or []
        p=conn.execute("SELECT * FROM problems WHERE contest_id=? AND api_id=?",(cid,pid)).fetchone()
        if not p or lid not in LANGUAGES: return api_error(400,"Invalid problem_id or language_id")
        if len(files)!=1 or "data" not in files[0]: return api_error(400,"files must contain exactly one base64 ZIP in data")
        try:
            raw=base64.b64decode(files[0]["data"],validate=True)
            with zipfile.ZipFile(io.BytesIO(raw)) as zf:
                names=[n for n in zf.namelist() if not n.endswith("/")]
                if len(names)!=1 or len(Path(names[0]).parts)!=1: raise ValueError
                source=zf.read(names[0]).decode("utf-8")
        except Exception:
            return api_error(400,"Invalid submission ZIP")
        now=iso(utcnow()); entry=data.get("entry_point")
        cur=conn.execute("INSERT INTO submissions(contest_id,account_id,problem_id,language,source,entry_point,submitted_at,status) VALUES(?,?,?,?,?,?,?,'PENDING')",(cid,a["id"],p["id"],lid,source,entry,now))
        s=conn.execute("SELECT * FROM submissions WHERE id=?",(cur.lastrowid,)).fetchone(); obj=api_submission(s,c,conn)
        append_event(conn,cid,"submissions",str(s["id"]),obj); conn.commit()
        resp=make_response(jsonify(obj),201); resp.headers["Location"]=url_for("api_submission_one",cid=cid,sid=s["id"],_external=True); return resp
    query="SELECT * FROM submissions WHERE contest_id=?"; params=[cid]
    if not a or a["role"]=="participant":
        if not a: return api_error(401,"Authentication required for submissions")
        query += " AND account_id=?"; params.append(a["id"])
    if request.args.get("problem_id"):
        p=conn.execute("SELECT id FROM problems WHERE contest_id=? AND api_id=?",(cid,request.args["problem_id"])).fetchone()
        if not p:return jsonify([])
        query += " AND problem_id=?"; params.append(p["id"])
    if request.args.get("language_id"):
        query += " AND language=?"; params.append(request.args["language_id"])
    query += " ORDER BY id"
    return jsonify([api_submission(s,c,conn) for s in conn.execute(query,params)])


@app.get("/api/contests/<cid>/submissions/<int:sid>")
def api_submission_one(cid,sid):
    conn=get_db(); c=contest_by_id(cid,conn); a=api_auth_account(conn)
    if not c:return api_error(404,"Contest not found")
    s=conn.execute("SELECT * FROM submissions WHERE contest_id=? AND id=?",(cid,sid)).fetchone()
    if not s:return api_error(404,"Submission not found")
    if not a or (a["role"]=="participant" and a["id"]!=s["account_id"]):return api_error(403,"Not permitted")
    return jsonify(api_submission(s,c,conn))


@app.get("/api/contests/<cid>/submissions/<int:sid>/files")
def api_submission_files(cid,sid):
    conn=get_db(); a=api_auth_account(conn); s=conn.execute("SELECT * FROM submissions WHERE contest_id=? AND id=?",(cid,sid)).fetchone()
    if not s:return api_error(404,"Submission not found")
    if not a or (a["role"]=="participant" and a["id"]!=s["account_id"]):return api_error(403,"Not permitted")
    ext=LANGUAGES[s["language"]].extension
    filename={"java":"Main.java","kotlin":"Main.kt","python3":"main.py","ruby":"main.rb"}.get(s["language"], f"main{ext}")
    buf=io.BytesIO()
    with zipfile.ZipFile(buf,"w",zipfile.ZIP_DEFLATED) as zf: zf.writestr(filename,s["source"])
    buf.seek(0)
    return send_file(buf,mimetype="application/zip",as_attachment=True,download_name="files.zip")


@app.get("/api/contests/<cid>/judgements")
def api_judgements(cid):
    conn=get_db(); c=contest_by_id(cid,conn); a=api_auth_account(conn)
    if not c:return api_error(404,"Contest not found")
    if not a:return api_error(401,"Authentication required for judgements")
    q="SELECT * FROM submissions WHERE contest_id=? AND judging_started_at IS NOT NULL"; params=[cid]
    if a["role"]=="participant": q += " AND account_id=?"; params.append(a["id"])
    vals=[api_judgement(s) for s in conn.execute(q+" ORDER BY id",params)]; vals=[v for v in vals if v]
    return jsonify(vals)


@app.get("/api/contests/<cid>/judgements/<jid>")
def api_judgement_one(cid,jid):
    conn=get_db(); c=contest_by_id(cid,conn); a=api_auth_account(conn)
    if not c:return api_error(404,"Contest not found")
    if not a:return api_error(401,"Authentication required")
    m=re.fullmatch(r"j-(\d+)",jid)
    if not m:return api_error(404,"Judgement not found")
    sub=conn.execute("SELECT * FROM submissions WHERE contest_id=? AND id=? AND judging_started_at IS NOT NULL",(cid,int(m.group(1)))).fetchone()
    if not sub:return api_error(404,"Judgement not found")
    if a["role"]=="participant" and a["id"]!=sub["account_id"]:return api_error(403,"Not permitted")
    return jsonify(api_judgement(sub))


@app.route("/api/contests/<cid>/clarifications", methods=["GET","POST"])
def api_clarifications(cid):
    conn=get_db(); c=contest_by_id(cid,conn); a=api_auth_account(conn)
    if not c:return api_error(404,"Contest not found")
    if not a:return api_error(401,"Authentication required for clarifications")
    if request.method=="POST":
        if a["role"]!="participant" or not participant_registered(cid,a["id"],conn):
            return api_error(403,"post_clar capability required")
        data=request.get_json(silent=True) or {}
        text=(data.get("text") or "").strip(); pid=data.get("problem_id")
        if not text:return api_error(400,"text is required")
        problem=None
        if pid:
            problem=conn.execute("SELECT * FROM problems WHERE contest_id=? AND api_id=?",(cid,pid)).fetchone()
            if not problem:return api_error(400,"Invalid problem_id")
        cur=conn.execute("INSERT INTO clarifications(contest_id,account_id,problem_id,question,created_at) VALUES(?,?,?,?,?)",(cid,a["id"],problem["id"] if problem else None,text,iso(utcnow())))
        row=conn.execute("SELECT * FROM clarifications WHERE id=?",(cur.lastrowid,)).fetchone(); obj=api_clarification_question(row,c,conn)
        append_event(conn,cid,"clarifications",obj["id"],obj); conn.commit()
        resp=make_response(jsonify(obj),201); resp.headers["Location"]=url_for("api_clarification_one",cid=cid,clid=obj["id"],_external=True); return resp
    if a["role"] == "participant":
        own = conn.execute("SELECT * FROM clarifications WHERE contest_id=? AND account_id=? ORDER BY id", (cid, a["id"])).fetchall()
        broadcasts = conn.execute("SELECT * FROM clarifications WHERE contest_id=? AND broadcast=1 AND answer IS NOT NULL AND account_id<>? ORDER BY id", (cid, a["id"])).fetchall()
        out=[]
        for row in own:
            out.append(api_clarification_question(row,c,conn))
            ans=api_clarification_answer(row,c,conn)
            if ans: out.append(ans)
        for row in broadcasts:
            ans=api_clarification_answer(row,c,conn)
            if ans: out.append(ans)
        return jsonify(out)
    rows=conn.execute("SELECT * FROM clarifications WHERE contest_id=? ORDER BY id", (cid,)).fetchall(); out=[]
    for row in rows:
        out.append(api_clarification_question(row,c,conn))
        ans=api_clarification_answer(row,c,conn)
        if ans: out.append(ans)
    return jsonify(out)

@app.get("/api/contests/<cid>/clarifications/<clid>")
def api_clarification_one(cid,clid):
    conn=get_db(); c=contest_by_id(cid,conn); a=api_auth_account(conn)
    if not c:return api_error(404,"Contest not found")
    if not a:return api_error(401,"Authentication required")
    m=re.fullmatch(r"([qa])-(\d+)",clid)
    if not m:return api_error(404,"Clarification not found")
    row=conn.execute("SELECT * FROM clarifications WHERE contest_id=? AND id=?",(cid,int(m.group(2)))).fetchone()
    if not row:return api_error(404,"Clarification not found")
    if a["role"]=="participant" and a["id"]!=row["account_id"]:
        if not (m.group(1)=="a" and row["broadcast"]): return api_error(403,"Not permitted")
    obj=api_clarification_question(row,c,conn) if m.group(1)=="q" else api_clarification_answer(row,c,conn)
    if not obj:return api_error(404,"Clarification not found")
    return jsonify(obj)


def _api_award_objects(cid, conn):
    awards=conn.execute("SELECT * FROM awards WHERE contest_id=? AND active=1 ORDER BY ordinal,id",(cid,)).fetchall(); out=[]
    contest=contest_by_id(cid,conn); st_rows,_=standings(contest); entity_to_team={r["participant"]["id"]:r["participant"]["team_api_id"] for r in st_rows}
    for a in awards:
        recips=conn.execute("SELECT * FROM award_recipients WHERE award_id=? ORDER BY entity_name",(a["id"],)).fetchall(); tids=[entity_to_team[r["entity_id"]] for r in recips if r["entity_id"] in entity_to_team]
        obj={"id":f"award-{a['id']}","citation":a["name"],"team_ids":tids}
        out.append(obj)
    return out


@app.get("/api/contests/<cid>/awards")
def api_awards(cid):
    conn=get_db(); c=contest_by_id(cid,conn)
    if not c: return api_error(404,"Contest not found")
    return jsonify(_api_award_objects(cid,conn))


@app.get("/api/contests/<cid>/awards/<aid>")
def api_award_one(cid,aid):
    conn=get_db(); c=contest_by_id(cid,conn)
    if not c: return api_error(404,"Contest not found")
    obj=next((x for x in _api_award_objects(cid,conn) if x["id"]==aid),None)
    if not obj: return api_error(404,"Award not found")
    return jsonify(obj)


@app.get("/api/contests/<cid>/scoreboard")
def api_scoreboard(cid):
    c=contest_by_id(cid)
    if not c:return api_error(404,"Contest not found")
    st=contest_state(c); frozen=st["phase"] in {"FROZEN","ENDED"} and not st["revealed"]
    rows, probs=standings(c, st["freeze_at"] if frozen else None)
    start, end, _=contest_times(c); now=utcnow(); contest_seconds=(min(now,end)-start).total_seconds() if start and end else 0
    out=[]
    for r in rows:
        if r.get("disqualified"):
            continue
        pdata=[]
        for p in probs:
            cell=r["cells"][p["api_id"]]
            item={"problem_id":p["api_id"],"num_judged":cell["judged"],"num_pending":cell["pending"],"solved":cell["status"]=="AC"}
            if cell["status"]=="AC": item["time"]=reltime(cell["minute"]*60)
            pdata.append(item)
        score={"num_solved":r["solved"],"total_time":reltime(r["penalty"]*60),"time":reltime(r["last_solve"]*60) if r["last_solve"] is not None else None}
        out.append({"rank":r["rank"],"team_id":r["participant"]["team_api_id"],"score":score,"problems":pdata})
    return jsonify({"time":iso(now),"contest_time":reltime(contest_seconds),"state":api_state(c),"rows":out})


@app.get("/api/contests/<cid>/event-feed")
def api_event_feed(cid):
    conn=get_db(); c=contest_by_id(cid,conn); a=api_auth_account(conn)
    if not c:return api_error(404,"Contest not found")
    if not a or a["role"] not in {"judge","admin"}:return api_error(403,"Event feed is restricted in this local build")
    after=request.args.get("since_token", request.args.get("token","0"))
    try: after_n=int(after)
    except ValueError: after_n=0
    events=conn.execute("SELECT * FROM api_events WHERE contest_id=? AND seq>? ORDER BY seq LIMIT 1000",(cid,after_n)).fetchall()
    lines=[]
    for e in events:
        lines.append(json.dumps({"type":e["type"],"id":e["object_id"],"data":json.loads(e["data_json"]) if e["data_json"] is not None else None,"token":str(e["seq"])},separators=(",",":")))
    resp=make_response("\n".join(lines)+("\n" if lines else "")); resp.mimetype="application/x-ndjson"; return resp


app.register_blueprint(fx.bp)

# Vercel imports the Flask application instead of executing app.py as
# __main__. Initialize an idempotent demo database during a cold start so the
# presentation site has working logins, sample problems, leaderboards and
# administration screens. Nothing in /tmp is guaranteed to persist.
if VERCEL_DEMO:
    os.environ.setdefault("INQUISITOR_DEMO_ACCOUNTS", "1")
    os.environ.setdefault("INQUISITOR_JUDGE_BACKEND", "disabled")
    init_db()

# ---------------- Development judge worker ----------------
def judging_worker():
    while True:
        conn = None
        try:
            conn = db_connect()
            sub = conn.execute(
                """SELECT s.*, p.api_id, p.time_limit_ms, p.memory_limit_mb FROM submissions s JOIN problems p ON p.id=s.problem_id
                   WHERE s.status='PENDING' ORDER BY s.id LIMIT 1"""
            ).fetchone()
            if not sub:
                time.sleep(0.5); continue
            started=iso(utcnow())
            claimed=conn.execute("UPDATE submissions SET status='JUDGING',judging_started_at=?,detail='Running on Inquisitor judge worker.' WHERE id=? AND status='PENDING'",(started,sub["id"]))
            conn.commit()
            if claimed.rowcount != 1: continue
            fresh=conn.execute("SELECT * FROM submissions WHERE id=?",(sub["id"],)).fetchone()
            if not fresh or fresh["status"]!="JUDGING": continue
            result=judge_submission(language=sub["language"],source=sub["source"],tests_dir=PROBLEMS_DIR/sub["contest_id"]/sub["api_id"]/"tests",time_limit_ms=sub["time_limit_ms"],memory_limit_mb=sub["memory_limit_mb"])
            judged=iso(utcnow())
            # Only write the result if the submission is still ours: a rejudge request or a judge's manual
            # verdict that landed while this ran must not be overwritten by a stale auto result.
            wrote=conn.execute("UPDATE submissions SET status='DONE',verdict=?,runtime_ms=?,detail=?,judged_at=? WHERE id=? AND status='JUDGING' AND manual_override=0",(result["verdict"],result["runtime_ms"],result["detail"],judged,sub["id"]))
            if wrote.rowcount!=1:
                conn.commit(); continue
            contest=contest_by_id(sub["contest_id"],conn); done=conn.execute("SELECT * FROM submissions WHERE id=?",(sub["id"],)).fetchone()
            append_event(conn,sub["contest_id"],"judgements",f"j-{sub['id']}",api_judgement(done)); problem=conn.execute("SELECT * FROM problems WHERE id=?",(sub["problem_id"],)).fetchone(); fx.on_judgement(conn,done,problem); conn.commit()
        except Exception as exc:
            if conn: conn.rollback()
            print("Judge worker error:",repr(exc),flush=True); time.sleep(1)
        finally:
            if conn: conn.close()


def judge_worker_count() -> int:
    """Number of parallel auto-judge threads (INQUISITOR_JUDGE_WORKERS). Each runs one Docker judge container at a time."""
    default = 3 if judge_backend() == "docker" else 1
    try:
        return max(1, min(16, int(os.environ.get("INQUISITOR_JUDGE_WORKERS", default))))
    except ValueError:
        return default


def start_worker():
    # A crash mid-judging leaves rows stuck in JUDGING; put them back in the queue.
    conn = db_connect()
    try:
        conn.execute("UPDATE submissions SET status='PENDING' WHERE status='JUDGING'"); conn.commit()
    finally:
        conn.close()
    # Submissions are claimed with an atomic UPDATE ... WHERE status='PENDING', so threads never judge the same one twice.
    for n in range(judge_worker_count()):
        threading.Thread(target=judging_worker,name=f"inquisitor-judge-{n+1}",daemon=True).start()


if __name__ == "__main__":
    init_db(); start_worker(); fx.start_maintenance_worker()
    print("\nInquisitor v1.0.8 — Galahad")
    print("Contestant: contestant / inquisitor")
    print("Judge:       judge / inquisitor")
    print("Admin:       admin / inquisitor")
    print("Open:        http://127.0.0.1:5000")
    print("LAN:         http://<server-ip>:5000\n")
    app.run(host="0.0.0.0",port=int(os.environ.get("PORT","5000")),debug=False,threaded=True)
