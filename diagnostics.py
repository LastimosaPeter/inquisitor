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
    """Remember a server error. Never raises, and never touches the database from inside the failing request (the background thread writes it)."""
    try:
        import telemetry
        trace = redact("".join(traceback.format_exception(type(exc), exc, exc.__traceback__))[-1800:])
        telemetry.queue_error((core.iso(core.utcnow()), method[:10], path.split("?")[0][:200], username[:60], type(exc).__name__, redact(str(exc))[:300], trace))
    except Exception:
        pass


def heartbeat(core, conn, state: str, judged_inc: int = 0) -> None:
    """A judge thread says it is alive and what it is doing. Kept in memory and written by the background thread: the judge loop's own
    database connection is never used for this, so it cannot commit or hold anything it should not."""
    try:
        import telemetry
        telemetry.queue_heartbeat(core, threading.current_thread().name or "judge", state, judged_inc)
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


def collect(core, conn, deep: bool = False) -> dict:
    """The report as data: {'attention': [...], 'sections': [(title, [lines])], 'generated': str}."""
    try:
        import telemetry
        telemetry.flush(core)           # anything queued in this process becomes visible in the report
    except Exception:
        pass
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

    if deep:
        try:
            sections += deep_sections(core, conn, attention)
        except Exception as exc:     # the deep part must never take the basic report down with it
            sections.append(("Deep dive", [f"Could not be built: {type(exc).__name__}: {redact(str(exc))[:200]}"]))
    return {"generated": f"{core.localdt(core.iso(now))} Philippine time", "attention": attention, "sections": sections, "deep": deep}


def render_text(report: dict) -> str:
    out = [f"Inquisitor report, {report['generated']}", ""]
    out.append("NEEDS ATTENTION" if report["attention"] else "NEEDS ATTENTION: nothing")
    out += [f"  ! {a}" for a in report["attention"]]
    for title, lines in report["sections"]:
        out += ["", title.upper()] + [f"  {l}" for l in lines]
    return "\n".join(out) + "\n"


# ====================================================================== the deep dive
BARS = "▁▂▃▄▅▆▇█"


def spark(values) -> str:
    values = [v if v is not None else 0 for v in values]
    top = max(values) if values else 0
    if top <= 0:
        return "·" * len(values)
    return "".join("·" if v <= 0 else BARS[min(7, int(v / top * 7.999))] for v in values)


def _minute_keys(core, count: int, step: int = 1):
    """Minute keys (UTC 'YYYY-MM-DDTHH:MM'), oldest first."""
    base = core.utcnow().replace(second=0, microsecond=0)
    return [core.iso(base - core.timedelta(minutes=i * step))[:16] for i in range(count - 1, -1, -1)]


