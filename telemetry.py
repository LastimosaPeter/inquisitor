"""Telemetry: the history behind the Diagnostics page.

A status page only shows "now". This module keeps the last few days in small SQLite tables so you can ask how the app behaved:

* metrics_minute   per minute and per page: how many requests, how many failed, how slow (a coarse histogram)
* slow_requests    the slowest individual requests (path and status only, never form data or query strings)
* resource_samples once a minute per process: CPU, memory, host load, judge queue depth, judge containers
* auth_events      sign-in funnel: Google started / failed (with the reason) / signed in / registered, staff password sign-ins
* process_starts   when the website and the judge worker (re)started

Nothing here can break a request: every entry point swallows its own errors. Everything is bounded and trimmed after 3 days.
"""
from __future__ import annotations

import os
import re
import subprocess
import threading
import time
from collections import deque
from pathlib import Path

SLOW_MS = 700.0
FLUSH_EVERY = 20.0
KEEP_DAYS = 3
_lock = threading.Lock()
_agg: dict[tuple[str, str], list[float]] = {}
_slow: deque = deque(maxlen=120)
_events: dict = {"auth": [], "errors": [], "workers": {}}   # written by the background thread, never from inside a request
_state = {"flusher": False, "last_cpu": None, "last_sample_minute": "", "last_trim": 0.0}


