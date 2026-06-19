#!/bin/bash
#
# Email Scheduler launcher
# - Creates a local virtual environment on first run
# - Installs/updates dependencies
# - Starts the web server and opens it in your browser
#
set -e

DIR="$(cd "$(dirname "$0")" && pwd)"
VENV="$DIR/.venv"
PORT=5001

# Require python3
if ! command -v python3 >/dev/null 2>&1; then
    echo "Error: python3 is not installed."
    echo "Install it from https://www.python.org/downloads/ or with: brew install python"
    exit 1
fi

# First-run setup: create venv and install dependencies
if [ ! -d "$VENV" ]; then
    echo "First run — setting up. This takes a minute..."
    python3 -m venv "$VENV"
    "$VENV/bin/pip" install --quiet --upgrade pip
    "$VENV/bin/pip" install --quiet -r "$DIR/requirements.txt"
    echo "Setup complete."
fi

# Free the port if a previous instance is still running
if lsof -ti ":$PORT" >/dev/null 2>&1; then
    echo "Port $PORT in use — stopping the old instance..."
    lsof -ti ":$PORT" | xargs kill -9 2>/dev/null || true
    sleep 1
fi

# Open the browser shortly after the server starts
( sleep 2 && open "http://127.0.0.1:$PORT" ) &

echo ""
echo "  Email Scheduler is running at http://127.0.0.1:$PORT"
echo "  Press Ctrl+C in this window to stop it."
echo ""

exec "$VENV/bin/python3" "$DIR/web_app.py"
