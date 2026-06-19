#!/bin/bash
# Run the Outlook Email Scheduler
DIR="$(cd "$(dirname "$0")" && pwd)"
VENV="$DIR/.venv"

if [ ! -d "$VENV" ]; then
    echo "Setting up virtual environment..."
    python3 -m venv "$VENV"
    "$VENV/bin/pip" install --quiet -r "$DIR/requirements.txt"
fi

"$VENV/bin/python3" "$DIR/app.py"