def deep_sections(core, conn, attention: list) -> list:
    out: list[tuple[str, list[str]]] = []
    now = core.utcnow()

    def parse(v):
        return core.parse_dt(v) if v else None

    # ---------------- requests
    minutes = _minute_keys(core, 60)
    rows = conn.execute("SELECT minute, SUM(n) n, SUM(e4) e4, SUM(e5) e5 FROM metrics_minute WHERE minute>=? GROUP BY minute", (minutes[0],)).fetchall()
    per = {r["minute"]: r for r in rows}
    n_series = [per[m]["n"] if m in per else 0 for m in minutes]
    e5_series = [per[m]["e5"] if m in per else 0 for m in minutes]
    total = sum(n_series); e4 = sum(r["e4"] for r in rows); e5 = sum(e5_series)
    lines = []
    if not total:
        lines.append("No request statistics yet (they are collected once a minute after this version starts).")
    else:
        peak = max(range(60), key=lambda i: n_series[i])
        lines += [f"Last 60 min: {total} requests ({total / 60:.1f}/min on average), {e4} client errors (4xx), {e5} server errors (5xx)",
                  f"Requests per minute, oldest to newest: {spark(n_series)}   peak {n_series[peak]}/min at {core.localdt(minutes[peak] + ':00.000+00:00')}",
                  f"Server errors per minute:             {spark(e5_series)}"]
        if e5 >= 3:
            attention.append(f"{e5} server errors (5xx) in the last hour; see 'Recent server errors'.")
    ep = conn.execute("""SELECT endpoint, SUM(n) n, SUM(e4) e4, SUM(e5) e5, SUM(total_ms) tot, MAX(max_ms) mx, SUM(h3+h4) slow
                         FROM metrics_minute WHERE minute>=? GROUP BY endpoint ORDER BY n DESC""", (minutes[0],)).fetchall()
    def epline(r):
        return f"{r['endpoint'][:30]:30} {r['n']:6} req  avg {r['tot'] / max(1, r['n']):5.0f} ms  max {r['mx']:6.0f} ms  slow(>=500 ms) {100 * r['slow'] / max(1, r['n']):3.0f}%  4xx {r['e4']}  5xx {r['e5']}"
    if ep:
        lines.append("")
        lines.append("Busiest pages (last 60 min):")
        lines += ["  " + epline(r) for r in ep[:10]]
        slow_eps = sorted([r for r in ep if r["n"] >= 5 and r["endpoint"] != "static"], key=lambda r: r["tot"] / r["n"], reverse=True)[:5]
        lines.append("")
        lines.append("Slowest pages on average (at least 5 requests):")
        lines += ["  " + epline(r) for r in slow_eps]
        for r in ep:
            if r["n"] >= 20 and r["slow"] / r["n"] > 0.3 and r["endpoint"] != "static":
                attention.append(f"Page '{r['endpoint']}' is slow: {100 * r['slow'] / r['n']:.0f}% of {r['n']} requests took 500 ms or more in the last hour.")
    out.append(("Requests", lines))

    cutoff3 = core.iso(now - core.timedelta(hours=3))
    slow = conn.execute("""SELECT r.at, r.method, r.path, r.endpoint, r.status, r.ms, a.username FROM slow_requests r LEFT JOIN accounts a ON a.id=r.user_id
                           WHERE r.at>=? ORDER BY r.ms DESC LIMIT 10""", (cutoff3,)).fetchall()
    out.append(("Slowest single requests (last 3 h, 700 ms or more)",
                [f"{core.localdt(r['at'])}  {r['ms']:7.0f} ms  {r['status']}  {r['method']} {r['path']}  [{r['username'] or 'anonymous'}]" for r in slow] or ["None."]))

    # ---------------- database
    from pathlib import Path as _P
    lines = []
    try:
        db_path = _P(core.DB_PATH)
        lines.append(f"Database file: {db_path.stat().st_size / 1e6:.1f} MB   write-ahead log: {(_P(str(db_path) + '-wal').stat().st_size / 1e6) if _P(str(db_path) + '-wal').exists() else 0:.1f} MB")
    except OSError:
        pass
    t = time.perf_counter()
    probe = core.db_connect()
    try:
        probe.execute("BEGIN IMMEDIATE"); waited = (time.perf_counter() - t) * 1000; probe.rollback()
    finally:
        probe.close()
    lines.append(f"Time to get the database write lock right now: {waited:.0f} ms" + ("   (slow: something is holding the database)" if waited > 500 else "   (healthy)"))
    if waited > 500:
        attention.append(f"The database write lock took {waited:.0f} ms to acquire: writes are queueing behind something.")
    locked = conn.execute("SELECT COUNT(*) FROM app_errors WHERE message LIKE '%locked%' AND at>=?", (core.iso(now - core.timedelta(hours=1)),)).fetchone()[0]
    lines.append(f"'Database is locked' errors in the last hour: {locked}")
    if locked:
        attention.append(f"{locked} 'database is locked' error(s) in the last hour.")
    counts = {t_: conn.execute(f"SELECT COUNT(*) FROM {t_}").fetchone()[0] for t_ in ("accounts", "submissions", "audit_log", "api_events")}
    lines.append("Rows: " + ", ".join(f"{k} {v}" for k, v in counts.items()))
    out.append(("Database", lines))

    # ---------------- judge, in depth
    since1 = core.iso(now - core.timedelta(hours=1))
    done = conn.execute("""SELECT s.id, s.language, s.verdict, s.submitted_at, s.judging_started_at, s.judged_at, p.code
                           FROM submissions s JOIN problems p ON p.id=s.problem_id WHERE s.judged_at>=? ORDER BY s.id DESC LIMIT 1500""", (since1,)).fetchall()
    def timing(r):
        a, b, c = parse(r["submitted_at"]), parse(r["judging_started_at"]), parse(r["judged_at"])
        return ((b - a).total_seconds() if a and b else None, (c - b).total_seconds() if b and c else None)
    def group(key):
        g: dict = {}
        for r in done:
            w, j = timing(r)
            d = g.setdefault(r[key], {"n": 0, "j": [], "w": [], "v": {}})
            d["n"] += 1; d["v"][r["verdict"]] = d["v"].get(r["verdict"], 0) + 1
            if j is not None: d["j"].append(j)
            if w is not None: d["w"].append(w)
        return g
    def gl(name, d):
        j, w = d["j"], d["w"]
        mix = ", ".join(f"{k} {v}" for k, v in sorted(d["v"].items(), key=lambda kv: -kv[1]))
        return f"{str(name):10} {d['n']:4} judged | judging avg {sum(j) / len(j) if j else 0:5.1f}s worst {max(j) if j else 0:5.1f}s | queue wait avg {sum(w) / len(w) if w else 0:4.1f}s worst {max(w) if w else 0:5.1f}s | {mix}"
    by_lang = group("language"); by_prob = group("code")
    out.append(("Judge by language (last hour)", [gl(k, v) for k, v in sorted(by_lang.items(), key=lambda kv: -kv[1]["n"])] or ["Nothing judged in the last hour."]))
    out.append(("Judge by problem (last hour)", [gl("problem " + str(k), v) for k, v in sorted(by_prob.items(), key=lambda kv: -kv[1]["n"])][:12] or ["Nothing judged in the last hour."]))
    ranked = []
    for r in conn.execute("""SELECT s.id, s.language, s.verdict, s.submitted_at, s.judging_started_at, s.judged_at, p.code FROM submissions s JOIN problems p ON p.id=s.problem_id
                             WHERE s.judged_at>=? AND s.judging_started_at IS NOT NULL""", (cutoff3,)).fetchall():
        w, j = timing(r)
        if j is not None: ranked.append((j, w, r))
    ranked.sort(key=lambda x: -x[0])
    out.append(("Slowest judgings (last 3 h)", [f"#{r['id']} problem {r['code']} {r['language']} {r['verdict']}: judging took {j:.1f}s, waited {w if w is not None else 0:.1f}s in the queue" for j, w, r in ranked[:8]] or ["None."]))
    je = conn.execute("SELECT id, language, detail FROM submissions WHERE verdict='JE' AND judged_at>=? ORDER BY id DESC LIMIT 5", (cutoff3,)).fetchall()
    if je:
        out.append(("Judge errors (JE) in the last 3 h", [f"#{r['id']} {r['language']}: {redact((r['detail'] or '')[:160])}" for r in je]))

    # ---------------- timeline
    buckets = _minute_keys(core, 24, 5)
    def bucket_of(v):
        dt = parse(v)
        if not dt: return None
        m = core.iso(dt)[:16]
        for i in range(len(buckets) - 1, -1, -1):
            if m >= buckets[i]:
                return i
        return None
    subs = conn.execute("SELECT submitted_at, judging_started_at, judged_at FROM submissions WHERE submitted_at>=?", (buckets[0],)).fetchall()
    sub_n, jud_n, wait_s, wait_c = [0] * 24, [0] * 24, [0.0] * 24, [0] * 24
    for r in subs:
        i = bucket_of(r["submitted_at"])
        if i is not None:
            sub_n[i] += 1
            a, b = parse(r["submitted_at"]), parse(r["judging_started_at"])
            if a and b: wait_s[i] += (b - a).total_seconds(); wait_c[i] += 1
        k = bucket_of(r["judged_at"])
        if k is not None: jud_n[k] += 1
    waits = [wait_s[i] / wait_c[i] if wait_c[i] else 0 for i in range(24)]
    depth = [0] * 24
    for r in conn.execute("SELECT minute, pending FROM resource_samples WHERE role='worker' AND minute>=?", (buckets[0],)).fetchall():
        i = None
        for j in range(23, -1, -1):
            if r["minute"] >= buckets[j]: i = j; break
        if i is not None and r["pending"]: depth[i] = max(depth[i], r["pending"])
    out.append(("Timeline: last 2 hours in 5-minute steps (oldest to newest)", [
        f"Submitted         {spark(sub_n)}   peak {max(sub_n)} per 5 min",
        f"Judged            {spark(jud_n)}   peak {max(jud_n)} per 5 min",
        f"Queue wait (avg)  {spark(waits)}   worst bucket {max(waits):.1f}s",
        f"Waiting (max)     {spark(depth)}   worst {max(depth)} waiting at once"]))
    if max(waits) > 30:
        attention.append(f"Queue wait reached {max(waits):.0f}s on average in a 5-minute window in the last 2 hours.")

    # ---------------- resources
    res_min = _minute_keys(core, 60)
    lines = []
    for role in ("web", "worker"):
        rows = {r["minute"]: r for r in conn.execute("SELECT * FROM resource_samples WHERE role=? AND minute>=?", (role, res_min[0])).fetchall()}
        if not rows:
            lines.append(f"{role:6}: no samples yet"); continue
        cpu = [rows[m]["cpu_pct"] if m in rows and rows[m]["cpu_pct"] is not None else 0 for m in res_min]
        mem = [rows[m]["mem_mb"] for m in rows.values() if rows[m]["mem_mb"] is not None] if False else [r["mem_mb"] for r in rows.values() if r["mem_mb"] is not None]
        lines.append(f"{role:6} CPU {spark(cpu)}  peak {max(cpu):.0f}% of one core   memory {min(mem) if mem else 0:.0f}-{max(mem) if mem else 0:.0f} MB")
        if role == "worker":
            jc = [rows[m]["judge_ctrs"] if m in rows and rows[m]["judge_ctrs"] else 0 for m in res_min]
            jcpu = [rows[m]["judge_cpu"] if m in rows and rows[m]["judge_cpu"] else 0 for m in res_min]
            lines.append(f"       judge containers {spark(jc)}  most at once {max(jc)}   their CPU {spark(jcpu)} peak {max(jcpu):.0f}%")
    host = conn.execute("SELECT minute, MAX(load1) l, MIN(mem_avail_mb) m FROM resource_samples WHERE minute>=? GROUP BY minute", (res_min[0],)).fetchall()
    hl = {r["minute"]: r for r in host}
    loads = [hl[m]["l"] if m in hl and hl[m]["l"] is not None else 0 for m in res_min]
    if host:
        lines.append(f"Host load {spark(loads)}  peak {max(loads):.2f} on {os.cpu_count()} cores   least free memory {min(r['m'] for r in host if r['m'] is not None):.0f} MB")
        if max(loads) > (os.cpu_count() or 1) * 0.85:
            attention.append(f"The host load reached {max(loads):.1f} on {os.cpu_count()} cores in the last hour: CGS and the contest were competing for the CPU.")
    out.append(("Resources (last 60 min, one sample per minute)", lines or ["No samples yet."]))

    # ---------------- sign-in funnel
    since24 = core.iso(now - core.timedelta(hours=24))
    kinds = conn.execute("SELECT kind, COUNT(*) n FROM auth_events WHERE at>=? GROUP BY kind ORDER BY n DESC", (since24,)).fetchall()
    lines = [", ".join(f"{r['kind']} {r['n']}" for r in kinds) or "No sign-in events yet (collected after this version starts)."]
    fails = conn.execute("SELECT detail, COUNT(*) n FROM auth_events WHERE at>=? AND kind='google_fail' GROUP BY detail ORDER BY n DESC LIMIT 6", (since24,)).fetchall()
    if fails:
        lines.append("Google sign-in failures by reason (last 24 h):")
        lines += [f"  {r['n']:4}  {r['detail']}" for r in fails]
        started = next((r["n"] for r in kinds if r["kind"] == "google_start"), 0); nf = sum(r["n"] for r in fails)
        if started >= 5 and nf / started > 0.3:
            attention.append(f"{nf} of {started} Google sign-in attempts failed in the last 24 hours (see the reasons).")
    recent = conn.execute("SELECT at, kind, detail FROM auth_events ORDER BY id DESC LIMIT 8").fetchall()
    if recent:
        lines.append("Latest events:")
        lines += [f"  {core.localdt(r['at'])}  {r['kind']} {r['detail']}" for r in recent]
    out.append(("Sign-in funnel (last 24 h)", lines))

    # ---------------- restarts and staff actions
    starts = conn.execute("SELECT at, role, build FROM process_starts WHERE at>=? ORDER BY id DESC LIMIT 10", (since24,)).fetchall()
    out.append(("Restarts (last 24 h)", [f"{core.localdt(r['at'])}  {r['role']} started   {r['build']}" for r in starts] or ["None recorded."]))
    if sum(1 for r in starts if r["role"] == "worker") >= 3:
        attention.append("The judge worker restarted 3 or more times in the last 24 hours.")
    acts = conn.execute("SELECT created_at, actor_name, action, detail FROM audit_log ORDER BY id DESC LIMIT 12").fetchall()
    out.append(("Latest staff actions", [f"{core.localdt(r['created_at'])}  {r['actor_name']}: {r['action']}  {redact((r['detail'] or '')[:90])}" for r in acts] or ["None."]))
    return out


