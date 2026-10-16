"""Dedicated judging and maintenance process for deployed Inquisitor."""
from __future__ import annotations
import os
import sqlite3
import time

import app
import features_v1 as fx


def wait_for_database():
    deadline = time.time() + int(os.environ.get("INQUISITOR_DB_WAIT_SECONDS", "120"))
    while time.time() < deadline:
        try:
            if app.DB_PATH.exists():
                conn = app.db_connect()
                ok = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='contests'").fetchone()
                conn.close()
                if ok:
                    return
        except sqlite3.Error:
            pass
        time.sleep(1)
    raise RuntimeError("Timed out waiting for initialized Inquisitor database.")


def main():
    wait_for_database()
    app.start_worker()
    fx.start_maintenance_worker()
    print(f"Inquisitor worker online ({app.judge_worker_count()} judge thread(s))", flush=True)
    while True:
        time.sleep(3600)


if __name__ == "__main__":
    main()
