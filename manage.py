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
                    (username, email, also, fx.unusable_password_hash(), username, "TACTICS x ACPC", app.iso(app.utcnow())))
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
                                   (username, email, also, fx.unusable_password_hash(), username, "TACTICS x ACPC", app.iso(app.utcnow())))
                aid = cur.lastrowid
            conn.execute("DELETE FROM staff_assignments WHERE contest_id=? AND account_id=?", (contest["id"], aid))
            conn.execute("INSERT INTO staff_assignments(contest_id,account_id,staff_role,created_at) VALUES(?,?,?,?)", (contest["id"], aid, role, app.iso(app.utcnow())))
            conn.commit()
            print(f"{username} <{email}> is now {fx.STAFF_ROLES[role]} for {contest['name']}.")
        finally:
            conn.close()
    print("They sign in with Google on the normal sign-in page. Anyone not on the staff list is a contestant.")


def stats_command():
    """One-screen summary for the contest day: phase, registrations, judging queue."""
    with app.app.app_context():
        conn = app.db_connect()
        try:
            c = app.active_contest(conn)
            if not c:
                print("No active contest."); return
            st = app.contest_state(c); left = int(st["remaining"]); cid = c["id"]
            print(f"Contest: {c['name']}   phase: {st['phase']}   time left: {left // 3600:d}:{left % 3600 // 60:02d}:{left % 60:02d}")
            reg = dict(conn.execute("SELECT approval_status, COUNT(*) FROM registrations WHERE contest_id=? GROUP BY approval_status", (cid,)).fetchall())
            google = conn.execute("SELECT COUNT(*) FROM registrations r JOIN accounts a ON a.id=r.account_id WHERE r.contest_id=? AND a.google_sub IS NOT NULL", (cid,)).fetchone()[0]
            print(f"Contestants registered: {sum(reg.values())} (approved {reg.get('APPROVED', 0)}, waiting for approval {reg.get('PENDING', 0)}); signed up with Google: {google}")
            by = dict(conn.execute("SELECT status, COUNT(*) FROM submissions WHERE contest_id=? GROUP BY status", (cid,)).fetchall())
            minute_ago = app.iso(app.utcnow() - app.timedelta(seconds=60))
            last_min = conn.execute("SELECT COUNT(*) FROM submissions WHERE contest_id=? AND submitted_at>=?", (cid, minute_ago)).fetchone()[0]
            oldest = conn.execute("SELECT MIN(submitted_at) FROM submissions WHERE contest_id=? AND status='PENDING'", (cid,)).fetchone()[0]
            age = f"{int((app.utcnow() - app.parse_dt(oldest)).total_seconds())}s" if oldest else "none waiting"
            print(f"Submissions: {sum(by.values())} total | {by.get('PENDING', 0)} waiting | {by.get('JUDGING', 0)} being judged | {by.get('DONE', 0)} done | oldest waiting: {age} | last minute: {last_min}")
            verdicts = conn.execute("SELECT verdict, COUNT(*) FROM submissions WHERE contest_id=? AND verdict IS NOT NULL GROUP BY verdict ORDER BY 2 DESC", (cid,)).fetchall()
            print("Verdicts: " + (", ".join(f"{v} {n}" for v, n in verdicts) or "none yet"))
        finally:
            conn.close()


def _parse_local(text: str):
    """'2026-10-20 16:30' (Philippine time) -> datetime."""
    from datetime import datetime
    try:
        return datetime.fromisoformat(text.strip().replace(" ", "T"))
    except ValueError:
        raise SystemExit(f"Could not read the time '{text}'. Write it like 2026-10-20 16:30 (Philippine time).")


