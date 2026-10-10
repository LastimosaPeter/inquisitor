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


def main():
    parser = argparse.ArgumentParser(description="Inquisitor management utility")
    parser.add_argument("command", choices=["init", "backup", "check"])
    args = parser.parse_args()
    {"init": init_command, "backup": backup_command, "check": check_command}[args.command]()


if __name__ == "__main__":
    main()
