#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
export INQUISITOR_DEMO_ACCOUNTS="${INQUISITOR_DEMO_ACCOUNTS:-1}"
export INQUISITOR_JUDGE_BACKEND="${INQUISITOR_JUDGE_BACKEND:-local}"
if [ ! -x .venv/bin/python ]; then
  echo "Creating virtual environment..."
  python3 -m venv .venv
fi
source .venv/bin/activate
if ! python -c "import flask, tzdata, reportlab, qrcode, xlsxwriter" >/dev/null 2>&1; then
  echo "Installing/updating Inquisitor v1.0.7 requirements. Internet access may be needed once..."
  python -m pip install -r requirements.txt
fi
echo "Starting Inquisitor v1.0.7 in local development mode..."
python app.py
