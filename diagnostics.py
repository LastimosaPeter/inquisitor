"""What is the app doing right now? One place that gathers the answers, for the director page, `manage.py report` and `inq report`.

Everything here is read-only and safe to show to a director: counts, ages, verdict totals, recent server errors (path and message only,
never form data, passwords or query strings) and host load. The same report can be pasted into a chat to ask "what is going on?".
"""
from __future__ import annotations

import os
import shutil
import threading
import time
import traceback
from pathlib import Path

HEARTBEAT_EVERY = 10.0          # seconds between a judge thread's check-ins
STALE_AFTER = 45                # a judge thread that has not checked in for this long is considered stuck
_last_beat: dict[str, float] = {}
_beat_lock = threading.Lock()


def ensure_tables(conn) -> None:
    conn.execute("""CREATE TABLE IF NOT EXISTS app_errors (
        id INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT NOT NULL, method TEXT NOT NULL DEFAULT '', path TEXT NOT NULL DEFAULT '',
        username TEXT NOT NULL DEFAULT '', kind TEXT NOT NULL DEFAULT '', message TEXT NOT NULL DEFAULT '', trace TEXT NOT NULL DEFAULT '')""")
    conn.execute("""CREATE TABLE IF NOT EXISTS worker_status (
        name TEXT PRIMARY KEY, at TEXT NOT NULL, state TEXT NOT NULL DEFAULT '', judged INTEGER NOT NULL DEFAULT 0)""")


import re

_SECRET_WORDS = re.compile(r"""(?i)(api[_-]?key|passw(?:or)?d|passwd|token|secret|authorization|cookie|session|csrf|client[_-]?secret)(["']?\s*[=:]\s*["']?)[^\s"'&,;)]+""")
_QUERY = re.compile(r"\?[^\s\"']+")


def redact(text: str) -> str:
    """Hide anything that looks like a secret or a query string before it is stored or shown."""
    text = re.sub(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9._~+/=-]+", r"\1 [hidden]", str(text))
    return _QUERY.sub("?[hidden]", _SECRET_WORDS.sub(r"\1\2[hidden]", text))


def record_error(core, exc: BaseException, method: str = "", path: str = "", username: str = "") -> None:
    """Remember a server error. Never raises: a failure here must not hide the original problem."""
    try:
        conn = core.db_connect()
        try:
            ensure_tables(conn)
            trace = redact("".join(traceback.format_exception(type(exc), exc, exc.__traceback__))[-1800:])
            conn.execute("INSERT INTO app_errors(at,method,path,username,kind,message,trace) VALUES(?,?,?,?,?,?,?)",
                         (core.iso(core.utcnow()), method[:10], path.split("?")[0][:200], username[:60], type(exc).__name__, redact(str(exc))[:300], trace))
            conn.execute("DELETE FROM app_errors WHERE id NOT IN (SELECT id FROM app_errors ORDER BY id DESC LIMIT 200)")
            conn.commit()
        finally:
            conn.close()
    except Exception:
        pass


def heartbeat(core, conn, state: str, judged_inc: int = 0) -> None:
    """A judge thread says it is alive (at most every few seconds), and what it is doing."""
    name = threading.current_thread().name or "judge"
    now = time.time()
    with _beat_lock:
        if judged_inc == 0 and now - _last_beat.get(name, 0) < HEARTBEAT_EVERY:
            return
        _last_beat[name] = now
    try:
        conn.execute("""INSERT INTO worker_status(name,at,state,judged) VALUES(?,?,?,?)
                        ON CONFLICT(name) DO UPDATE SET at=excluded.at, state=excluded.state, judged=worker_status.judged+?""",
                     (name, core.iso(core.utcnow()), state[:80], judged_inc, judged_inc))
        conn.commit()
    except Exception:
        pass


def _build_info() -> str:
    for path in (Path(__file__).with_name("BUILD_INFO"), Path("/app/BUILD_INFO")):
        try:
            return path.read_text().strip()[:120]
        except OSError:
            continue
    return "unknown"


def _mem_available_mb() -> int | None:
    try:
        for line in open("/proc/meminfo"):
            if line.startswith("MemAvailable:"):
                return int(line.split()[1]) // 1024
    except OSError:
        pass
    return None


def _fmt_age(seconds: float | None) -> str:
    if seconds is None:
        return "never"
    seconds = int(seconds)
    if seconds < 90:
        return f"{seconds}s"
    if seconds < 5400:
        return f"{seconds // 60}m"
    return f"{seconds / 3600:.1f}h"


