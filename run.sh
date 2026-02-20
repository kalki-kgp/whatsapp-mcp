#!/bin/bash
# WhatsApp Assistant — start server on port 3009

if [ -z "$NEBIUS_API_KEY" ]; then
    echo "ERROR: NEBIUS_API_KEY environment variable is not set."
    echo "Run: export NEBIUS_API_KEY='your-key-here'"
    exit 1
fi

cd "$(dirname "$0")"

# Activate venv
if [ -d "venv" ]; then
    source venv/bin/activate
fi

python -m uvicorn app.main:app --host 0.0.0.0 --port 3009 --reload
