#!/usr/bin/env bash
# One-time setup: virtual environment + dependencies.
#   ./setup.sh
set -euo pipefail
cd "$(dirname "$0")"

echo "==> Python"
if command -v python3 >/dev/null 2>&1; then PY=python3; else
  echo "python3 not found. Install Python 3.11 from python.org and re-run."; exit 1
fi
$PY --version

echo "==> Checking tkinter (the GUI needs it)"
if ! $PY -c "import tkinter" >/dev/null 2>&1; then
  echo
  echo "  tkinter is MISSING from this Python."
  echo "  The console engine and tests will work, but the GUI will not start."
  echo
  echo "  macOS fix, pick one:"
  echo "    - brew install python-tk        (if this is Homebrew python)"
  echo "    - install Python 3.11 from python.org, then delete .venv and re-run"
  echo
  read -r -p "  Continue anyway? [y/N] " ans
  [[ "$ans" =~ ^[Yy]$ ]] || exit 1
fi

echo "==> Virtual environment (.venv)"
[ -d .venv ] || $PY -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip --quiet

echo "==> Dependencies"
pip install -r requirements.txt --quiet

echo "==> .env"
if [ ! -f .env ]; then
  cp .env.example .env
  chmod 600 .env
  echo "    created .env — fill in your Dhan client id, PIN and TOTP secret"
else
  echo "    .env already present, left untouched"
fi

echo "==> Offline tests"
python test_indicators.py >/dev/null && echo "    indicators OK"
python test_logic.py      >/dev/null && echo "    strategy   OK"

cat <<'MSG'

Setup complete.

  Terminal:
    source .venv/bin/activate
    python doctor.py --api
    python app.py

  VS Code:
    open this folder, then Cmd+Shift+P -> Python: Select Interpreter
    -> ./.venv/bin/python
    then press F5 and pick a configuration.

MSG
