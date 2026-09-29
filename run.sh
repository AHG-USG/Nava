#!/usr/bin/env bash
# Quick start for Mac/Linux: creates a virtualenv (first run only),
# installs dependencies, and starts the server.
set -e
cd "$(dirname "$0")/backend"

if [ ! -d "venv" ]; then
  echo "در حال ساخت محیط مجازی پایتون..."
  python3 -m venv venv
fi

source venv/bin/activate
pip install -q -r requirements.txt

echo ""
echo "نوا روی http://localhost:5000 در حال اجراست. برای توقف Ctrl+C را بزن."
echo ""
python app.py