def collect(core, conn) -> dict:
    """The report as data: {'attention': [...], 'sections': [(title, [lines])], 'generated': str}."""
    ensure_tables(conn)
    now = core.utcnow()
    attention: list[str] = []
    sections: list[tuple[str, list[str]]] = []

    def ago(value):
        dt = core.parse_dt(value) if value else None
        return (now - dt).total_seconds() if dt else None

    # ---- the app
    lines = [f"Build: {_build_info()}   version {os.environ.get('INQUISITOR_VERSION', '1.0.8')}",
             f"Judge: backend {os.environ.get('INQUISITOR_JUDGE_BACKEND', 'local')}, {core.judge_worker_count()} thread(s), compile memory {os.environ.get('INQUISITOR_JUDGE_COMPILE_MEMORY_MB', '1536')} MB, max waiting per student {os.environ.get('INQUISITOR_MAX_WAITING_SUBMISSIONS', '3')}",
             f"Public address: {os.environ.get('INQUISITOR_PUBLIC_URL', 'not set')}   secure cookies: {os.environ.get('INQUISITOR_SECURE_COOKIES', '0')}   Google sign-in: {'on' if core.google_login_config() else 'off'}"]
    sections.append(("App", lines))

    # ---- the contest
    c = core.active_contest(conn)
    cid = c["id"] if c else None
    if c:
        st = core.contest_state(c)
        left = int(st["remaining"])
        start = core.parse_dt(c["start_at"]) if c["start_at"] else None
        lines = [f"{c['name']} (id {cid})",
                 f"Phase: {st['phase']}   lifecycle setting: {c['lifecycle_phase']}   time left: {left // 3600}:{left % 3600 // 60:02d}:{left % 60:02d}",
                 f"Start: {core.localdt(c['start_at'])} (Philippine time)   duration {c['duration_minutes']} min   freeze last {c['freeze_minutes']} min",
                 f"Registration: {'closed' if core.registration_is_closed(c) else 'open'}   open entry: {'on' if c['open_entry'] else 'off'}"]
        regs = conn.execute("SELECT COUNT(*) FROM registrations WHERE contest_id=?", (cid,)).fetchone()[0]
        lines.append(f"Registered contestants: {regs}")
        if c["lifecycle_phase"] in ("registration", "registration_closed") and start and now >= start and st["phase"] not in ("RUNNING", "FROZEN", "ENDED"):
            attention.append(f"The start time has passed but the phase is still {st['phase']}: set Lifecycle phase to Running or Scheduled (Contests, Edit configuration).")
        sections.append(("Contest", lines))
    else:
        attention.append("There is no active contest.")

    # ---- the judge queue
    if cid:
        counts = dict(conn.execute("SELECT status, COUNT(*) FROM submissions WHERE contest_id=? GROUP BY status", (cid,)).fetchall())
        oldest = conn.execute("SELECT MIN(submitted_at) FROM submissions WHERE contest_id=? AND status='PENDING'", (cid,)).fetchone()[0]
        oldest_age = ago(oldest)
        stuck = conn.execute("SELECT COUNT(*) FROM submissions WHERE contest_id=? AND status='JUDGING' AND judging_started_at<?",
                             (cid, core.iso(now - core.timedelta(minutes=5)))).fetchone()[0]
        lines = [f"Waiting {counts.get('PENDING', 0)} | being judged {counts.get('JUDGING', 0)} | done {counts.get('DONE', 0)} | oldest waiting: {_fmt_age(oldest_age) if oldest else 'none'}"]
        if oldest_age is not None and oldest_age > 60:
            attention.append(f"Submissions are backing up: the oldest has waited {_fmt_age(oldest_age)} (waiting: {counts.get('PENDING', 0)}). Consider more judge threads (inq workers 3).")
        if stuck:
            attention.append(f"{stuck} submission(s) have been in JUDGING for over 5 minutes; the judge may be stuck (inq logs worker).")
        recent = conn.execute("""SELECT submitted_at, judging_started_at, judged_at, verdict FROM submissions
                                 WHERE contest_id=? AND judged_at IS NOT NULL AND judged_at>=? ORDER BY id DESC LIMIT 400""",
                              (cid, core.iso(now - core.timedelta(minutes=15)))).fetchall()
        waits, runs = [], []
        for r in recent:
            s, a, b = (core.parse_dt(r[k]) if r[k] else None for k in ("submitted_at", "judging_started_at", "judged_at"))
            if s and a: waits.append((a - s).total_seconds())
            if a and b: runs.append((b - a).total_seconds())
        def stat(values):
            if not values: return "n/a"
            values = sorted(values); return f"avg {sum(values) / len(values):.1f}s, worst {values[-1]:.1f}s"
        lines.append(f"Last 15 min: {len(recent)} judged | queue wait {stat(waits)} | judging time {stat(runs)}")
        minute = conn.execute("SELECT COUNT(*) FROM submissions WHERE contest_id=? AND submitted_at>=?", (cid, core.iso(now - core.timedelta(seconds=60)))).fetchone()[0]
        active = conn.execute("SELECT COUNT(DISTINCT account_id) FROM submissions WHERE contest_id=? AND submitted_at>=?", (cid, core.iso(now - core.timedelta(minutes=15)))).fetchone()[0]
        lines.append(f"Submissions in the last minute: {minute} | contestants who submitted in the last 15 min: {active}")
        hour = conn.execute("SELECT verdict, COUNT(*) FROM submissions WHERE contest_id=? AND submitted_at>=? AND verdict IS NOT NULL GROUP BY verdict ORDER BY 2 DESC",
                            (cid, core.iso(now - core.timedelta(hours=1)))).fetchall()
        lines.append("Verdicts in the last hour: " + (", ".join(f"{v} {n}" for v, n in hour) or "none"))
        je = sum(n for v, n in hour if v == "JE")
        if je:
            attention.append(f"{je} judge error (JE) verdict(s) in the last hour: the judge itself failed on those submissions (inq logs worker).")
        sections.append(("Judge queue", lines))
        needs = conn.execute("SELECT COUNT(*) FROM submissions WHERE contest_id=? AND status='DONE' AND verdict IS NOT NULL AND verdict!='AC' AND feedback=''", (cid,)).fetchone()[0]
        sections.append(("Judge feedback", [f"Not-accepted submissions still without feedback: {needs}"]))

    # ---- judge threads
    rows = conn.execute("SELECT name, at, state, judged FROM worker_status ORDER BY name").fetchall()
    lines = []
    alive = 0
    for r in rows:
        age = ago(r["at"])
        ok = age is not None and age <= STALE_AFTER
        alive += 1 if ok else 0
        lines.append(f"{r['name']}: {'alive' if ok else 'NOT RESPONDING'} (last check-in {_fmt_age(age)} ago) {r['state']} | judged since start {r['judged']}")
    if not rows:
        lines.append("No judge thread has checked in yet.")
        attention.append("No judge thread has ever checked in: is the worker running? (inq status)")
    elif alive == 0:
        attention.append("Every judge thread has stopped checking in. Submissions will not be judged (inq restart).")
    elif alive < len(rows):
        attention.append(f"{len(rows) - alive} of {len(rows)} judge thread(s) are not responding.")
    sections.append(("Judge threads", lines))

    # ---- recent server errors
    errs = conn.execute("SELECT * FROM app_errors ORDER BY id DESC LIMIT 12").fetchall()
    recent_errs = [e for e in errs if (ago(e["at"]) or 1e9) < 900]
    lines = [f"{core.localdt(e['at'])} {e['method']} {e['path']} [{e['username'] or 'anonymous'}] {e['kind']}: {redact(e['message'])}" for e in errs] or ["No server errors recorded."]
    if recent_errs:
        attention.append(f"{len(recent_errs)} server error(s) in the last 15 minutes (see below).")
    sections.append(("Recent server errors (newest first, up to 12)", lines))

    # ---- the host
    lines = []
    try:
        disk = shutil.disk_usage(core.DATA_DIR)
        lines.append(f"Disk: {disk.free / 1e9:.0f} GB free of {disk.total / 1e9:.0f} GB")
        if disk.free < 3e9: attention.append(f"Only {disk.free / 1e9:.1f} GB of disk left.")
    except OSError:
        pass
    mem = _mem_available_mb()
    if mem is not None:
        lines.append(f"Memory available: {mem} MB")
        if mem < 800: attention.append(f"Only {mem} MB of memory available.")
    try:
        load = os.getloadavg(); lines.append(f"Load average: {load[0]:.2f} {load[1]:.2f} {load[2]:.2f} on {os.cpu_count()} cores")
    except OSError:
        pass
    try:
        db_size = Path(core.DB_PATH).stat().st_size / 1e6
        lines.append(f"Database: {db_size:.1f} MB")
    except OSError:
        pass
    backups = sorted((Path(core.DATA_DIR) / "backups").glob("*.db"), key=lambda f: f.stat().st_mtime) if (Path(core.DATA_DIR) / "backups").is_dir() else []
    if backups:
        age = time.time() - backups[-1].stat().st_mtime
        lines.append(f"Latest backup: {_fmt_age(age)} ago ({len(backups)} kept)")
        if age > 3 * 3600: attention.append(f"The latest backup is {_fmt_age(age)} old (inq backup).")
    else:
        lines.append("Latest backup: none")
        attention.append("There is no backup yet (inq backup).")
    sections.append(("Server", lines))

    return {"generated": f"{core.localdt(core.iso(now))} Philippine time", "attention": attention, "sections": sections}


def render_text(report: dict) -> str:
    out = [f"Inquisitor report, {report['generated']}", ""]
    out.append("NEEDS ATTENTION" if report["attention"] else "NEEDS ATTENTION: nothing")
    out += [f"  ! {a}" for a in report["attention"]]
    for title, lines in report["sections"]:
        out += ["", title.upper()] + [f"  {l}" for l in lines]
    return "\n".join(out) + "\n"
