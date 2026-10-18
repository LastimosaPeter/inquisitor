from __future__ import annotations
import argparse
import os
import sys
from pathlib import Path

import app
import features_v1 as fx


def init_command():
    with app.app.app_context():
        app.init_db()
    print(f"Database ready: {app.DB_PATH}")


def backup_command():
    with app.app.app_context():
        app.init_db()
        conn = app.db_connect()
        try:
            contest = app.active_contest(conn)
            contest_id = contest["id"] if contest else None
        finally:
            conn.close()
        path = fx.create_backup("manual-cli", None, contest_id)
    print(path)


def check_command():
    with app.app.app_context():
        app.init_db()
        conn = app.db_connect()
        try:
            contest = app.active_contest(conn)
            pending = conn.execute("SELECT COUNT(*) FROM submissions WHERE status='PENDING'").fetchone()[0]
            print("Inquisitor check: OK")
            print("Data directory:", app.DATA_DIR)
            print("Active contest:", contest["name"] if contest else "none")
            print("Judge backend:", os.environ.get("INQUISITOR_JUDGE_BACKEND", "local"))
            print("Pending submissions:", pending)
            print("Languages:", ", ".join(x.label for x in app.LANGUAGES.values()))
        finally:
            conn.close()


def staff_path_command():
    print(app.staff_login_path())


def add_director_command(args):
    """Create (or promote) a director who signs in with Google. Usage: manage.py add-director --email a@adnu.edu.ph [--also a@gbox.adnu.edu.ph]"""
    import re, secrets
    from werkzeug.security import generate_password_hash
    email = (args.email or "").strip().lower()
    also = (args.also or "").strip().lower() or None
    pattern = r"[^@\s]+@[^@\s]+\.[^@\s]+"
    if not re.fullmatch(pattern, email) or (also and not re.fullmatch(pattern, also)):
        raise SystemExit("Give a valid email: add-director --email name@adnu.edu.ph [--also name@gbox.adnu.edu.ph]")
    with app.app.app_context():
        app.init_db()
        conn = app.db_connect()
        try:
            row = conn.execute("SELECT id,role,username FROM accounts WHERE lower(email) IN (?,?) OR lower(alt_email) IN (?,?) ORDER BY id LIMIT 1", (email, also or email, email, also or email)).fetchone()
            if row and row["role"] == "admin":
                print(f"{email} is already a director (username {row['username']}).")
                return
            if row:
                conn.execute("UPDATE accounts SET role='admin', email=COALESCE(NULLIF(email,''),?), alt_email=COALESCE(alt_email,?) WHERE id=?", (email, also, row["id"]))
                print(f"Promoted existing account {row['username']} to director.")
            else:
                username = app._unique_username(conn, email)
                conn.execute(
                    "INSERT INTO accounts(username,email,alt_email,password_hash,display_name,affiliation,role,created_at) VALUES(?,?,?,?,?,?,'admin',?)",
                    (username, email, also, generate_password_hash(secrets.token_hex(32)), username, "TACTICS x ACPC", app.iso(app.utcnow())))
                print(f"Created director {username} <{email}>" + (f" (also accepts {also})" if also else "") + ".")
            conn.commit()
        finally:
            conn.close()
    print("They can now sign in with Google at the site's sign-in page. Their name is filled in from Google the first time.")


def add_staff_command(args):
    """Whitelist someone by email: manage.py add-staff --email a@adnu.edu.ph --role judge|director|chief_judge|problem_setter|technical_admin|registration_staff|spectator [--also b]"""
    import re, secrets
    from werkzeug.security import generate_password_hash
    role = (args.role or "").strip().lower().replace("-", "_")
    if role in ("director", "admin"):
        return add_director_command(args)
    if role not in fx.STAFF_ROLES:
        raise SystemExit("Role must be one of: director, " + ", ".join(fx.STAFF_ROLES))
    email = (args.email or "").strip().lower(); also = (args.also or "").strip().lower() or None
    if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email) or (also and not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", also)):
        raise SystemExit("Give a valid email address.")
    with app.app.app_context():
        app.init_db()
        conn = app.db_connect()
        try:
            contest = app.active_contest(conn)
            if not contest:
                raise SystemExit("There is no active contest to assign staff to.")
            row = conn.execute("SELECT id,role,username FROM accounts WHERE lower(email) IN (?,?) OR lower(alt_email) IN (?,?) ORDER BY id LIMIT 1", (email, also or email, email, also or email)).fetchone()
            if row and row["role"] == "admin":
                print(f"{email} is already a director."); return
            if row:
                aid, username = row["id"], row["username"]
                conn.execute("UPDATE accounts SET role='judge', alt_email=COALESCE(alt_email,?) WHERE id=?", (also, aid))
            else:
                username = app._unique_username(conn, email)
                cur = conn.execute("INSERT INTO accounts(username,email,alt_email,password_hash,display_name,affiliation,role,created_at) VALUES(?,?,?,?,?,?,'judge',?)",
                                   (username, email, also, generate_password_hash(secrets.token_hex(32)), username, "TACTICS x ACPC", app.iso(app.utcnow())))
                aid = cur.lastrowid
            conn.execute("DELETE FROM staff_assignments WHERE contest_id=? AND account_id=?", (contest["id"], aid))
            conn.execute("INSERT INTO staff_assignments(contest_id,account_id,staff_role,created_at) VALUES(?,?,?,?)", (contest["id"], aid, role, app.iso(app.utcnow())))
            conn.commit()
            print(f"{username} <{email}> is now {fx.STAFF_ROLES[role]} for {contest['name']}.")
        finally:
            conn.close()
    print("They sign in with Google on the normal sign-in page. Anyone not on the staff list is a contestant.")


def main():
    parser = argparse.ArgumentParser(description="Inquisitor management utility")
    parser.add_argument("command", choices=["init", "backup", "check", "staff-path", "add-director", "add-staff"])
    parser.add_argument("--email", help="add-director: the director's ADNU email")
    parser.add_argument("--also", help="add-director/add-staff: a second address that also signs in as this person")
    parser.add_argument("--role", help="add-staff: director, chief_judge, judge, problem_setter, technical_admin, registration_staff or spectator")
    args = parser.parse_args()
    if args.command == "add-director":
        return add_director_command(args)
    if args.command == "add-staff":
        return add_staff_command(args)
    {"init": init_command, "backup": backup_command, "check": check_command, "staff-path": staff_path_command}[args.command]()


if __name__ == "__main__":
    main()
