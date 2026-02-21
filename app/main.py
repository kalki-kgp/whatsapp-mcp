import json
import logging
import subprocess
import uuid
from datetime import datetime, timezone

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from app.agent import chat, chat_sync
from app.db import refresh_db
from app.config import SERVER_PORT, BRIDGE_URL
from app.scheduler import start_scheduler, list_scheduled
from app.settings import get_settings, update_settings

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="WhatsApp Assistant", version="0.2.0")

# In-memory conversation store (per session)
conversations: dict[str, list[dict]] = {}

# Voice event stream — voice assistant pushes events here, UI polls them
voice_events: list[dict] = []
voice_event_id: int = 0


@app.on_event("startup")
async def startup_event():
    start_scheduler()


@app.get("/api/health")
async def health():
    return {"status": "ok", "version": app.version}


@app.get("/", response_class=HTMLResponse)
async def index():
    return HTML_PAGE


@app.post("/api/chat")
async def api_chat(request: Request):
    """Chat endpoint. Accepts {"message": str, "conversation_id": str?}"""
    body = await request.json()
    user_message = body.get("message", "").strip()
    conv_id = body.get("conversation_id") or str(uuid.uuid4())

    if not user_message:
        return {"error": "Empty message"}

    # Get or create conversation
    if conv_id not in conversations:
        conversations[conv_id] = []

    conversations[conv_id].append({"role": "user", "content": user_message})

    # Run agent
    result = chat_sync(conversations[conv_id])

    # Add assistant response to history
    conversations[conv_id].append({"role": "assistant", "content": result["response"]})

    return {
        "conversation_id": conv_id,
        "response": result["response"],
        "tool_calls": result["tool_calls"],
    }


@app.post("/api/chat/stream")
async def api_chat_stream(request: Request):
    """Streaming chat endpoint. Returns SSE events."""
    body = await request.json()
    user_message = body.get("message", "").strip()
    conv_id = body.get("conversation_id") or str(uuid.uuid4())

    if not user_message:
        return {"error": "Empty message"}

    if conv_id not in conversations:
        conversations[conv_id] = []

    conversations[conv_id].append({"role": "user", "content": user_message})

    def generate():
        yield f"data: {json.dumps({'type': 'conv_id', 'conversation_id': conv_id})}\n\n"
        final_content = ""
        for event in chat(conversations[conv_id]):
            yield f"data: {json.dumps(event)}\n\n"
            if event["type"] == "message":
                final_content = event["content"]
        # Save assistant response
        if final_content:
            conversations[conv_id].append({"role": "assistant", "content": final_content})
        yield "data: [DONE]\n\n"

    return StreamingResponse(generate(), media_type="text/event-stream")


@app.post("/api/refresh")
async def api_refresh():
    """Force refresh the WhatsApp database copies."""
    refresh_db()
    return {"status": "ok", "message": "Database copies refreshed"}


@app.delete("/api/conversation/{conv_id}")
async def clear_conversation(conv_id: str):
    """Clear a conversation's history."""
    conversations.pop(conv_id, None)
    return {"status": "ok"}


# ---------------------------------------------------------------------------
# Bridge proxy endpoints
# ---------------------------------------------------------------------------

@app.get("/api/bridge/status")
async def bridge_status():
    """Proxy to WhatsApp bridge status endpoint."""
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(f"{BRIDGE_URL}/api/status", timeout=5)
            return resp.json()
    except httpx.ConnectError:
        return {"status": "bridge_offline"}
    except Exception:
        return {"status": "bridge_offline"}


@app.get("/api/bridge/qr")
async def bridge_qr():
    """Proxy to WhatsApp bridge QR code endpoint."""
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(f"{BRIDGE_URL}/api/qr", timeout=5)
            return resp.json()
    except httpx.ConnectError:
        return {"status": "bridge_offline", "message": "Bridge not running"}
    except Exception:
        return {"status": "error", "message": "Failed to get QR code"}


@app.get("/api/bridge/incoming")
async def bridge_incoming(since: int = 0):
    """Proxy to bridge incoming messages endpoint."""
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(f"{BRIDGE_URL}/api/incoming", params={"since": since}, timeout=5)
            return resp.json()
    except httpx.ConnectError:
        return {"messages": [], "count": 0}
    except Exception:
        return {"messages": [], "count": 0}


@app.get("/api/scheduled")
async def api_scheduled():
    """Get pending scheduled messages."""
    messages = list_scheduled()
    return {"scheduled_messages": messages, "count": len(messages)}


# ---------------------------------------------------------------------------
# Settings endpoints
# ---------------------------------------------------------------------------

@app.get("/api/settings")
async def api_get_settings():
    """Return current settings."""
    return get_settings()


@app.put("/api/settings")
async def api_put_settings(request: Request):
    """Partial update of settings."""
    body = await request.json()
    updated = update_settings(body)
    return updated


@app.get("/api/tts-voices")
async def api_tts_voices():
    """Return list of available macOS TTS voices via `say -v ?`."""
    try:
        result = subprocess.run(
            ["say", "-v", "?"], capture_output=True, text=True, timeout=5
        )
        voices = []
        for line in result.stdout.strip().splitlines():
            # Format: "Name       lang_REGION  # Sample text"
            parts = line.split("#", 1)
            name_lang = parts[0].strip()
            tokens = name_lang.split()
            if len(tokens) >= 2:
                lang = tokens[-1]
                name = " ".join(tokens[:-1])
                voices.append({"name": name, "lang": lang})
        return {"voices": voices}
    except Exception:
        return {"voices": []}