def new_contest_command(args):
    """Create a ready-to-run contest by copying another one's problems (and tests, pictures): scheduled start, registration window, judges."""
    import re, shutil
    from datetime import timedelta
    if not args.start:
        raise SystemExit('Give the start time in Philippine time: new-contest --start "2026-10-20 16:30"')
    start_local = _parse_local(args.start)
    reg_close_local = _parse_local(args.reg_close) if args.reg_close else start_local + timedelta(minutes=15)
    duration = max(1, min(2880, int(args.duration or 40)))
    freeze = max(0, min(duration, int(args.freeze or 0)))
    judges = [e.lower() for e in (args.judge or [])]
    domains = (app.google_login_config() or {}).get("domains") or ["adnu.edu.ph", "gbox.adnu.edu.ph"]
    bad = [e for e in judges if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", e) or e.rpartition("@")[2] not in domains]
    if bad:
        raise SystemExit("Judges must have ADNU emails (" + ", ".join("@" + d for d in domains) + "): " + ", ".join(bad))
    with app.app.app_context():
        app.init_db()
        conn = app.db_connect()
        try:
            src = app.contest_by_id(args.clone_from, conn) if args.clone_from else app.active_contest(conn)
            if not src:
                raise SystemExit("There is no contest to copy problems from. Use --clone-from CONTEST_ID.")
            cid = args.id or "loadtest-" + start_local.strftime("%Y%m%d")
            if not app.ID_RE.fullmatch(cid):
                raise SystemExit("The contest id may only use letters, numbers, _, - and . (up to 36 characters).")
            if app.contest_by_id(cid, conn):
                raise SystemExit(f"A contest with the id '{cid}' already exists. Choose another with --id.")
            name = args.name or "Load Test"
            now = app.iso(app.utcnow())
            cols = [r[1] for r in conn.execute("PRAGMA table_info(contests)") if r[1] != "id"]
            over = {"name": name, "formal_name": name, "start_at": app.local_input_to_utc(start_local.isoformat(timespec="minutes")),
                    "duration_minutes": duration, "freeze_minutes": freeze, "lifecycle_phase": "scheduled", "registration_start": now,
                    "registration_end": app.local_input_to_utc(reg_close_local.isoformat(timespec="minutes")), "signup_open": 1, "requires_approval": 0,
                    "require_checkin": 0, "submissions_open": 1, "practice_mode": 0, "active": 1, "created_at": now, "archived": 0, "finalized_at": None,
                    "paused_at": None, "pause_accumulated_seconds": 0, "scoreboard_revealed": 0, "scoreboard_thawed_at": None, "allowed_emails": "",
                    "open_entry": 0, "cloned_from": src["id"], "emergency_message": "", "projector_autoscroll": 0}
            values = [over[c] if c in over else src[c] for c in cols]
            conn.execute("UPDATE contests SET active=0")
            conn.execute(f"INSERT INTO contests(id,{','.join(cols)}) VALUES(?,{','.join('?' for _ in cols)})", [cid, *values])
            pcols = [r[1] for r in conn.execute("PRAGMA table_info(problems)") if r[1] not in {"id", "contest_id"}]
            problems = conn.execute("SELECT * FROM problems WHERE contest_id=? ORDER BY ordinal,code", (src["id"],)).fetchall()
            for p in problems:
                conn.execute(f"INSERT INTO problems(contest_id,{','.join(pcols)}) VALUES(?,{','.join('?' for _ in pcols)})", [cid, *[p[c] for c in pcols]])
                src_dir, dst_dir = app.PROBLEMS_DIR / src["id"] / p["api_id"], app.PROBLEMS_DIR / cid / p["api_id"]
                if src_dir.is_dir():
                    shutil.copytree(src_dir, dst_dir, dirs_exist_ok=True)
            added = 0
            for email in judges:
                if fx.whitelist_staff(conn, app, cid, email, "judge") != "director":
                    added += 1
            contest = app.contest_by_id(cid, conn)
            app.append_event(conn, cid, "contest", None, app.api_contest(contest))
            fx.audit(conn, "contest.create", contest_id=cid, object_type="contest", object_id=cid, detail=f"{name} (created with new-contest, copied from {src['id']})")
            conn.commit()
            fmt = lambda d: d.strftime("%a %d %b %Y, %I:%M %p")
            print(f"Created contest '{name}' (id {cid}) and made it the active contest.")
            print(f"  Starts:       {fmt(start_local)} Philippine time, lasts {duration} minutes, " + (f"scoreboard freezes for the last {freeze} minutes" if freeze else "no scoreboard freeze"))
            print(f"  Registration: open now, closes {fmt(reg_close_local)}")
            print(f"  Problems:     {len(problems)} copied from '{src['name']}' with their hidden tests and pictures: " + ", ".join(f"{p['code']} {p['title']}" for p in problems))
            print(f"  Judges:       {added} added" + (" (they sign in with Google)" if added else ""))
            print("It starts by itself at the start time. Peter can still edit the contest, add or replace problems, and add more judges in the director pages.")
            print("Next: run inq preflight to check that everything is ready.")
        finally:
            conn.close()


def preflight_command(args):
    """Is this server ready for a contest? Plain PASS / WARN / FAIL lines with what to do."""
    import json, shutil, tempfile, time, urllib.request, urllib.error
    from email.utils import parsedate_to_datetime
    counts = {"PASS": 0, "WARN": 0, "FAIL": 0}
    def line(level, text, fix=""):
        counts[level] += 1
        print(f"  {level:<4}  {text}" + (f"\n          -> {fix}" if fix else ""), flush=True)
    def section(title):
        print(f"\n{title}", flush=True)
    def fetch(url, method="GET", timeout=10):
        req = urllib.request.Request(url, method=method, headers={"User-Agent": "inquisitor-preflight"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.status, r.headers
        except urllib.error.HTTPError as e:
            return e.code, e.headers
    with app.app.app_context():
        app.init_db()
        conn = app.db_connect()
        try:
            print("Inquisitor pre-flight check   " + time.strftime("%Y-%m-%d %H:%M:%S"))
            section("This server")
            data_dir = str(app.DATA_DIR); free_gb = shutil.disk_usage(data_dir).free / 1e9
            line("PASS" if free_gb >= 10 else ("WARN" if free_gb >= 3 else "FAIL"), f"disk space free: {free_gb:.0f} GB", "" if free_gb >= 10 else "free some disk space before the contest")
            mem = {k: int(v.split()[0]) / 1024 for k, v in (l.split(":", 1) for l in open("/proc/meminfo"))}
            avail = mem.get("MemAvailable", 0)
            line("PASS" if avail >= 2000 else ("WARN" if avail >= 1200 else "FAIL"), f"memory available: {avail:.0f} MB of {mem.get('MemTotal', 0):.0f} MB", "" if avail >= 2000 else "something else is using memory; check inq watch")
            load = os.getloadavg()[0]; cores = os.cpu_count() or 1
            line("PASS" if load < cores * 0.6 else "WARN", f"load average {load:.2f} on {cores} CPU cores", "" if load < cores * 0.6 else "the server is busy right now")
            try:
                status, headers = fetch("https://www.google.com", "HEAD")
                skew = abs((parsedate_to_datetime(headers["Date"]) - app.utcnow()).total_seconds())
                line("PASS" if skew <= 5 else ("WARN" if skew <= 60 else "FAIL"), f"server clock is within {skew:.0f} s of real time", "" if skew <= 5 else "fix the server's clock; contest start and end times depend on it")
            except Exception as exc:
                line("WARN", f"could not check the clock against the internet ({exc.__class__.__name__})")
            section("Judge")
            backend = os.environ.get("INQUISITOR_JUDGE_BACKEND", "local")
            line("PASS" if backend == "docker" else "FAIL", f"judge backend: {backend}", "" if backend == "docker" else "student code must run in the Docker judge")
            line("PASS", f"parallel judge threads: {app.judge_worker_count()}   compile memory limit: {os.environ.get('INQUISITOR_JUDGE_COMPILE_MEMORY_MB', '1536')} MB")
            samples = {"python3": "print(int(input())+1)\n", "cpp": "#include <iostream>\nint main(){long long x;std::cin>>x;std::cout<<x+1<<std::endl;}\n",
                       "java": "public class Main{public static void main(String[] a){java.util.Scanner s=new java.util.Scanner(System.in);System.out.println(s.nextLong()+1);}}\n"}
            with tempfile.TemporaryDirectory() as tmp:
                tests = Path(tmp); (tests / "input01.txt").write_text("41\n"); (tests / "output01.txt").write_text("42\n")
                for lang, src in samples.items():
                    t0 = time.time(); r = app.judge_submission(language=lang, source=src, tests_dir=tests, time_limit_ms=2000)
                    line("PASS" if r["verdict"] == "AC" else "FAIL", f"a test {lang} program was judged {r['verdict']} in {time.time() - t0:.1f} s", "" if r["verdict"] == "AC" else (r["detail"] or "")[:160])
            section("Network")
            for label, url, ok_codes in (("Google sign-in service", "https://accounts.google.com/.well-known/openid-configuration", (200,)), ("Google token service", "https://oauth2.googleapis.com/token", (200, 400, 404, 405))):
                try:
                    status, _ = fetch(url)
                    line("PASS" if status in ok_codes else "FAIL", f"{label} reachable from this server (HTTP {status})", "" if status in ok_codes else "students could not finish signing in")
                except Exception as exc:
                    line("FAIL", f"{label} not reachable: {exc.__class__.__name__}", "the server needs outbound internet access to Google")
            public = os.environ.get("INQUISITOR_PUBLIC_URL", "").rstrip("/")
            if public:
                try:
                    status, _ = fetch(public + "/healthz")
                    line("PASS" if status == 200 else "WARN", f"public address {public} answers (HTTP {status})", "" if status == 200 else "check the Cloudflare tunnel")
                except Exception as exc:
                    line("WARN", f"public address {public} did not answer from here ({exc.__class__.__name__})", "check the Cloudflare tunnel and open the address from a phone on mobile data")
            else:
                line("WARN", "INQUISITOR_PUBLIC_URL is not set", "run inq google-setup")
            section("Sign-in")
            cfg = app.google_login_config()
            line("PASS" if cfg else "FAIL", "Google sign-in is configured" if cfg else "Google sign-in is not configured", "" if cfg else "run inq google-setup")
            if cfg:
                line("PASS", "allowed email domains: " + ", ".join("@" + d for d in cfg["domains"]))
            line("PASS" if public.startswith("https://") else "WARN", f"site address: {public or 'not set'}", "" if public.startswith("https://") else "students should use an https address")
            line("PASS" if os.environ.get("INQUISITOR_SECURE_COOKIES") == "1" else "WARN", "sign-in cookies are https-only" if os.environ.get("INQUISITOR_SECURE_COOKIES") == "1" else "sign-in cookies are not https-only (optional hardening)", "" if os.environ.get("INQUISITOR_SECURE_COOKIES") == "1" else "inq https-on")
            admins = conn.execute("SELECT COUNT(*) FROM accounts WHERE role='admin'").fetchone()[0]
            line("PASS" if admins >= 2 else "WARN", f"directors: {admins}", "" if admins >= 2 else "add a second director with inq add-staff EMAIL director")
            section("Contest")
            c = app.active_contest(conn)
            if not c:
                line("FAIL", "there is no active contest", "create one in the director pages, or run inq new-contest")
            else:
                st = app.contest_state(c); closed = app.registration_is_closed(c)
                line("PASS", f"active contest: {c['name']} (id {c['id']})")
                start_txt = app.localdt(c["start_at"]) if c["start_at"] else "no start time"
                line("PASS" if st["phase"] in ("UPCOMING", "RUNNING", "REGISTRATION") else "WARN", f"phase: {st['phase']}; start {start_txt} Philippine time; {c['duration_minutes']} minutes", "" if st["phase"] in ("UPCOMING", "RUNNING", "REGISTRATION") else "the contest is not in a phase where students can compete; edit its times or phase")
                line("PASS" if not closed else "WARN", "registration is open" + (f" until {app.localdt(c['registration_end'])}" if c["registration_end"] else "") if not closed else "registration is closed", "" if not closed else "open it in the contest settings if students still need to register")
                line("PASS", f"open entry: {'on' if c['open_entry'] else 'off'}; approval needed: {'yes' if c['requires_approval'] else 'no'}; contestant list: {len([x for x in c['allowed_emails'].split() if x]) or 'none (any ADNU account)'}")
                staff = conn.execute("SELECT s.staff_role, COUNT(*) n FROM staff_assignments s WHERE s.contest_id=? GROUP BY s.staff_role", (c["id"],)).fetchall()
                judges = sum(r["n"] for r in staff if r["staff_role"] in ("judge", "chief_judge"))
                line("PASS" if judges else "WARN", f"judges assigned: {judges}", "" if judges else "add judges in Staff & Roles or the contest form")
                problems = conn.execute("SELECT * FROM problems WHERE contest_id=? ORDER BY ordinal,code", (c["id"],)).fetchall()
                line("PASS" if problems else "FAIL", f"published problems: {len(problems)}", "" if problems else "add problems before the contest")
                for p in problems:
                    t0 = time.time(); rep = fx.validate_problem(p, conn); took = time.time() - t0
                    failed = [x for x in rep["checks"] if not x["ok"]]
                    hard = [x for x in failed if x["name"] != "Reference solution"]
                    level = "PASS" if rep["ok"] else ("FAIL" if hard else "WARN")
                    shown = (hard or failed)[0] if failed else None
                    line(level, f"problem {p['code']} {p['title']}: " + ("all checks green" if rep["ok"] else shown["name"] + ": " + shown["detail"][:110]) + f" ({took:.1f} s)",
                         "" if rep["ok"] else ("open the problem and press Validate" if hard else "optional: a reference solution lets Validate prove the hidden tests are correct"))
                regs = conn.execute("SELECT COUNT(*) FROM registrations WHERE contest_id=?", (c["id"],)).fetchone()[0]
                subs = conn.execute("SELECT COUNT(*) FROM submissions WHERE contest_id=?", (c["id"],)).fetchone()[0]
                line("PASS", f"already registered: {regs}; submissions so far: {subs}")
            section("Backups")
            backups = sorted((app.DATA_DIR / "backups").glob("*.db"), key=lambda f: f.stat().st_mtime) if (app.DATA_DIR / "backups").is_dir() else []
            if backups:
                age_h = (time.time() - backups[-1].stat().st_mtime) / 3600
                line("PASS" if age_h < 2 else "WARN", f"latest backup is {age_h:.1f} hours old ({len(backups)} kept)", "" if age_h < 2 else "run inq backup")
            else:
                line("WARN", "no backup yet", "run inq backup")
            print(f"\nResult: {counts['PASS']} passed, {counts['WARN']} warnings, {counts['FAIL']} failures.   " + ("NOT READY: fix the failures above." if counts["FAIL"] else ("READY (read the warnings)." if counts["WARN"] else "READY.")))
        finally:
            conn.close()


def main():
    parser = argparse.ArgumentParser(description="Inquisitor management utility")
    parser.add_argument("command", choices=["init", "backup", "check", "staff-path", "add-director", "add-staff", "stats", "new-contest", "preflight"])
    parser.add_argument("--email", help="add-director: the director's ADNU email")
    parser.add_argument("--also", help="add-director/add-staff: a second address that also signs in as this person")
    parser.add_argument("--id", help="new-contest: id for the new contest")
    parser.add_argument("--name", help="new-contest: title (default Load Test)")
    parser.add_argument("--start", help="new-contest: start time in Philippine time, e.g. \"2026-10-20 16:30\"")
    parser.add_argument("--duration", help="new-contest: minutes (default 40)")
    parser.add_argument("--freeze", help="new-contest: scoreboard freeze minutes (default 0)")
    parser.add_argument("--reg-close", dest="reg_close", help="new-contest: when registration closes (default 15 minutes after the start)")
    parser.add_argument("--clone-from", dest="clone_from", help="new-contest: contest to copy problems from (default: the active one)")
    parser.add_argument("--judge", action="append", help="new-contest: ADNU email of a judge (repeat for more)")
    parser.add_argument("--role", help="add-staff: director, chief_judge, judge, problem_setter, technical_admin, registration_staff or spectator")
    args = parser.parse_args()
    if args.command == "add-director":
        return add_director_command(args)
    if args.command == "add-staff":
        return add_staff_command(args)
    if args.command == "new-contest":
        return new_contest_command(args)
    if args.command == "preflight":
        return preflight_command(args)
    {"init": init_command, "backup": backup_command, "check": check_command, "staff-path": staff_path_command, "stats": stats_command}[args.command]()


if __name__ == "__main__":
    main()
