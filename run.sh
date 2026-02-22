#!/bin/bash
# WhatsApp MCP — start bridge + server

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

# Install bridge deps if needed
if [ ! -d "bridge/node_modules" ]; then
    echo "[run] Installing bridge dependencies..."
    (cd bridge && npm install)
fi

# Start bridge in background
echo "[run] Starting WhatsApp bridge on :3010..."
(cd bridge && npx tsx src/server.ts) &
BRIDGE_PID=$!

# Cleanup on exit
cleanup() {
    echo ""
    echo "[run] Shutting down..."
    kill $BRIDGE_PID 2>/dev/null
    wait $BRIDGE_PID 2>/dev/null
    exit 0
}
trap cleanup SIGINT SIGTERM

# Give bridge a moment to start
sleep 2

# Start Python server in foreground
echo "[run] Starting Python server on :3009..."
python -m uvicorn app.main:app --host 0.0.0.0 --port 3009 --reload &
PYTHON_PID=$!

# Voice assistant (optional — run in separate terminal)
# python voice/assistant.py

# Wait for either process to exit
wait $BRIDGE_PID $PYTHON_PID
cleanup