@app.post("/api/tts-test")
async def api_tts_test(request: Request):
    """Play a TTS sample with the given voice and speed."""
    body = await request.json()
    voice = body.get("voice", "Samantha")
    speed = body.get("speed", 190)
    text = body.get("text", f"Hi, I'm {voice}. How can I help you today?")
    try:
        subprocess.Popen(
            ["say", "-v", voice, "-r", str(speed), text],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        return {"status": "ok"}
    except Exception as e:
        return {"status": "error", "message": str(e)}


# ---------------------------------------------------------------------------
# Voice event stream
# ---------------------------------------------------------------------------

@app.post("/api/voice/event")
async def api_voice_event(request: Request):
    """Voice assistant pushes events here for the UI to display."""
    global voice_event_id
    body = await request.json()
    voice_event_id += 1
    body["id"] = voice_event_id
    voice_events.append(body)
    # Keep last 200 events
    if len(voice_events) > 200:
        del voice_events[:len(voice_events) - 200]
    return {"id": voice_event_id}


@app.get("/api/voice/events")
async def api_voice_events(after: int = 0):
    """UI polls for voice events after a given ID."""
    new = [e for e in voice_events if e["id"] > after]
    return {"events": new}


# ---------------------------------------------------------------------------
# Inline HTML page — single-file chat UI
# ---------------------------------------------------------------------------
HTML_PAGE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
<title>WhatsApp Assistant</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=DM+Sans:ital,wght@0,400;0,500;0,600;0,700;1,400&family=JetBrains+Mono:wght@400;500&display=swap" rel="stylesheet">
<style>
  :root {
    --bg-deep: #090f13;
    --bg-panel: #0d1418;
    --bg-header: #151e24;
    --bg-input: #1c2830;
    --bg-msg-in: #151e24;
    --bg-msg-out: #004a3f;
    --accent: #00c896;
    --accent-dim: rgba(0,200,150,0.12);
    --accent-glow: rgba(0,200,150,0.25);
    --text: #e4e9ec;
    --text-2: #7e919d;
    --text-3: rgba(228,233,236,0.4);
    --border: #1e2a32;
    --blue: #4fc3f7;
    --red: #ef5350;
    --yellow: #ffd54f;
    --separator: #1a242c;
    --radius: 10px;
    --toast-green: #00c896;
  }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body {
    font-family: 'DM Sans', -apple-system, BlinkMacSystemFont, sans-serif;
    background: var(--bg-deep);
    color: var(--text);
    height: 100vh; height: 100dvh;
    display: flex; flex-direction: column;
    overflow: hidden;
  }

  /* ===== HEADER ===== */
  .header {
    background: var(--bg-header);
    padding: 0 20px;
    display: flex; align-items: center; gap: 14px;
    height: 64px; flex-shrink: 0;
    border-bottom: 1px solid var(--border);
    z-index: 10;
  }
  .avatar {
    width: 42px; height: 42px;
    background: linear-gradient(135deg, var(--accent), #00897b);
    border-radius: 50%;
    display: flex; align-items: center; justify-content: center;
    flex-shrink: 0;
    box-shadow: 0 0 16px var(--accent-glow);
  }
  .avatar svg { width: 22px; height: 22px; fill: #fff; }
  .info { flex: 1; min-width: 0; }
  .info h2 { font-size: 15px; font-weight: 600; letter-spacing: -0.2px; }
  .status-row { display: flex; align-items: center; gap: 6px; }
  .status-dot {
    width: 7px; height: 7px; border-radius: 50%;
    background: var(--accent); flex-shrink: 0;
    transition: all 0.3s;
  }
  .status-dot.busy { background: var(--yellow); animation: pulse-dot 1.4s ease-in-out infinite; }
  @keyframes pulse-dot { 0%,100%{opacity:1} 50%{opacity:0.3} }
  .info p { font-size: 12px; color: var(--text-2); }
  .header-actions { display: flex; align-items: center; gap: 6px; flex-shrink: 0; }

  .bridge-pill {
    display: flex; align-items: center; gap: 6px;
    padding: 5px 12px; border-radius: 20px;
    background: rgba(255,255,255,0.04);
    border: 1px solid var(--border);
    font-size: 11px; font-weight: 500;
    color: var(--text-2); cursor: pointer;
    transition: all 0.2s; white-space: nowrap;
  }
  .bridge-pill:hover { background: rgba(255,255,255,0.08); }
  .bridge-pill .bdot {
    width: 7px; height: 7px; border-radius: 50%; flex-shrink: 0;
    transition: background 0.3s;
  }
  .bdot.connected { background: var(--accent); box-shadow: 0 0 6px var(--accent-glow); }
  .bdot.qr_pending { background: var(--yellow); animation: pulse-dot 1.4s ease-in-out infinite; }
  .bdot.disconnected,.bdot.bridge_offline { background: var(--red); }

  .view-toggle {
    display: flex; background: var(--bg-input);
    border-radius: 20px; overflow: hidden;
    border: 1px solid var(--border);
  }
  .view-toggle button {
    background: none; border: none; color: var(--text-2);
    padding: 5px 14px; font-size: 11px; font-weight: 600;
    cursor: pointer; transition: all 0.2s;
    letter-spacing: 0.4px; text-transform: uppercase;
    font-family: 'DM Sans', sans-serif;
  }
  .view-toggle button.active { background: var(--accent); color: var(--bg-deep); }
  .view-toggle button:hover:not(.active) { color: var(--text); }

  .hbtn {
    width: 36px; height: 36px; background: none; border: none;
    color: var(--text-2); border-radius: 50%; cursor: pointer;
    display: flex; align-items: center; justify-content: center;
    transition: all 0.2s;
  }
  .hbtn:hover { background: rgba(255,255,255,0.06); color: var(--text); }
  .hbtn svg { width: 18px; height: 18px; fill: currentColor; }

  /* ===== SETTINGS MODAL ===== */
  .settings-overlay { display:none; position:fixed; inset:0; background:rgba(0,0,0,0.75); z-index:100; align-items:center; justify-content:center; }
  .settings-overlay.active { display: flex; }
  .settings-modal {
    background: var(--bg-panel); border-radius: 16px;
    padding: 28px 32px; max-width: 480px; width: 90%;
    box-shadow: 0 16px 64px rgba(0,0,0,0.5);
    border: 1px solid var(--border); max-height: 85vh; overflow-y: auto;
  }
  .settings-modal h3 {
    font-size: 18px; font-weight: 600; margin-bottom: 20px;
    display: flex; align-items: center; gap: 10px;
  }
  .settings-modal h3 svg { width: 20px; height: 20px; fill: var(--accent); }
  .s-group { margin-bottom: 18px; }
  .s-group label {
    display: block; font-size: 12px; font-weight: 600;
    color: var(--text-2); text-transform: uppercase;
    letter-spacing: 0.5px; margin-bottom: 6px;
  }
  .s-group input[type="text"], .s-group select {
    width: 100%; background: var(--bg-input); border: 1px solid var(--border);
    border-radius: 8px; padding: 10px 12px; color: var(--text);
    font-size: 14px; font-family: 'DM Sans', sans-serif; outline: none;
    transition: border-color 0.2s;
  }
  .s-group input[type="text"]:focus, .s-group select:focus { border-color: rgba(0,200,150,0.4); }
  .s-group select { appearance: none; cursor: pointer; }
  .s-range-row { display: flex; align-items: center; gap: 12px; }
  .s-range-row input[type="range"] {
    flex: 1; accent-color: var(--accent); height: 6px; cursor: pointer;
  }
  .s-range-val {
    min-width: 48px; text-align: center; font-size: 13px;
    font-weight: 600; color: var(--accent);
    font-family: 'JetBrains Mono', monospace;
  }
  .s-toggle-row {
    display: flex; align-items: center; justify-content: space-between;
    padding: 8px 0;
  }
  .s-toggle-row span { font-size: 14px; }
  .s-toggle {
    width: 44px; height: 24px; background: var(--bg-input);
    border-radius: 12px; border: 1px solid var(--border);
    position: relative; cursor: pointer; transition: all 0.2s;
  }
  .s-toggle.on { background: var(--accent); border-color: var(--accent); }
  .s-toggle::after {
    content: ''; position: absolute; top: 2px; left: 2px;
    width: 18px; height: 18px; background: #fff; border-radius: 50%;
    transition: transform 0.2s;
  }
  .s-toggle.on::after { transform: translateX(20px); }
  .s-voice-row { display: flex; gap: 8px; align-items: stretch; }
  .s-voice-row select { flex: 1; }
  .s-test-btn {
    background: var(--accent-dim); border: 1px solid var(--accent);
    color: var(--accent); border-radius: 8px; padding: 0 14px;
    font-size: 12px; font-weight: 600; cursor: pointer;
    font-family: 'DM Sans', sans-serif; transition: all 0.2s;
    white-space: nowrap; display: flex; align-items: center; gap: 5px;
  }
  .s-test-btn:hover { background: var(--accent); color: var(--bg-deep); }
  .s-test-btn svg { width: 14px; height: 14px; fill: currentColor; }
  .s-actions { display: flex; gap: 10px; justify-content: flex-end; margin-top: 20px; }
  .s-actions button {
    padding: 10px 24px; border-radius: 24px; font-size: 13px;
    font-weight: 500; font-family: 'DM Sans', sans-serif;
    cursor: pointer; transition: all 0.2s; border: none;
  }
  .s-btn-cancel { background: var(--bg-input); color: var(--text); border: 1px solid var(--border) !important; }
  .s-btn-cancel:hover { background: var(--bg-header); }
  .s-btn-save { background: var(--accent); color: var(--bg-deep); font-weight: 600; }
  .s-btn-save:hover { background: #00e6aa; }

  /* ===== MESSAGES AREA ===== */
  .messages-wrapper { flex: 1; overflow: hidden; position: relative; background: var(--bg-deep); }
  .messages-wrapper::before {
    content: ''; position: absolute; inset: 0; pointer-events: none; opacity: 0.4;
    background-image: radial-gradient(circle at 20% 50%, rgba(0,200,150,0.03) 0%, transparent 50%),
                      radial-gradient(circle at 80% 20%, rgba(79,195,247,0.02) 0%, transparent 50%);
  }
  .messages {
    height: 100%; overflow-y: auto; overflow-x: hidden;
    padding: 20px 56px 12px;
    display: flex; flex-direction: column; gap: 3px;
    position: relative; z-index: 1; scroll-behavior: smooth;
  }
  .messages::-webkit-scrollbar { width: 5px; }
  .messages::-webkit-scrollbar-thumb { background: rgba(255,255,255,0.12); border-radius: 3px; }

  .date-separator { text-align: center; padding: 10px 0 6px; user-select: none; }
  .date-separator span {
    background: var(--bg-header); color: var(--text-2);
    font-size: 11px; font-weight: 500; padding: 5px 14px;
    border-radius: 8px; letter-spacing: 0.3px;
    border: 1px solid var(--border);
  }

  /* ===== MESSAGE BUBBLES ===== */
  .msg {
    max-width: 62%; padding: 8px 10px 8px 12px;
    border-radius: var(--radius); font-size: 14px; line-height: 1.5;
    position: relative; word-wrap: break-word; overflow-wrap: break-word;
    animation: msg-in 0.25s ease-out;
  }
  @keyframes msg-in { from { opacity:0; transform: translateY(8px); } to { opacity:1; transform: none; } }
  .msg.user {
    background: var(--bg-msg-out); align-self: flex-end;
    border-bottom-right-radius: 3px;
    box-shadow: 0 1px 2px rgba(0,0,0,0.2);
  }
  .msg.assistant {
    background: var(--bg-msg-in); align-self: flex-start;
    border-bottom-left-radius: 3px;
    border: 1px solid var(--border);
  }
  .msg .meta-row {
    display: flex; justify-content: flex-end; align-items: center;
    gap: 4px; margin-top: 2px; float: right; margin-left: 12px;
    position: relative; top: 4px;
  }
  .msg .time { font-size: 10px; color: var(--text-3); }
  .msg.user .check-marks { display: inline-flex; margin-left: 2px; }
  .msg.user .check-marks svg { width: 16px; height: 11px; fill: var(--blue); }
  .voice-badge {
    display: inline-flex; align-items: center; gap: 3px;
    font-size: 10px; color: var(--accent); opacity: 0.7;
  }
  .voice-badge svg { width: 12px; height: 12px; fill: var(--accent); }

  /* ===== TYPING ===== */
  .typing-bubble {
    background: var(--bg-msg-in); align-self: flex-start;
    border-radius: var(--radius); border-bottom-left-radius: 3px;
    padding: 12px 18px; border: 1px solid var(--border);
    display: flex; flex-direction: column; gap: 8px; max-width: 280px;
    animation: msg-in 0.25s ease-out;
  }
  .typing-dots { display: flex; align-items: center; gap: 5px; height: 20px; }
  .typing-dots span {
    width: 7px; height: 7px; background: var(--text-2);
    border-radius: 50%; animation: tbounce 1.4s ease-in-out infinite;
  }
  .typing-dots span:nth-child(2) { animation-delay: 0.2s; }
  .typing-dots span:nth-child(3) { animation-delay: 0.4s; }
  @keyframes tbounce { 0%,60%,100%{transform:translateY(0);opacity:0.35} 30%{transform:translateY(-5px);opacity:1} }
  .typing-status {
    font-size: 11px; color: var(--accent); display: flex;
    align-items: center; gap: 6px; font-weight: 500;
  }
  .typing-status .spinner {
    width: 12px; height: 12px;
    border: 2px solid var(--accent); border-top-color: transparent;
    border-radius: 50%; animation: spin 0.7s linear infinite;
  }
  @keyframes spin { to { transform: rotate(360deg); } }

  /* ===== DEV TOOLS ===== */
  .dev-tool-panel { margin-top: 8px; border-top: 1px solid var(--separator); padding-top: 8px; }
  .dev-tool-item { margin-bottom: 6px; border-radius: 8px; overflow: hidden; border: 1px solid var(--separator); }
  .dev-tool-header {
    display: flex; align-items: center; gap: 6px;
    padding: 6px 10px; background: var(--accent-dim);
    cursor: pointer; font-size: 12px; color: var(--accent);
    user-select: none; transition: background 0.15s; font-weight: 500;
  }
  .dev-tool-header:hover { background: var(--accent-glow); }
  .dev-tool-header .tbadge {
    background: var(--accent); color: var(--bg-deep);
    font-size: 9px; font-weight: 700; padding: 2px 6px;
    border-radius: 4px; text-transform: uppercase;
    letter-spacing: 0.5px; font-family: 'JetBrains Mono', monospace;
  }
  .dev-tool-header .tname { font-weight: 600; }
  .dev-tool-header .tarrow { margin-left: auto; transition: transform 0.2s; font-size: 10px; }
  .dev-tool-header.open .tarrow { transform: rotate(90deg); }
  .dev-tool-body { display: none; padding: 8px; background: rgba(0,0,0,0.25); }
  .dev-tool-body.open { display: block; }
  .dev-tool-section { margin-bottom: 6px; }
  .dev-tool-section:last-child { margin-bottom: 0; }
  .dev-tool-label {
    font-size: 10px; font-weight: 600; color: var(--text-2);
    text-transform: uppercase; letter-spacing: 0.5px; margin-bottom: 4px;
  }
  .dev-tool-section pre {
    font-size: 11px; line-height: 1.4; color: var(--text);
    white-space: pre-wrap; word-break: break-all;
    max-height: 180px; overflow-y: auto;
    background: rgba(0,0,0,0.2); padding: 8px 10px; border-radius: 6px;
    font-family: 'JetBrains Mono', monospace;
  }
  .dev-tool-summary {
    display: flex; align-items: center; gap: 6px;
    font-size: 11px; color: var(--text-2); padding: 4px 0 2px;
  }
  .dev-tool-summary .tcount {
    background: var(--accent-dim); color: var(--accent);
    padding: 2px 8px; border-radius: 10px; font-size: 11px; font-weight: 600;
  }

  /* ===== INPUT ===== */
  .input-area {
    background: var(--bg-header); padding: 10px 20px;
    display: flex; gap: 10px; align-items: flex-end;
    border-top: 1px solid var(--border); z-index: 10;
  }
  .input-wrapper {
    flex: 1; display: flex; align-items: flex-end;
    background: var(--bg-input); border-radius: 12px;
    padding: 0 14px; min-height: 44px;
    border: 1px solid var(--border);
    transition: border-color 0.2s;
  }
  .input-wrapper:focus-within { border-color: rgba(0,200,150,0.3); }
  .input-area textarea {
    flex: 1; background: none; border: none; outline: none;
    color: var(--text); padding: 11px 0; font-size: 14px;
    font-family: 'DM Sans', sans-serif; resize: none;
    max-height: 100px; line-height: 1.4;
  }
  .input-area textarea::placeholder { color: var(--text-2); }
  .send-btn {
    background: var(--accent); border: none; color: var(--bg-deep);
    width: 44px; height: 44px; border-radius: 50%;
    cursor: pointer; display: flex; align-items: center; justify-content: center;
    transition: all 0.2s; flex-shrink: 0; font-weight: 700;
  }
  .send-btn:hover { background: #00e6aa; transform: scale(1.05); box-shadow: 0 0 20px var(--accent-glow); }
  .send-btn:disabled { background: var(--bg-input); cursor: not-allowed; transform: none; box-shadow: none; }
  .send-btn svg { width: 20px; height: 20px; fill: currentColor; }

  /* ===== WELCOME ===== */
  .welcome { text-align: center; color: var(--text-2); margin: auto; max-width: 480px; padding: 24px; }
  .welcome-icon {
    width: 80px; height: 80px;
    background: linear-gradient(135deg, var(--accent-dim), rgba(79,195,247,0.08));
    border-radius: 24px; display: flex; align-items: center; justify-content: center;
    margin: 0 auto 24px; box-shadow: 0 0 40px var(--accent-glow);
  }
  .welcome-icon svg { width: 40px; height: 40px; fill: var(--accent); }
  .welcome h3 { color: var(--text); font-size: 22px; font-weight: 600; margin-bottom: 8px; letter-spacing: -0.3px; }
  .welcome p { font-size: 14px; line-height: 1.6; margin-bottom: 28px; }
  .feature-badge {
    display: inline-flex; align-items: center; gap: 6px;
    background: var(--accent-dim); border-radius: 20px;
    padding: 6px 14px; font-size: 12px; color: var(--accent);
    margin-bottom: 28px; font-weight: 500;
  }
  .feature-badge svg { width: 14px; height: 14px; fill: var(--accent); }
  .examples { text-align: left; display: grid; grid-template-columns: 1fr 1fr; gap: 8px; }
  .example-item {
    background: var(--bg-msg-in); padding: 14px 16px; border-radius: var(--radius);
    cursor: pointer; transition: all 0.2s; display: flex;
    align-items: center; gap: 12px; font-size: 13px;
    color: var(--text); line-height: 1.4;
    border: 1px solid var(--border);
  }
  .example-item:hover { background: var(--bg-input); border-color: rgba(0,200,150,0.2); transform: translateY(-1px); }
  .example-icon {
    width: 36px; height: 36px;
    background: var(--accent-dim); border-radius: 10px;
    display: flex; align-items: center; justify-content: center;
    flex-shrink: 0; font-size: 16px;
  }

  /* ===== CONTENT FORMATTING ===== */
  .msg-content { white-space: pre-wrap; }
  .msg-content p { margin: 0.3em 0; }
  .msg-content p:first-child { margin-top: 0; }
  .msg-content p:last-child { margin-bottom: 0; }
  .msg-content code {
    background: rgba(255,255,255,0.07); padding: 2px 6px;
    border-radius: 4px; font-size: 12.5px;
    font-family: 'JetBrains Mono', monospace;
  }
  .msg-content pre { background: rgba(0,0,0,0.3); border-radius: 8px; margin: 8px 0; overflow-x: auto; }
  .msg-content pre code { display: block; padding: 12px 14px; background: none; font-size: 12px; line-height: 1.5; }
  .msg-content strong { color: #fff; font-weight: 600; }
  .msg-content em { color: rgba(228,233,236,0.8); }
  .msg-content ul, .msg-content ol { padding-left: 20px; margin: 4px 0; }
  .msg-content li { margin: 2px 0; }
  .msg-content a { color: var(--blue); text-decoration: none; }
  .msg-content a:hover { text-decoration: underline; }
  .msg-content table { border-collapse: collapse; margin: 8px 0; width: 100%; font-size: 13px; }
  .msg-content th, .msg-content td { border: 1px solid var(--border); padding: 6px 10px; text-align: left; }
  .msg-content th { background: var(--accent-dim); font-weight: 600; }

  /* ===== TOAST NOTIFICATIONS ===== */
  .toast-container {
    position: fixed; top: 76px; right: 20px;
    z-index: 90; display: flex; flex-direction: column; gap: 8px;
    pointer-events: none;
  }
  .toast {
    background: var(--bg-header); border: 1px solid var(--border);
    border-left: 3px solid var(--accent); border-radius: 10px;
    padding: 12px 16px; min-width: 280px; max-width: 360px;
    box-shadow: 0 8px 32px rgba(0,0,0,0.4);
    animation: toast-in 0.35s cubic-bezier(0.16,1,0.3,1);
    pointer-events: auto; cursor: pointer;
    transition: opacity 0.3s, transform 0.3s;
  }
  .toast.fade-out { opacity: 0; transform: translateX(20px); }
  @keyframes toast-in { from { opacity:0; transform: translateX(40px); } to { opacity:1; transform: none; } }
  .toast-sender { font-size: 13px; font-weight: 600; color: var(--accent); margin-bottom: 2px; }
  .toast-text { font-size: 12px; color: var(--text-2); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .toast-time { font-size: 10px; color: var(--text-3); margin-top: 4px; }

  /* ===== QR MODAL ===== */
  .overlay { display:none; position:fixed; inset:0; background:rgba(0,0,0,0.75); z-index:100; align-items:center; justify-content:center; }
  .overlay.active { display: flex; }
  .modal {
    background: var(--bg-panel); border-radius: 16px;
    padding: 32px; max-width: 400px; width: 90%;
    text-align: center; box-shadow: 0 16px 64px rgba(0,0,0,0.5);
    border: 1px solid var(--border);
  }
  .modal h3 { font-size: 18px; font-weight: 600; margin-bottom: 6px; }
  .modal p { font-size: 13px; color: var(--text-2); margin-bottom: 20px; line-height: 1.5; }
  .qr-box {
    background: #fff; border-radius: 12px; padding: 16px;
    display: inline-flex; align-items: center; justify-content: center;
    margin-bottom: 20px; min-height: 280px; min-width: 280px;
  }
  .qr-box img { width: 248px; height: 248px; }
  .qr-box .qr-msg { color: #666; font-size: 13px; }
  .modal-btn {
    background: var(--bg-input); border: 1px solid var(--border);
    color: var(--text); padding: 10px 28px; border-radius: 24px;
    cursor: pointer; font-size: 13px; font-weight: 500;
    font-family: 'DM Sans', sans-serif; transition: all 0.2s;
  }
  .modal-btn:hover { background: var(--bg-header); }

  /* ===== RESPONSIVE ===== */
  @media (max-width: 768px) {
    .messages { padding: 12px 14px 8px; }
    .msg { max-width: 88%; }
    .examples { grid-template-columns: 1fr; }
    .header { padding: 0 12px; }
    .view-toggle button { padding: 5px 10px; font-size: 10px; }
  }
</style>
</head>
<body>
<div class="header">
  <div class="avatar">
    <svg viewBox="0 0 24 24"><path d="M17.498 14.382c-.301-.15-1.767-.867-2.04-.966-.273-.101-.473-.15-.673.15-.197.295-.771.964-.944 1.162-.175.195-.349.21-.646.075-.3-.15-1.263-.465-2.403-1.485-.888-.795-1.484-1.77-1.66-2.07-.174-.3-.019-.465.13-.615.136-.135.301-.345.451-.523.146-.181.194-.301.297-.496.1-.21.049-.375-.025-.524-.075-.15-.672-1.62-.922-2.206-.24-.584-.487-.51-.672-.51-.172-.015-.371-.015-.571-.015-.2 0-.523.074-.797.359-.273.3-1.045 1.02-1.045 2.475s1.07 2.865 1.219 3.075c.149.195 2.105 3.195 5.1 4.485.714.3 1.27.48 1.704.629.714.227 1.365.195 1.88.121.574-.091 1.767-.721 2.016-1.426.255-.691.255-1.29.18-1.425-.074-.135-.27-.21-.57-.345z"/><path d="M20.52 3.449C12.831-3.984.106 1.407.101 11.893c0 2.096.549 4.14 1.595 5.945L0 24l6.335-1.652c7.905 4.27 17.661-1.4 17.665-10.449 0-2.8-1.092-5.434-3.08-7.406l-.4-.044zm-8.52 18.2c-1.792 0-3.546-.48-5.076-1.385l-.363-.216-3.776.99 1.008-3.676-.235-.374A9.846 9.846 0 012.1 11.893C2.1 6.443 6.543 2.001 12 2.001c2.647 0 5.133 1.03 7.002 2.899a9.825 9.825 0 012.898 6.993c-.003 5.45-4.437 9.756-9.9 9.756z"/></svg>
  </div>
  <div class="info">
    <h2>WhatsApp Assistant</h2>
    <div class="status-row">
      <div class="status-dot" id="status-dot"></div>
      <p id="status-text">online</p>
    </div>
  </div>
  <div class="header-actions">
    <div class="bridge-pill" id="bridge-pill" onclick="onBridgeClick()" title="Bridge connection">
      <div class="bdot disconnected" id="bdot"></div>
      <span id="blabel">Bridge offline</span>
    </div>
    <div class="view-toggle">
      <button class="active" data-view="user" onclick="setView('user')">Chat</button>
      <button data-view="dev" onclick="setView('dev')">Dev</button>
    </div>
    <button class="hbtn" onclick="openSettings()" title="Voice settings">
      <svg viewBox="0 0 24 24"><path d="M19.14 12.94c.04-.3.06-.61.06-.94 0-.32-.02-.64-.07-.94l2.03-1.58a.49.49 0 00.12-.61l-1.92-3.32a.488.488 0 00-.59-.22l-2.39.96c-.5-.38-1.03-.7-1.62-.94l-.36-2.54a.484.484 0 00-.48-.41h-3.84c-.24 0-.43.17-.47.41l-.36 2.54c-.59.24-1.13.57-1.62.94l-2.39-.96c-.22-.08-.47 0-.59.22L2.74 8.87c-.12.21-.08.47.12.61l2.03 1.58c-.05.3-.07.62-.07.94s.02.64.07.94l-2.03 1.58a.49.49 0 00-.12.61l1.92 3.32c.12.22.37.29.59.22l2.39-.96c.5.38 1.03.7 1.62.94l.36 2.54c.05.24.24.41.48.41h3.84c.24 0 .44-.17.47-.41l.36-2.54c.59-.24 1.13-.56 1.62-.94l2.39.96c.22.08.47 0 .59-.22l1.92-3.32c.12-.22.07-.47-.12-.61l-2.01-1.58zM12 15.6A3.6 3.6 0 1115.6 12 3.611 3.611 0 0112 15.6z"/></svg>
    </button>
    <button class="hbtn" onclick="fetch('/api/refresh',{method:'POST'})" title="Refresh data">
      <svg viewBox="0 0 24 24"><path d="M17.65 6.35A7.958 7.958 0 0012 4c-4.42 0-7.99 3.58-7.99 8s3.57 8 7.99 8c3.73 0 6.84-2.55 7.73-6h-2.08A5.99 5.99 0 0112 18c-3.31 0-6-2.69-6-6s2.69-6 6-6c1.66 0 3.14.69 4.22 1.78L13 11h7V4l-2.35 2.35z"/></svg>
    </button>
    <button class="hbtn" onclick="clearChat()" title="New chat">
      <svg viewBox="0 0 24 24"><path d="M6 19c0 1.1.9 2 2 2h8c1.1 0 2-.9 2-2V7H6v12zM19 4h-3.5l-1-1h-5l-1 1H5v2h14V4z"/></svg>
    </button>
  </div>
</div>

<div class="messages-wrapper">
  <div class="messages" id="messages">
    <div class="welcome" id="welcome-screen">
      <div class="welcome-icon">
        <svg viewBox="0 0 24 24"><path d="M17.498 14.382c-.301-.15-1.767-.867-2.04-.966-.273-.101-.473-.15-.673.15-.197.295-.771.964-.944 1.162-.175.195-.349.21-.646.075-.3-.15-1.263-.465-2.403-1.485-.888-.795-1.484-1.77-1.66-2.07-.174-.3-.019-.465.13-.615.136-.135.301-.345.451-.523.146-.181.194-.301.297-.496.1-.21.049-.375-.025-.524-.075-.15-.672-1.62-.922-2.206-.24-.584-.487-.51-.672-.51-.172-.015-.371-.015-.571-.015-.2 0-.523.074-.797.359-.273.3-1.045 1.02-1.045 2.475s1.07 2.865 1.219 3.075c.149.195 2.105 3.195 5.1 4.485.714.3 1.27.48 1.704.629.714.227 1.365.195 1.88.121.574-.091 1.767-.721 2.016-1.426.255-.691.255-1.29.18-1.425-.074-.135-.27-.21-.57-.345z"/><path d="M20.52 3.449C12.831-3.984.106 1.407.101 11.893c0 2.096.549 4.14 1.595 5.945L0 24l6.335-1.652c7.905 4.27 17.661-1.4 17.665-10.449 0-2.8-1.092-5.434-3.08-7.406l-.4-.044zm-8.52 18.2c-1.792 0-3.546-.48-5.076-1.385l-.363-.216-3.776.99 1.008-3.676-.235-.374A9.846 9.846 0 012.1 11.893C2.1 6.443 6.543 2.001 12 2.001c2.647 0 5.133 1.03 7.002 2.899a9.825 9.825 0 012.898 6.993c-.003 5.45-4.437 9.756-9.9 9.756z"/></svg>
      </div>
      <h3>WhatsApp Assistant</h3>
      <div class="feature-badge">
        <svg viewBox="0 0 24 24"><path d="M18 8h-1V6c0-2.76-2.24-5-5-5S7 3.24 7 6v2H6c-1.1 0-2 .9-2 2v10c0 1.1.9 2 2 2h12c1.1 0 2-.9 2-2V10c0-1.1-.9-2-2-2zm-6 9c-1.1 0-2-.9-2-2s.9-2 2-2 2 .9 2 2-.9 2-2 2zm3.1-9H8.9V6c0-1.71 1.39-3.1 3.1-3.1s3.1 1.39 3.1 3.1v2z"/></svg>
        Read &middot; Send &middot; Schedule &middot; Summarize
      </div>
      <p>Search contacts, read conversations, send messages, schedule deliveries, and get AI-powered summaries. Everything stays on your device.</p>
      <div class="examples">
        <div class="example-item" onclick="askExample(this.querySelector('.et').textContent)">
          <div class="example-icon">&#128172;</div>
          <span class="et">Catch me up on unread messages</span>
        </div>
        <div class="example-item" onclick="askExample(this.querySelector('.et').textContent)">
          <div class="example-icon">&#128269;</div>
          <span class="et">Find contact Priya and show our chat</span>
        </div>
        <div class="example-item" onclick="askExample(this.querySelector('.et').textContent)">
          <div class="example-icon">&#128228;</div>
          <span class="et">Send a message to Krishna saying hi</span>
        </div>
        <div class="example-item" onclick="askExample(this.querySelector('.et').textContent)">
          <div class="example-icon">&#9200;</div>
          <span class="et">Schedule a birthday wish tomorrow at midnight</span>
        </div>
      </div>
    </div>
  </div>
</div>

<div class="toast-container" id="toast-container"></div>

<div class="overlay" id="qr-overlay" onclick="closeQR(event)">
  <div class="modal">
    <h3>Connect WhatsApp</h3>
    <p>Scan with your phone: WhatsApp &gt; Settings &gt; Linked Devices &gt; Link a Device</p>
    <div class="qr-box" id="qr-box"><span class="qr-msg">Loading...</span></div>
    <br><button class="modal-btn" onclick="closeQR()">Close</button>
  </div>
</div>

<div class="settings-overlay" id="settings-overlay" onclick="closeSettings(event)">
  <div class="settings-modal" onclick="event.stopPropagation()">
    <h3>
      <svg viewBox="0 0 24 24"><path d="M12 3a1 1 0 00-1 1v.28c-1.18.28-2.24.84-3.1 1.62l-.24-.14a1 1 0 00-1.37.37l-1 1.73a1 1 0 00.37 1.37l.24.14A7.06 7.06 0 005.5 10H5a1 1 0 00-1 1v2a1 1 0 001 1h.5c.1.7.3 1.37.6 1.99l-.25.14a1 1 0 00-.36 1.37l1 1.73a1 1 0 001.37.36l.24-.14c.86.78 1.92 1.34 3.1 1.62V21a1 1 0 001 1h2a1 1 0 001-1v-.28c1.18-.28 2.24-.84 3.1-1.62l.24.14a1 1 0 001.37-.36l1-1.73a1 1 0 00-.37-1.37l-.24-.14c.3-.62.5-1.29.6-1.99h.5a1 1 0 001-1v-2a1 1 0 00-1-1h-.5a7.06 7.06 0 00-.6-1.99l.25-.14a1 1 0 00.36-1.37l-1-1.73a1 1 0 00-1.37-.36l-.24.14A6.94 6.94 0 0013 4.28V4a1 1 0 00-1-1zm0 6a3 3 0 110 6 3 3 0 010-6z"/></svg>
      Voice Settings
    </h3>
    <div class="s-group">
      <label>Wake Word</label>
      <input type="text" id="s-wake-word" placeholder="hey tanu">
    </div>
    <div class="s-group">
      <label>STT Engine</label>
      <select id="s-stt-engine">
        <option value="google">Google Web Speech</option>
        <option value="apple">Apple On-Device</option>
        <option value="whisper">Whisper (Local)</option>
      </select>
    </div>
    <div class="s-group">
      <label>TTS Voice</label>
      <div class="s-voice-row">
        <select id="s-tts-voice"><option value="Samantha">Samantha</option></select>
        <button class="s-test-btn" onclick="testTTS()" title="Test selected voice">
          <svg viewBox="0 0 24 24"><path d="M3 9v6h4l5 5V4L7 9H3zm13.5 3A4.5 4.5 0 0014 8.5v7a4.47 4.47 0 002.5-3.5zM14 3.23v2.06a6.51 6.51 0 010 13.42v2.06A8.5 8.5 0 0014 3.23z"/></svg>
          Test
        </button>
      </div>
    </div>
    <div class="s-group">
      <label>TTS Speed</label>
      <div class="s-range-row">
        <input type="range" id="s-tts-speed" min="100" max="300" step="10" value="190">
        <span class="s-range-val" id="s-speed-val">190</span>
      </div>
    </div>
    <div class="s-group">
      <label>Follow-up Timeout</label>
      <div class="s-range-row">
        <input type="range" id="s-follow-up" min="1" max="10" step="1" value="3">
        <span class="s-range-val" id="s-follow-val">3s</span>
      </div>
    </div>
    <div class="s-group">
      <div class="s-toggle-row">
        <span>Auto Listen</span>
        <div class="s-toggle on" id="s-auto-listen" onclick="this.classList.toggle('on')"></div>
      </div>
    </div>
    <div class="s-group">
      <div class="s-toggle-row">
        <span>Sound Feedback</span>
        <div class="s-toggle on" id="s-sound-feedback" onclick="this.classList.toggle('on')"></div>
      </div>
    </div>
    <div class="s-actions">
      <button class="s-btn-cancel" onclick="closeSettings()">Cancel</button>
      <button class="s-btn-save" onclick="saveSettings()">Save</button>
    </div>
  </div>
</div>

<div class="input-area">
  <div class="input-wrapper">
    <textarea id="input" rows="1" placeholder="Message WhatsApp Assistant..." autofocus></textarea>
  </div>
  <button class="send-btn" id="send-btn" onclick="sendMessage()">
    <svg viewBox="0 0 24 24"><path d="M2.01 21L23 12 2.01 3 2 10l15 2-15 2z"/></svg>
  </button>
</div>

<script>
let conversationId = null, sending = false, currentView = 'user';
const M = document.getElementById('messages');
const I = document.getElementById('input');
const SB = document.getElementById('send-btn');
const ST = document.getElementById('status-text');
const SD = document.getElementById('status-dot');

const phrases = {
  c:['Connecting...','Syncing...','Dialing in...'],
  s:['Scrolling through chats...','Browsing contacts...','Scanning conversations...','Sifting through archives...','Combing through chats...','Rummaging through messages...'],
  p:['Decrypting insights...','Piecing together the story...','Connecting the dots...','Reading between the lines...','Cross-referencing chats...','Sorting signal from noise...'],
  f:['Composing reply...','Drafting response...','Wrapping things up...','Final touches...','Almost there...']
};
let phase='c', si=null;
const rp = p => { const a=phrases[p]||phrases.c; return a[Math.floor(Math.random()*a.length)]; };
function startSC(){phase='c';usd(rp('c'));let t=0;si=setInterval(()=>{t++;phase=t<2?'c':t<5?'s':t<10?'p':'f';usd(rp(phase));},2500);}
function stopSC(){clearInterval(si);si=null;}
function usd(t){ST.textContent=t;SD.classList.add('busy');}
function setON(){ST.textContent='online';SD.classList.remove('busy');}

function setView(v){
  currentView=v;
  document.querySelectorAll('.view-toggle button').forEach(b=>b.classList.toggle('active',b.dataset.view===v));
  document.querySelectorAll('.dev-tool-panel').forEach(el=>el.style.display=v==='dev'?'block':'none');
}

I.addEventListener('keydown',e=>{if(e.key==='Enter'&&!e.shiftKey&&!sending){e.preventDefault();sendMessage();}});
I.addEventListener('input',()=>{I.style.height='auto';I.style.height=Math.min(I.scrollHeight,100)+'px';});
function askExample(t){I.value=t;I.style.height='auto';I.style.height=Math.min(I.scrollHeight,100)+'px';sendMessage();}

function addDS(){
  if(!M.querySelector('.date-separator')){
    const s=document.createElement('div');s.className='date-separator';
    s.innerHTML='<span>TODAY</span>';M.appendChild(s);
  }
}
function addMsg(role,content,tc,tr){
  const w=document.getElementById('welcome-screen');if(w)w.remove();addDS();
  const d=document.createElement('div');d.className=`msg ${role}`;
  const c=document.createElement('div');c.className='msg-content';c.innerHTML=renderMD(content);d.appendChild(c);
  if(role==='assistant'&&tc&&tc.length>0){
    const tp=document.createElement('div');tp.className='dev-tool-panel';
    tp.style.display=currentView==='dev'?'block':'none';
    const sm=document.createElement('div');sm.className='dev-tool-summary';
    sm.innerHTML=`<span class="tcount">${tc.length}</span> tool${tc.length>1?'s':''} used`;tp.appendChild(sm);
    tc.forEach((t,i)=>{
      const it=document.createElement('div');it.className='dev-tool-item';
      const h=document.createElement('div');h.className='dev-tool-header';
      h.innerHTML=`<span class="tbadge">TOOL</span><span class="tname">${esc(t.name)}</span><span class="tarrow">&#9654;</span>`;
      const b=document.createElement('div');b.className='dev-tool-body';
      h.onclick=()=>{h.classList.toggle('open');b.classList.toggle('open');};
      const as=document.createElement('div');as.className='dev-tool-section';
      as.innerHTML='<div class="dev-tool-label">Args</div>';
      const ap=document.createElement('pre');ap.textContent=JSON.stringify(t.arguments,null,2);as.appendChild(ap);b.appendChild(as);
      if(tr&&tr[i]){const rs=document.createElement('div');rs.className='dev-tool-section';
        rs.innerHTML='<div class="dev-tool-label">Result</div>';const rp=document.createElement('pre');
        try{rp.textContent=JSON.stringify(JSON.parse(tr[i]),null,2);}catch{rp.textContent=tr[i];}
        rs.appendChild(rp);b.appendChild(rs);}
      it.appendChild(h);it.appendChild(b);tp.appendChild(it);
    });
    d.appendChild(tp);
  }
  const m=document.createElement('div');m.className='meta-row';
  const ts=document.createElement('span');ts.className='time';
  ts.textContent=new Date().toLocaleTimeString([],{hour:'2-digit',minute:'2-digit'});m.appendChild(ts);
  if(role==='user'){const ck=document.createElement('span');ck.className='check-marks';
    ck.innerHTML='<svg viewBox="0 0 16 11"><path d="M11.071.653a.457.457 0 0 0-.304-.102.493.493 0 0 0-.381.178l-6.19 7.636-2.405-2.272a.463.463 0 0 0-.336-.146.47.47 0 0 0-.343.146l-.311.31a.445.445 0 0 0-.14.337c0 .136.047.25.14.343l2.996 2.996a.724.724 0 0 0 .501.203.697.697 0 0 0 .546-.266l6.646-8.417a.497.497 0 0 0 .108-.299.441.441 0 0 0-.14-.337l-.387-.31zm-2.26 7.636l.387.387a.732.732 0 0 0 .514.178.697.697 0 0 0 .546-.266l6.646-8.417a.497.497 0 0 0 .108-.299.441.441 0 0 0-.14-.337l-.387-.31a.457.457 0 0 0-.304-.102.493.493 0 0 0-.381.178l-6.19 7.636"/></svg>';
    m.appendChild(ck);}
  d.appendChild(m);M.appendChild(d);M.scrollTop=M.scrollHeight;return d;
}
function addTyping(){
  const w=document.getElementById('welcome-screen');if(w)w.remove();addDS();
  const d=document.createElement('div');d.className='typing-bubble';d.id='typing-ind';
  d.innerHTML='<div class="typing-dots"><span></span><span></span><span></span></div><div class="typing-status"><div class="spinner"></div><span id="ttext">Connecting...</span></div>';
  M.appendChild(d);M.scrollTop=M.scrollHeight;
}
function updTyping(t){const e=document.getElementById('ttext');if(e)e.textContent=t;M.scrollTop=M.scrollHeight;}
function rmTyping(){const e=document.getElementById('typing-ind');if(e)e.remove();}

async function sendMessage(){
  const t=I.value.trim();if(!t||sending)return;
  sending=true;SB.disabled=true;I.value='';I.style.height='auto';
  addMsg('user',t);addTyping();startSC();
  try{
    const r=await fetch('/api/chat/stream',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({message:t,conversation_id:conversationId})});
    const rd=r.body.getReader(),dec=new TextDecoder();let buf='',tc=[],tr=[],fc='',ti=0;
    while(true){const{done,value}=await rd.read();if(done)break;buf+=dec.decode(value,{stream:true});
      const lines=buf.split('\n');buf=lines.pop();
      for(const l of lines){if(!l.startsWith('data: '))continue;const d=l.slice(6).trim();if(d==='[DONE]')continue;
        try{const ev=JSON.parse(d);
          if(ev.type==='conv_id')conversationId=ev.conversation_id;
          else if(ev.type==='tool_call'){tc.push({name:ev.name,arguments:ev.arguments});ti=tc.length-1;
            updTyping(currentView==='user'?rp('s'):`Calling ${ev.name}...`);
            usd(currentView==='user'?rp('s'):`Using: ${ev.name}`);}
          else if(ev.type==='tool_result'){tr[ti]=ev.result||'';
            updTyping(currentView==='user'?rp('p'):`${ev.name} done`);}
          else if(ev.type==='message'){fc=ev.content;if(currentView==='user')updTyping(rp('f'));}
          else if(ev.type==='error')fc=`Error: ${ev.content}`;
        }catch{}}
    }
    rmTyping();stopSC();if(fc)addMsg('assistant',fc,tc,tr);
  }catch(err){rmTyping();stopSC();addMsg('assistant',`Something went wrong: ${err.message}`);}
  sending=false;SB.disabled=false;setON();I.focus();
}

const welcomeHTML = M.innerHTML;
function clearChat(){
  if(conversationId)fetch(`/api/conversation/${conversationId}`,{method:'DELETE'}).catch(()=>{});
  conversationId=null;M.innerHTML=welcomeHTML;
}

function renderMD(t){if(!t)return'';let s=t.replace(/<tts>[\s\S]*?<\/tts>/g,'').trim();let h=esc(s||t);
  h=h.replace(/```([\s\S]*?)```/g,'<pre><code>$1</code></pre>');
  h=h.replace(/`([^`]+)`/g,'<code>$1</code>');
  h=h.replace(/\*\*(.+?)\*\*/g,'<strong>$1</strong>');
  h=h.replace(/\*(.+?)\*/g,'<em>$1</em>');
  h=h.replace(/^### (.+)$/gm,'<br><strong>$1</strong>');
  h=h.replace(/^## (.+)$/gm,'<br><strong style="font-size:15px">$1</strong>');
  h=h.replace(/^# (.+)$/gm,'<br><strong style="font-size:16px">$1</strong>');
  h=h.replace(/^[-*] (.+)$/gm,'&bull; $1');
  h=h.replace(/^\d+\. (.+)$/gm,'&bull; $1');
  h=h.replace(/\n/g,'<br>');h=h.replace(/(<br>){3,}/g,'<br><br>');return h;}
function esc(t){const d=document.createElement('div');d.textContent=t;return d.innerHTML;}

// ===== BRIDGE STATUS =====
let bs='bridge_offline',bpi=null,qpi=null,qmo=false;
const BD=document.getElementById('bdot'),BL=document.getElementById('blabel');
const bLabels={connected:'Connected',qr_pending:'Scan QR',disconnected:'Disconnected',bridge_offline:'Bridge offline'};
async function pollBS(){try{const r=await fetch('/api/bridge/status');const d=await r.json();const s=d.status||'bridge_offline';if(s!==bs){bs=s;updBUI();}}catch{if(bs!=='bridge_offline'){bs='bridge_offline';updBUI();}}}
function updBUI(){BD.className='bdot '+bs;BL.textContent=bLabels[bs]||bs;if(bs==='connected'&&qmo)closeQR();}
function onBridgeClick(){if(bs==='connected')return;openQR();}
function openQR(){qmo=true;document.getElementById('qr-overlay').classList.add('active');fetchQR();qpi=setInterval(fetchQR,3000);}
function closeQR(e){if(e&&e.target!==document.getElementById('qr-overlay')&&!e.target.classList.contains('modal-btn'))return;qmo=false;document.getElementById('qr-overlay').classList.remove('active');clearInterval(qpi);qpi=null;}
async function fetchQR(){const c=document.getElementById('qr-box');try{const r=await fetch('/api/bridge/qr');const d=await r.json();if(d.qr)c.innerHTML=`<img src="${d.qr}" alt="QR">`;else if(d.status==='connected'){c.innerHTML='<span class="qr-msg" style="color:var(--accent)">Connected!</span>';setTimeout(closeQR,1500);}else c.innerHTML='<span class="qr-msg">Waiting for QR...</span>';}catch{c.innerHTML='<span class="qr-msg">Bridge not reachable</span>';}}
pollBS();bpi=setInterval(pollBS,10000);

// ===== INCOMING MESSAGE NOTIFICATIONS =====
let lastIncomingTs = Math.floor(Date.now()/1000);
const TC = document.getElementById('toast-container');
const seenIds = new Set();

async function pollIncoming(){
  if(bs!=='connected')return;
  try{
    const r=await fetch(`/api/bridge/incoming?since=${lastIncomingTs}`);
    const d=await r.json();
    if(d.messages&&d.messages.length>0){
      for(const msg of d.messages){
        if(seenIds.has(msg.id))continue;
        seenIds.add(msg.id);
        showToast(msg.pushName||msg.senderJid, msg.text||`[${msg.messageType}]`, msg.timestamp);
      }
      lastIncomingTs=d.latest_timestamp||lastIncomingTs;
    }
  }catch{}
}

function showToast(sender, text, ts){
  const t=document.createElement('div');t.className='toast';
  const time=new Date(ts*1000).toLocaleTimeString([],{hour:'2-digit',minute:'2-digit'});
  t.innerHTML=`<div class="toast-sender">${esc(sender)}</div><div class="toast-text">${esc(text)}</div><div class="toast-time">${time}</div>`;
  t.onclick=()=>{I.value=`What did ${sender} say?`;I.style.height='auto';I.style.height=Math.min(I.scrollHeight,100)+'px';I.focus();t.classList.add('fade-out');setTimeout(()=>t.remove(),300);};
  TC.appendChild(t);
  setTimeout(()=>{t.classList.add('fade-out');setTimeout(()=>t.remove(),300);},8000);
  // Keep max 3 toasts
  while(TC.children.length>3)TC.firstChild.remove();
}

setInterval(pollIncoming, 5000);

// ===== SETTINGS =====
const sSpeed = document.getElementById('s-tts-speed');
const sSpeedVal = document.getElementById('s-speed-val');
const sFollowUp = document.getElementById('s-follow-up');
const sFollowVal = document.getElementById('s-follow-val');
sSpeed.addEventListener('input', () => { sSpeedVal.textContent = sSpeed.value; });
sFollowUp.addEventListener('input', () => { sFollowVal.textContent = sFollowUp.value + 's'; });

async function openSettings(){
  document.getElementById('settings-overlay').classList.add('active');
  try {
    const r = await fetch('/api/settings');
    const s = await r.json();
    document.getElementById('s-wake-word').value = s.wake_word || 'hey tanu';
    document.getElementById('s-stt-engine').value = s.stt_engine || 'google';
    sSpeed.value = s.tts_speed || 190;
    sSpeedVal.textContent = sSpeed.value;
    sFollowUp.value = s.follow_up_timeout || 3;
    sFollowVal.textContent = sFollowUp.value + 's';
    const al = document.getElementById('s-auto-listen');
    al.classList.toggle('on', s.auto_listen !== false);
    const sf = document.getElementById('s-sound-feedback');
    sf.classList.toggle('on', s.sound_feedback !== false);
    // Load TTS voices
    const vr = await fetch('/api/tts-voices');
    const vd = await vr.json();
    const sel = document.getElementById('s-tts-voice');
    if (vd.voices && vd.voices.length > 0) {
      sel.innerHTML = '';
      for (const v of vd.voices) {
        const o = document.createElement('option');
        o.value = v.name;
        o.textContent = `${v.name} (${v.lang})`;
        sel.appendChild(o);
      }
    }
    sel.value = s.tts_voice || 'Samantha';
  } catch(e) { console.error('Failed to load settings', e); }
}

function closeSettings(e){
  if(e && e.target !== document.getElementById('settings-overlay')) return;
  document.getElementById('settings-overlay').classList.remove('active');
}

async function testTTS(){
  const voice = document.getElementById('s-tts-voice').value;
  const speed = parseInt(sSpeed.value);
  try {
    await fetch('/api/tts-test', {
      method:'POST', headers:{'Content-Type':'application/json'},
      body:JSON.stringify({voice, speed})
    });
  } catch(e) { console.error('TTS test failed', e); }
}

async function saveSettings(){
  const data = {
    wake_word: document.getElementById('s-wake-word').value.trim() || 'hey tanu',
    stt_engine: document.getElementById('s-stt-engine').value,
    tts_voice: document.getElementById('s-tts-voice').value,
    tts_speed: parseInt(sSpeed.value),
    follow_up_timeout: parseInt(sFollowUp.value),
    auto_listen: document.getElementById('s-auto-listen').classList.contains('on'),
    sound_feedback: document.getElementById('s-sound-feedback').classList.contains('on'),
  };
  try {
    await fetch('/api/settings', {method:'PUT', headers:{'Content-Type':'application/json'}, body:JSON.stringify(data)});
    document.getElementById('settings-overlay').classList.remove('active');
  } catch(e) { console.error('Failed to save settings', e); }
}

// ===== VOICE EVENT STREAM =====
let lastVoiceEventId = 0;
let voiceTypingEl = null;

function addVoiceMsg(role, content, tc) {
  // Reuse the existing addMsg which handles formatting, tool panels, dev view, etc.
  const el = addMsg(role, content, tc || [], []);
  // Add mic badge to the meta row to indicate this came from voice
  if (el) {
    const meta = el.querySelector('.meta-row');
    if (meta) {
      const vb = document.createElement('span'); vb.className = 'voice-badge';
      vb.innerHTML = '<svg viewBox="0 0 24 24"><path d="M12 14c1.66 0 3-1.34 3-3V5c0-1.66-1.34-3-3-3S9 3.34 9 5v6c0 1.66 1.34 3 3 3zm-1-9c0-.55.45-1 1-1s1 .45 1 1v6c0 .55-.45 1-1 1s-1-.45-1-1V5zm6 6c0 2.76-2.24 5-5 5s-5-2.24-5-5H5c0 3.53 2.61 6.43 6 6.92V21h2v-3.08c3.39-.49 6-3.39 6-6.92h-2z"/></svg>';
      meta.insertBefore(vb, meta.firstChild);
    }
  }
}

function showVoiceTyping() {
  if (voiceTypingEl) return;
  const w = document.getElementById('welcome-screen'); if(w) w.remove(); addDS();
  voiceTypingEl = document.createElement('div'); voiceTypingEl.className = 'typing-bubble'; voiceTypingEl.id = 'voice-typing';
  voiceTypingEl.innerHTML = '<div class="typing-dots"><span></span><span></span><span></span></div><div class="typing-status"><div class="spinner"></div><span id="vttext">Processing voice command...</span></div>';
  M.appendChild(voiceTypingEl); M.scrollTop = M.scrollHeight;
}

function updateVoiceTyping(text) {
  const el = document.getElementById('vttext');
  if (el) el.textContent = text;
  M.scrollTop = M.scrollHeight;
}

function removeVoiceTyping() {
  if (voiceTypingEl) { voiceTypingEl.remove(); voiceTypingEl = null; }
}

async function pollVoiceEvents() {
  try {
    const r = await fetch(`/api/voice/events?after=${lastVoiceEventId}`);
    const d = await r.json();
    if (!d.events || d.events.length === 0) return;
    for (const ev of d.events) {
      lastVoiceEventId = ev.id;
      if (ev.type === 'voice_user') {
        removeVoiceTyping();
        addVoiceMsg('user', ev.text);
        showVoiceTyping();
      } else if (ev.type === 'voice_tool_call') {
        updateVoiceTyping(`Calling ${ev.name}...`);
      } else if (ev.type === 'voice_tool_result') {
        updateVoiceTyping(`${ev.name} done`);
      } else if (ev.type === 'voice_assistant') {
        removeVoiceTyping();
        addVoiceMsg('assistant', ev.text, ev.tool_calls);
      }
    }
  } catch {}
}

setInterval(pollVoiceEvents, 1000);
</script>
</body>
</html>"""