# ====================================================================== after the contest: how did the app do?
def _pct(values, q):
    values = sorted(values)
    if not values: return 0.0
    return values[min(len(values) - 1, int(round(q * (len(values) - 1))))]


_ACCESS = re.compile(r'^(?P<ip>\S+) \S+ \S+ \[(?P<ts>[^\]]+)\] "(?P<method>[A-Z]+) (?P<path>\S+) [^"]*" (?P<status>\d{3}) (?P<size>\S+)')


def parse_access_log(text: str, start, end):
    """Web access-log lines (gunicorn format) inside [start, end]: a list of (datetime, ip, method, path, status)."""
    from datetime import datetime, timezone
    rows = []
    for line in text.splitlines():
        m = _ACCESS.match(line.split("|", 1)[-1].strip() if line.startswith("web") else line)
        if not m:
            continue
        try:
            at = datetime.strptime(m.group("ts"), "%d/%b/%Y:%H:%M:%S %z").astimezone(timezone.utc)
        except ValueError:
            continue
        if start <= at <= end and "/healthz" not in m.group("path"):
            rows.append((at, m.group("ip"), m.group("method"), m.group("path").split("?")[0], int(m.group("status"))))
    return rows


def contest_report(core, conn, contest_id: str | None = None, log_text: str | None = None) -> str:
    """A plain-text retrospective of one contest, built only from data that already exists (database, and optionally the web access log)."""
    from datetime import timezone
    c = core.contest_by_id(contest_id, conn) if contest_id else core.active_contest(conn)
    if not c or not c["start_at"]:
        return "No contest with a start time to report on.\n"
    cid = c["id"]
    start = core.parse_dt(c["start_at"]); end = start + core.timedelta(minutes=c["duration_minutes"])
    lo, hi = core.iso(start - core.timedelta(minutes=5)), core.iso(end + core.timedelta(minutes=30))
    rows = conn.execute("""SELECT s.id, s.account_id, s.language, s.verdict, s.status, s.submitted_at, s.judging_started_at, s.judged_at, s.runtime_ms, s.feedback,
                                  s.manual_override, s.reviewed_at, p.code FROM submissions s JOIN problems p ON p.id=s.problem_id
                           WHERE s.contest_id=? AND s.submitted_at>=? AND s.submitted_at<=? ORDER BY s.id""", (cid, lo, hi)).fetchall()
    out = [f"POST-CONTEST REPORT: {c['name']} (id {cid})",
           f"Ran {core.localdt(core.iso(start))} to {core.localdt(core.iso(end))} Philippine time ({c['duration_minutes']} minutes)", ""]
    reg = conn.execute("SELECT COUNT(*) FROM registrations WHERE contest_id=?", (cid,)).fetchone()[0]
    who = {}
    for r in rows: who.setdefault(r["account_id"], []).append(r)
    solvers = {a: {r["code"] for r in rs if r["verdict"] == "AC"} for a, rs in who.items()}
    dist = {"0": 0, "1": 0, "2": 0, "3": 0, "4+": 0}
    for a, probs in solvers.items(): dist["4+" if len(probs) >= 4 else str(len(probs))] += 1
    out += ["PARTICIPATION", f"  Registered {reg}; submitted at least once {len(who)}; solved at least one {sum(1 for v in solvers.values() if v)}",
            "  Problems solved per contestant (of those who submitted): " + ", ".join(f"{k}: {v}" for k, v in dist.items()), f"  Submissions in total: {len(rows)}", ""]

    def t(v): return core.parse_dt(v) if v else None
    waits, runs, per_lang, per_prob = [], [], {}, {}
    events = []
    rejudged = 0
    je_students = set()
    for r in rows:
        a, b, d = t(r["submitted_at"]), t(r["judging_started_at"]), t(r["judged_at"])
        w = (b - a).total_seconds() if a and b else None
        j = (d - b).total_seconds() if b and d else None
        if w is not None and w > 900:
            rejudged += 1                       # judged long after it was submitted: almost always a judge pressing Rejudge
        elif w is not None:
            waits.append(w); events += [(a, 1), (b, -1)]
        if j is not None: runs.append(j)
        L = per_lang.setdefault(r["language"], {"n": 0, "j": []}); L["n"] += 1
        if j is not None: L["j"].append(j)
        P = per_prob.setdefault(r["code"], {"n": 0, "ac": 0, "v": {}, "solvers": set(), "first": None, "j": []})
        P["n"] += 1; P["v"][r["verdict"]] = P["v"].get(r["verdict"], 0) + 1
        if r["verdict"] == "AC":
            P["ac"] += 1; P["solvers"].add(r["account_id"]); minute = (a - start).total_seconds() / 60 if a else None
            if minute is not None and (P["first"] is None or minute < P["first"]): P["first"] = minute
        if j is not None: P["j"].append(j)
        if r["verdict"] == "JE": je_students.add(r["account_id"])
    # judging: did it keep up?
    out.append("JUDGING: DID IT KEEP UP?")
    if waits:
        slow30 = sum(1 for w in waits if w > 30); slow60 = sum(1 for w in waits if w > 60)
        events.sort(); depth = peak_depth = 0; peak_at = None
        for at, d in events:
            depth += d
            if depth > peak_depth: peak_depth, peak_at = depth, at
        out += [f"  Time a submission waited before judging started: average {sum(waits) / len(waits):.1f}s, median {_pct(waits, .5):.1f}s, 95th percentile {_pct(waits, .95):.1f}s, worst {max(waits):.1f}s",
                f"  Waited more than 30 s: {slow30} of {len(waits)} ({100 * slow30 / len(waits):.1f}%); more than 60 s: {slow60}",
                f"  Most submissions waiting at the same moment: {peak_depth}" + (f" at {core.localdt(core.iso(peak_at))}" if peak_at else "")]
    if rejudged:
        out.append(f"  {rejudged} submission(s) were judged more than 15 minutes after they were submitted: a judge pressed Rejudge (left out of the waiting figures above)")
    if runs:
        out.append(f"  Judging time per submission: average {sum(runs) / len(runs):.1f}s, 95th percentile {_pct(runs, .95):.1f}s, worst {max(runs):.1f}s")
    for lang, d in sorted(per_lang.items(), key=lambda kv: -kv[1]["n"]):
        out.append(f"    {lang:8} {d['n']:4} submissions, judging avg {sum(d['j']) / len(d['j']) if d['j'] else 0:.1f}s, worst {max(d['j']) if d['j'] else 0:.1f}s")
    je = sum(1 for r in rows if r["verdict"] == "JE")
    out.append(f"  Judge errors (JE: the judge itself failed): {je}" + (f", affecting {len(je_students)} contestant(s)" if je else ""))
    buckets = 36; span = (end - start).total_seconds() / buckets
    sub_n = [0] * buckets; bw = [[] for _ in range(buckets)]
    for r in rows:
        a, b = t(r["submitted_at"]), t(r["judging_started_at"])
        if not a: continue
        i = int((a - start).total_seconds() // span)
        if 0 <= i < buckets:
            sub_n[i] += 1
            if b and (b - a).total_seconds() <= 900: bw[i].append((b - a).total_seconds())
    out += ["", f"TIMELINE (the contest in {int(span / 60)}-minute steps, start to end)",
            f"  Submissions     {spark(sub_n)}   peak {max(sub_n)} in {int(span / 60)} min",
            f"  Queue wait avg  {spark([sum(x) / len(x) if x else 0 for x in bw])}   worst step {max((sum(x) / len(x) if x else 0) for x in bw):.1f}s"]
    out += ["", "VERDICTS AND PROBLEMS", "  problem | attempts | accepted | solvers | first solve | judging avg | verdict mix"]
    for code, d in sorted(per_prob.items()):
        mix = ", ".join(f"{k} {v}" for k, v in sorted(d["v"].items(), key=lambda kv: -kv[1]))
        first = f"minute {d['first']:.0f}" if d["first"] is not None else "never"
        out.append(f"  {code:7} | {d['n']:8} | {100 * d['ac'] / d['n']:7.0f}% | {len(d['solvers']):7} | {first:11} | {sum(d['j']) / len(d['j']) if d['j'] else 0:9.1f}s | {mix}")
    never = [k for k, d in per_prob.items() if d["ac"] == 0]
    allp = [r[0] for r in conn.execute("SELECT code FROM problems WHERE contest_id=? ORDER BY ordinal,code", (cid,)).fetchall()]
    untouched = [x for x in allp if x not in per_prob]
    if untouched: out.append(f"  Never attempted: {', '.join(untouched)}")
    if never: out.append(f"  Attempted but never solved: {', '.join(sorted(never))}")
    tle = {k: d["v"].get("TLE", 0) for k, d in per_prob.items() if d["v"].get("TLE", 0)}
    if tle: out.append("  Time-limit verdicts by problem (check the limits are fair): " + ", ".join(f"{k} {v}" for k, v in sorted(tle.items(), key=lambda kv: -kv[1])))
    reviewed = sum(1 for r in rows if r["reviewed_at"]); overrides = sum(1 for r in rows if r["manual_override"])
    nonac = [r for r in rows if r["verdict"] and r["verdict"] != "AC"]
    out += ["", "JUDGES", f"  Code reviews done: {reviewed}, verdicts changed by a judge: {overrides}",
            f"  Not-accepted submissions with feedback: {sum(1 for r in nonac if r['feedback'])} of {len(nonac)}"]
    if log_text is not None:
        logs = parse_access_log(log_text, start, end + core.timedelta(minutes=5))
        out += ["", "WEB SERVER (from the access log; it records requests and statuses, not how long each took)"]
        if not logs:
            out.append("  No access-log lines found for this time window (the log may have been cleared).")
        else:
            classes = {"2xx": 0, "3xx": 0, "4xx": 0, "5xx": 0}
            for _, _, _, _, st in logs: classes[f"{st // 100}xx"] = classes.get(f"{st // 100}xx", 0) + 1
            per_min: dict = {}
            for at, *_ in logs: per_min[core.iso(at)[:16]] = per_min.get(core.iso(at)[:16], 0) + 1
            series = [0] * buckets
            for at, *_ in logs:
                i = int((at - start).total_seconds() // span)
                if 0 <= i < buckets: series[i] += 1
            pk = max(per_min, key=per_min.get)
            ips = {ip for _, ip, *_ in logs}
            out += [f"  {len(logs)} requests from {len(ips)} different addresses; statuses: " + ", ".join(f"{k} {v}" for k, v in classes.items() if v),
                    f"  Requests            {spark(series)}   peak {per_min[pk]} in one minute at {core.localdt(pk + ':00.000+00:00')}"]
            bad = [(at, m, p_, st) for at, ip, m, p_, st in logs if st >= 500]
            out.append(f"  Server errors (5xx): {len(bad)}" + (":" if bad else ""))
            out += [f"    {core.localdt(core.iso(at))} {m} {p_} -> {st}" for at, m, p_, st in bad[:10]]
            lim = sum(1 for *_, st in logs if st == 429); out.append(f"  Sign-in attempts blocked by the rate limit (429): {lim}")
            posts = sum(1 for _, _, m, p_, _ in logs if m == "POST" and p_ == "/submit")
            lb = sum(1 for _, _, m, p_, _ in logs if m == "GET" and p_ in ("/leaderboard", "/scoreboard/projector"))
            out.append(f"  Submit form posts: {posts}; leaderboard/projector page loads: {lb}")
    # the verdict in plain words
    out += ["", "IN SHORT"]
    if waits:
        p95 = _pct(waits, .95)
        out.append("  Judging: " + ("kept up comfortably" if p95 < 10 else "kept up, with some waiting at the busiest moments" if p95 < 30 else "fell behind at times") + f" (95% of submissions started judging within {p95:.0f}s).")
    out.append("  Judge failures: " + ("none" if not je else f"{je} judge error(s): look at them"))
    return "\n".join(out) + "\n"