def ensure_tables(conn) -> None:
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS metrics_minute (
        minute TEXT NOT NULL, endpoint TEXT NOT NULL, n INTEGER NOT NULL DEFAULT 0, e4 INTEGER NOT NULL DEFAULT 0, e5 INTEGER NOT NULL DEFAULT 0,
        total_ms REAL NOT NULL DEFAULT 0, max_ms REAL NOT NULL DEFAULT 0,
        h1 INTEGER NOT NULL DEFAULT 0, h2 INTEGER NOT NULL DEFAULT 0, h3 INTEGER NOT NULL DEFAULT 0, h4 INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY (minute, endpoint));
    CREATE TABLE IF NOT EXISTS slow_requests (
        id INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT NOT NULL, method TEXT NOT NULL DEFAULT '', path TEXT NOT NULL DEFAULT '',
        endpoint TEXT NOT NULL DEFAULT '', status INTEGER NOT NULL DEFAULT 0, ms REAL NOT NULL DEFAULT 0, user_id INTEGER);
    CREATE TABLE IF NOT EXISTS resource_samples (
        minute TEXT NOT NULL, role TEXT NOT NULL, cpu_pct REAL, mem_mb REAL, load1 REAL, mem_avail_mb REAL,
        pending INTEGER, judging INTEGER, judge_ctrs INTEGER, judge_cpu REAL, judge_mem_mb REAL,
        PRIMARY KEY (minute, role));
    CREATE TABLE IF NOT EXISTS auth_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT NOT NULL, kind TEXT NOT NULL, detail TEXT NOT NULL DEFAULT '');
    CREATE TABLE IF NOT EXISTS process_starts (
        id INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT NOT NULL, role TEXT NOT NULL, build TEXT NOT NULL DEFAULT '');
    CREATE INDEX IF NOT EXISTS idx_slow_requests_at ON slow_requests(at);
    CREATE INDEX IF NOT EXISTS idx_auth_events_at ON auth_events(at);
    """)


def _minute(core, dt=None) -> str:
    return core.iso(dt or core.utcnow())[:16]


# ------------------------------------------------------------------ requests
def record_request(core, endpoint: str, method: str, path: str, status: int, ms: float, user_id) -> None:
    try:
        key = (_minute(core), endpoint[:80])
        with _lock:
            row = _agg.setdefault(key, [0, 0, 0, 0.0, 0.0, 0, 0, 0, 0])
            row[0] += 1
            if 400 <= status < 500: row[1] += 1
            elif status >= 500: row[2] += 1
            row[3] += ms
            row[4] = max(row[4], ms)
            row[5 if ms < 100 else 6 if ms < 500 else 7 if ms < 1500 else 8] += 1
            if ms >= SLOW_MS and endpoint != "static":
                _slow.append((core.iso(core.utcnow()), method[:8], path.split("?")[0][:160], endpoint[:80], status, round(ms, 1), user_id))
    except Exception:
        pass


def flush(core) -> None:
    with _lock:
        agg, slow = dict(_agg), list(_slow)
        auth, errors, workers = list(_events["auth"]), list(_events["errors"]), dict(_events["workers"])
        _agg.clear(); _slow.clear(); _events["auth"].clear(); _events["errors"].clear(); _events["workers"].clear()
    if not (agg or slow or auth or errors or workers):
        return
    try:
        conn = core.db_connect()
        try:
            ensure_tables(conn)
            import diagnostics
            diagnostics.ensure_tables(conn)
            for e in auth:
                conn.execute("INSERT INTO auth_events(at,kind,detail) VALUES(?,?,?)", e)
            for e in errors:
                conn.execute("INSERT INTO app_errors(at,method,path,username,kind,message,trace) VALUES(?,?,?,?,?,?,?)", e)
            if errors:
                conn.execute("DELETE FROM app_errors WHERE id NOT IN (SELECT id FROM app_errors ORDER BY id DESC LIMIT 200)")
            for name, (at, state, inc) in workers.items():
                conn.execute("""INSERT INTO worker_status(name,at,state,judged) VALUES(?,?,?,?)
                                ON CONFLICT(name) DO UPDATE SET at=excluded.at, state=excluded.state, judged=worker_status.judged+excluded.judged""", (name, at, state, inc))
            for (minute, endpoint), r in agg.items():
                conn.execute("""INSERT INTO metrics_minute(minute,endpoint,n,e4,e5,total_ms,max_ms,h1,h2,h3,h4) VALUES(?,?,?,?,?,?,?,?,?,?,?)
                                ON CONFLICT(minute,endpoint) DO UPDATE SET n=n+excluded.n, e4=e4+excluded.e4, e5=e5+excluded.e5,
                                  total_ms=total_ms+excluded.total_ms, max_ms=MAX(max_ms,excluded.max_ms),
                                  h1=h1+excluded.h1, h2=h2+excluded.h2, h3=h3+excluded.h3, h4=h4+excluded.h4""",
                             (minute, endpoint, int(r[0]), int(r[1]), int(r[2]), r[3], r[4], int(r[5]), int(r[6]), int(r[7]), int(r[8])))
            for s in slow:
                conn.execute("INSERT INTO slow_requests(at,method,path,endpoint,status,ms,user_id) VALUES(?,?,?,?,?,?,?)", s)
            conn.commit()
        finally:
            conn.close()
    except Exception:
        pass


# ------------------------------------------------------------------ resources
def _read(path: str):
    try:
        return Path(path).read_text().strip()
    except OSError:
        return None


def _cgroup_cpu_usec():
    text = _read("/sys/fs/cgroup/cpu.stat")
    if text:
        for line in text.splitlines():
            if line.startswith("usage_usec"):
                return int(line.split()[1])
    old = _read("/sys/fs/cgroup/cpuacct/cpuacct.usage")
    return int(old) // 1000 if old else None


def _cgroup_mem_mb():
    for path in ("/sys/fs/cgroup/memory.current", "/sys/fs/cgroup/memory/memory.usage_in_bytes"):
        v = _read(path)
        if v and v.isdigit():
            return int(v) / 1048576
    return None


def _host_mem_avail_mb():
    try:
        for line in open("/proc/meminfo"):
            if line.startswith("MemAvailable:"):
                return int(line.split()[1]) / 1024
    except OSError:
        pass
    return None


def _judge_containers():
    """Count and total CPU/memory of the running judge sandboxes (docker stats, a couple of seconds, best effort)."""
    try:
        out = subprocess.run(["docker", "stats", "--no-stream", "--format", "{{.Name}}|{{.CPUPerc}}|{{.MemUsage}}"],
                             capture_output=True, text=True, timeout=8).stdout
    except Exception:
        return None, None, None
    n, cpu, mem = 0, 0.0, 0.0
    for line in out.splitlines():
        parts = line.split("|")
        if len(parts) != 3 or not parts[0].startswith("inquisitor-judge-"):
            continue
        n += 1
        try: cpu += float(parts[1].strip("% "))
        except ValueError: pass
        m = re.match(r"([\d.]+)\s*([KMG]i?B)", parts[2])
        if m:
            mem += float(m.group(1)) * {"KB": 1 / 1024, "KIB": 1 / 1024, "MB": 1, "MIB": 1, "GB": 1024, "GIB": 1024}[m.group(2).upper()]
    return n, cpu, mem


def sample_resources(core, role: str, interval: float) -> None:
    try:
        minute = _minute(core)
        if _state["last_sample_minute"] == minute:
            return
        _state["last_sample_minute"] = minute
        cpu_now, cpu_pct = _cgroup_cpu_usec(), None
        prev = _state["last_cpu"]
        if cpu_now is not None and prev and cpu_now >= prev[0]:
            elapsed = max(1e-3, time.time() - prev[1])
            cpu_pct = (cpu_now - prev[0]) / (elapsed * 1e6) * 100
        _state["last_cpu"] = (cpu_now, time.time()) if cpu_now is not None else None
        try: load1 = os.getloadavg()[0]
        except OSError: load1 = None
        pending = judging = ctrs = jcpu = jmem = None
        conn = core.db_connect()
        try:
            ensure_tables(conn)
            if role == "worker":
                counts = dict(conn.execute("SELECT status, COUNT(*) FROM submissions GROUP BY status").fetchall())
                pending, judging = counts.get("PENDING", 0), counts.get("JUDGING", 0)
                ctrs, jcpu, jmem = _judge_containers()
            conn.execute("INSERT OR REPLACE INTO resource_samples(minute,role,cpu_pct,mem_mb,load1,mem_avail_mb,pending,judging,judge_ctrs,judge_cpu,judge_mem_mb) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                         (minute, role, cpu_pct, _cgroup_mem_mb(), load1, _host_mem_avail_mb(), pending, judging, ctrs, jcpu, jmem))
            if time.time() - _state["last_trim"] > 3600:
                _state["last_trim"] = time.time()
                cutoff = core.iso(core.utcnow() - core.timedelta(days=KEEP_DAYS))
                for table, col in (("metrics_minute", "minute"), ("resource_samples", "minute"), ("slow_requests", "at"), ("auth_events", "at"), ("process_starts", "at")):
                    conn.execute(f"DELETE FROM {table} WHERE {col}<?", (cutoff[:16] if col == "minute" else cutoff,))
            conn.commit()
        finally:
            conn.close()
    except Exception:
        pass


def _build_info() -> str:
    for path in (Path(__file__).with_name("BUILD_INFO"), Path("/app/BUILD_INFO")):
        try:
            return path.read_text().strip()[:120]
        except OSError:
            continue
    return "unknown"


def start(core, role: str) -> None:
    """Start the background thread (once per process): flushes request counters and samples resources every minute."""
    with _lock:
        if _state["flusher"]:
            return
        _state["flusher"] = True
    def loop():
        try:       # the start is recorded from the background thread, never from inside the first request
            conn = core.db_connect()
            try:
                ensure_tables(conn)
                conn.execute("INSERT INTO process_starts(at,role,build) VALUES(?,?,?)", (core.iso(core.utcnow()), role, _build_info()))
                conn.commit()
            finally:
                conn.close()
        except Exception:
            pass
        while True:
            time.sleep(FLUSH_EVERY)
            flush(core)
            sample_resources(core, role, FLUSH_EVERY)

    threading.Thread(target=loop, name=f"telemetry-{role}", daemon=True).start()


# ------------------------------------------------------------------ sign-in funnel
_EMAIL = re.compile(r"([A-Za-z0-9._%+-])[A-Za-z0-9._%+-]*@([A-Za-z0-9.-]+)")


def auth_event(core, kind: str, detail: str = "") -> None:
    """One line in the sign-in history. Email addresses are shortened to the first letter and the domain. Queued in memory (the background
    thread writes it), so a sign-in never waits on the database for this."""
    try:
        detail = _EMAIL.sub(lambda m: f"{m.group(1)}***@{m.group(2)}", detail)[:200]
        with _lock:
            if len(_events["auth"]) < 2000:
                _events["auth"].append((core.iso(core.utcnow()), kind[:40], detail))
    except Exception:
        pass


def queue_error(row: tuple) -> None:
    with _lock:
        if len(_events["errors"]) < 500:
            _events["errors"].append(row)


def queue_heartbeat(core, name: str, state: str, judged_inc: int = 0) -> None:
    with _lock:
        prev = _events["workers"].get(name)
        _events["workers"][name] = (core.iso(core.utcnow()), state[:80], (prev[2] if prev else 0) + judged_inc)
