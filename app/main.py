import json
import logging
import uuid
from datetime import datetime, timezone

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from app.agent import chat, chat_sync
from app.db import refresh_db
from app.config import SERVER_PORT, BRIDGE_URL

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="WhatsApp Assistant", version="0.1.0")

# In-memory conversation store (per session)
conversations: dict[str, list[dict]] = {}


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


# ---------------------------------------------------------------------------
# Inline HTML page — single-file chat UI
# ---------------------------------------------------------------------------
HTML_PAGE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
<title>WhatsApp Assistant</title>
<style>
  :root {
    --wa-bg-deep: #0b141a;
    --wa-bg-panel: #111b21;
    --wa-bg-header: #202c33;
    --wa-bg-input: #2a3942;
    --wa-bg-msg-in: #202c33;
    --wa-bg-msg-out: #005c4b;
    --wa-green: #00a884;
    --wa-green-light: #25d366;
    --wa-green-hover: #06cf9c;
    --wa-text: #e9edef;
    --wa-text-secondary: #8696a0;
    --wa-text-muted: rgba(255,255,255,0.45);
    --wa-border: #2a3942;
    --wa-blue-check: #53bdeb;
    --wa-separator: #222e35;
  }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body {
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Helvetica, Arial, sans-serif;
    background: var(--wa-bg-deep);
    color: var(--wa-text);
    height: 100vh;
    height: 100dvh;
    display: flex;
    flex-direction: column;
    overflow: hidden;
  }

  /* ===== HEADER ===== */
  .header {
    background: var(--wa-bg-header);
    padding: 10px 16px;
    display: flex;
    align-items: center;
    gap: 12px;
    min-height: 60px;
    z-index: 10;
  }
  .header .avatar {
    width: 40px; height: 40px;
    background: var(--wa-green);
    border-radius: 50%;
    display: flex; align-items: center; justify-content: center;
    flex-shrink: 0;
    position: relative;
    overflow: hidden;
  }
  .header .avatar svg { width: 24px; height: 24px; fill: #fff; }
  .header .info { flex: 1; min-width: 0; }
  .header .info h2 {
    font-size: 16px; font-weight: 500;
    white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
  }
  .header .info .status-row {
    display: flex; align-items: center; gap: 6px;
  }
  .header .info .status-dot {
    width: 6px; height: 6px; border-radius: 50%;
    background: var(--wa-green-light);
    flex-shrink: 0;
  }
  .header .info .status-dot.busy {
    background: #f5c842;
    animation: pulse-dot 1.5s ease-in-out infinite;
  }
  @keyframes pulse-dot {
    0%, 100% { opacity: 1; }
    50% { opacity: 0.3; }
  }
  .header .info p {
    font-size: 12px; color: var(--wa-text-secondary);
    white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
  }
  .header-actions {
    display: flex; align-items: center; gap: 4px; flex-shrink: 0;
  }
  .header-actions .view-toggle {
    display: flex;
    background: var(--wa-bg-input);
    border-radius: 20px;
    overflow: hidden;
    border: 1px solid var(--wa-border);
  }
  .view-toggle button {
    background: none;
    border: none;
    color: var(--wa-text-secondary);
    padding: 6px 14px;
    font-size: 11px;
    font-weight: 500;
    cursor: pointer;
    transition: all 0.2s;
    letter-spacing: 0.3px;
    text-transform: uppercase;
  }
  .view-toggle button.active {
    background: var(--wa-green);
    color: #111b21;
  }
  .view-toggle button:hover:not(.active) { color: var(--wa-text); }
  .header-btn {
    width: 36px; height: 36px;
    background: none; border: none;
    color: var(--wa-text-secondary);
    border-radius: 50%;
    cursor: pointer;
    display: flex; align-items: center; justify-content: center;
    transition: background 0.2s;
  }
  .header-btn:hover { background: rgba(255,255,255,0.06); }
  .header-btn svg { width: 20px; height: 20px; fill: currentColor; }

  /* ===== CHAT WALLPAPER ===== */
  .messages-wrapper {
    flex: 1;
    overflow: hidden;
    position: relative;
    background: var(--wa-bg-deep);
  }
  .messages-wrapper::before {
    content: '';
    position: absolute;
    inset: 0;
    background-image: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='300' height='300'%3E%3Cdefs%3E%3Cpattern id='p' width='50' height='50' patternUnits='userSpaceOnUse'%3E%3Cpath d='M25 0v50M0 25h50' stroke='%23ffffff' stroke-width='0.3' opacity='0.02'/%3E%3C/pattern%3E%3C/defs%3E%3Crect fill='url(%23p)' width='300' height='300'/%3E%3C/svg%3E");
    opacity: 0.6;
    pointer-events: none;
  }
  .messages {
    height: 100%;
    overflow-y: auto;
    overflow-x: hidden;
    padding: 16px 60px 8px;
    display: flex;
    flex-direction: column;
    gap: 2px;
    position: relative;
    z-index: 1;
    scroll-behavior: smooth;
  }
  .messages::-webkit-scrollbar { width: 6px; }
  .messages::-webkit-scrollbar-thumb { background: rgba(255,255,255,0.15); border-radius: 3px; }
  .messages::-webkit-scrollbar-track { background: transparent; }

  /* ===== DATE SEPARATOR ===== */
  .date-separator {
    text-align: center;
    padding: 8px 0 4px;
    user-select: none;
  }
  .date-separator span {
    background: #182229;
    color: var(--wa-text-secondary);
    font-size: 12px;
    padding: 5px 12px;
    border-radius: 7px;
    box-shadow: 0 1px 1px rgba(0,0,0,0.15);
  }

  /* ===== MESSAGES ===== */
  .msg {
    max-width: 65%;
    padding: 6px 7px 8px 9px;
    border-radius: 7.5px;
    font-size: 14.2px;
    line-height: 1.45;
    position: relative;
    word-wrap: break-word;
    overflow-wrap: break-word;
    box-shadow: 0 1px 0.5px rgba(0,0,0,0.13);
  }
  .msg.user {
    background: var(--wa-bg-msg-out);
    align-self: flex-end;
    border-top-right-radius: 0;
  }
  .msg.user::before {
    content: '';
    position: absolute;
    top: 0; right: -8px;
    width: 8px; height: 13px;
    background: var(--wa-bg-msg-out);
    clip-path: polygon(0 0, 0 100%, 100% 0);
  }
  .msg.assistant {
    background: var(--wa-bg-msg-in);
    align-self: flex-start;
    border-top-left-radius: 0;
  }
  .msg.assistant::before {
    content: '';
    position: absolute;
    top: 0; left: -8px;
    width: 8px; height: 13px;
    background: var(--wa-bg-msg-in);
    clip-path: polygon(100% 0, 0 0, 100% 100%);
  }
  .msg .meta-row {
    display: flex;
    justify-content: flex-end;
    align-items: center;
    gap: 4px;
    margin-top: 2px;
    float: right;
    margin-left: 12px;
    position: relative;
    top: 5px;
  }
  .msg .time {
    font-size: 11px;
    color: var(--wa-text-muted);
    white-space: nowrap;
  }
  .msg.user .check-marks { display: inline-flex; margin-left: 2px; }
  .msg.user .check-marks svg { width: 16px; height: 11px; fill: var(--wa-blue-check); }

  /* ===== TYPING INDICATOR (WhatsApp-style bubbles) ===== */
  .typing-bubble {
    background: var(--wa-bg-msg-in);
    align-self: flex-start;
    border-radius: 7.5px;
    border-top-left-radius: 0;
    padding: 10px 16px;
    position: relative;
    box-shadow: 0 1px 0.5px rgba(0,0,0,0.13);
    display: flex;
    flex-direction: column;
    gap: 8px;
    max-width: 320px;
  }
  .typing-bubble::before {
    content: '';
    position: absolute;
    top: 0; left: -8px;
    width: 8px; height: 13px;
    background: var(--wa-bg-msg-in);
    clip-path: polygon(100% 0, 0 0, 100% 100%);
  }
  .typing-dots {
    display: flex;
    align-items: center;
    gap: 4px;
    height: 20px;
  }
  .typing-dots span {
    width: 7px; height: 7px;
    background: var(--wa-text-secondary);
    border-radius: 50%;
    animation: typing-bounce 1.4s ease-in-out infinite;
  }
  .typing-dots span:nth-child(2) { animation-delay: 0.2s; }
  .typing-dots span:nth-child(3) { animation-delay: 0.4s; }
  @keyframes typing-bounce {
    0%, 60%, 100% { transform: translateY(0); opacity: 0.4; }
    30% { transform: translateY(-5px); opacity: 1; }
  }
  .typing-status {
    font-size: 12px;
    color: var(--wa-green);
    display: flex;
    align-items: center;
    gap: 6px;
    overflow: hidden;
    white-space: nowrap;
    text-overflow: ellipsis;
  }
  .typing-status .status-icon {
    flex-shrink: 0;
    width: 14px; height: 14px;
    border: 2px solid var(--wa-green);
    border-top-color: transparent;
    border-radius: 50%;
    animation: spin 0.8s linear infinite;
  }
  @keyframes spin { to { transform: rotate(360deg); } }

  /* ===== DEVELOPER TOOL PANEL ===== */
  .dev-tool-panel {
    margin-top: 6px;
    border-top: 1px solid var(--wa-separator);
    padding-top: 6px;
  }
  .dev-tool-item {
    margin-bottom: 6px;
    border-radius: 6px;
    overflow: hidden;
    border: 1px solid var(--wa-separator);
  }
  .dev-tool-header {
    display: flex;
    align-items: center;
    gap: 6px;
    padding: 6px 8px;
    background: rgba(0,168,132,0.08);
    cursor: pointer;
    font-size: 12px;
    color: var(--wa-green);
    user-select: none;
    transition: background 0.15s;
  }
  .dev-tool-header:hover { background: rgba(0,168,132,0.15); }
  .dev-tool-header .tool-badge {
    background: var(--wa-green);
    color: #111b21;
    font-size: 9px;
    font-weight: 700;
    padding: 2px 5px;
    border-radius: 3px;
    text-transform: uppercase;
    letter-spacing: 0.5px;
  }
  .dev-tool-header .tool-name { font-weight: 600; }
  .dev-tool-header .tool-arrow {
    margin-left: auto;
    transition: transform 0.2s;
    font-size: 10px;
  }
  .dev-tool-header.open .tool-arrow { transform: rotate(90deg); }
  .dev-tool-body {
    display: none;
    padding: 8px;
    background: rgba(0,0,0,0.2);
  }
  .dev-tool-body.open { display: block; }
  .dev-tool-section {
    margin-bottom: 6px;
  }
  .dev-tool-section:last-child { margin-bottom: 0; }
  .dev-tool-label {
    font-size: 10px;
    font-weight: 600;
    color: var(--wa-text-secondary);
    text-transform: uppercase;
    letter-spacing: 0.5px;
    margin-bottom: 3px;
  }
  .dev-tool-section pre {
    font-size: 11px;
    line-height: 1.4;
    color: var(--wa-text);
    white-space: pre-wrap;
    word-break: break-all;
    max-height: 180px;
    overflow-y: auto;
    background: rgba(0,0,0,0.15);
    padding: 6px 8px;
    border-radius: 4px;
    font-family: 'SF Mono', 'Fira Code', 'Cascadia Code', monospace;
  }
  .dev-tool-section pre::-webkit-scrollbar { width: 4px; }
  .dev-tool-section pre::-webkit-scrollbar-thumb { background: rgba(255,255,255,0.1); border-radius: 2px; }
  .dev-tool-summary {
    display: flex;
    align-items: center;
    gap: 6px;
    font-size: 11px;
    color: var(--wa-text-secondary);
    padding: 4px 0 2px;
  }
  .dev-tool-summary .tool-count {
    background: rgba(0,168,132,0.15);
    color: var(--wa-green);
    padding: 2px 8px;
    border-radius: 10px;
    font-size: 11px;
    font-weight: 500;
  }

  /* ===== INPUT AREA ===== */
  .input-area {
    background: var(--wa-bg-header);
    padding: 8px 16px;
    display: flex;
    gap: 8px;
    align-items: flex-end;
    z-index: 10;
  }
  .input-area .input-wrapper {
    flex: 1;
    display: flex;
    align-items: flex-end;
    background: var(--wa-bg-input);
    border-radius: 8px;
    padding: 0 12px;
    min-height: 42px;
  }
  .input-area textarea {
    flex: 1;
    background: none;
    border: none;
    outline: none;
    color: var(--wa-text);
    padding: 10px 0;
    font-size: 14px;
    font-family: inherit;
    resize: none;
    max-height: 100px;
    line-height: 1.4;
    overflow-y: auto;
  }
  .input-area textarea::placeholder { color: var(--wa-text-secondary); }
  .input-area textarea::-webkit-scrollbar { width: 4px; }
  .input-area textarea::-webkit-scrollbar-thumb { background: rgba(255,255,255,0.1); border-radius: 2px; }
  .send-btn {
    background: var(--wa-green);
    border: none;
    color: #111b21;
    width: 42px; height: 42px;
    border-radius: 50%;
    cursor: pointer;
    display: flex; align-items: center; justify-content: center;
    transition: all 0.2s;
    flex-shrink: 0;
  }
  .send-btn:hover { background: var(--wa-green-hover); transform: scale(1.05); }
  .send-btn:disabled { background: var(--wa-bg-input); cursor: not-allowed; transform: none; }
  .send-btn svg { width: 20px; height: 20px; fill: currentColor; }

  /* ===== WELCOME SCREEN ===== */
  .welcome {
    text-align: center;
    color: var(--wa-text-secondary);
    margin: auto;
    max-width: 440px;
    padding: 20px;
  }
  .welcome .welcome-icon {
    width: 72px; height: 72px;
    background: rgba(0,168,132,0.12);
    border-radius: 50%;
    display: flex; align-items: center; justify-content: center;
    margin: 0 auto 20px;
  }
  .welcome .welcome-icon svg { width: 36px; height: 36px; fill: var(--wa-green); }
  .welcome h3 {
    color: var(--wa-text);
    font-size: 20px;
    font-weight: 400;
    margin-bottom: 8px;
  }
  .welcome p {
    font-size: 14px;
    line-height: 1.5;
    margin-bottom: 24px;
  }
  .welcome .e2e-badge {
    display: inline-flex;
    align-items: center;
    gap: 6px;
    background: rgba(0,168,132,0.08);
    border-radius: 20px;
    padding: 6px 14px;
    font-size: 12px;
    color: var(--wa-text-secondary);
    margin-bottom: 24px;
  }
  .welcome .e2e-badge svg { width: 14px; height: 14px; fill: var(--wa-text-secondary); }
  .welcome .examples {
    text-align: left;
    display: flex;
    flex-direction: column;
    gap: 8px;
  }
  .welcome .examples .example-item {
    background: var(--wa-bg-msg-in);
    padding: 12px 16px;
    border-radius: 10px;
    cursor: pointer;
    transition: all 0.2s;
    display: flex;
    align-items: center;
    gap: 12px;
    font-size: 13.5px;
    color: var(--wa-text);
    line-height: 1.4;
  }
  .welcome .examples .example-item:hover {
    background: var(--wa-bg-input);
    transform: translateX(4px);
  }
  .welcome .examples .example-icon {
    width: 32px; height: 32px;
    background: rgba(0,168,132,0.12);
    border-radius: 50%;
    display: flex; align-items: center; justify-content: center;
    flex-shrink: 0;
    font-size: 15px;
  }

  /* ===== CONTENT FORMATTING ===== */
  .msg-content { white-space: pre-wrap; }
  .msg-content p { margin: 0.3em 0; }
  .msg-content p:first-child { margin-top: 0; }
  .msg-content p:last-child { margin-bottom: 0; }
  .msg-content code {
    background: rgba(255,255,255,0.08);
    padding: 1px 5px;
    border-radius: 4px;
    font-size: 13px;
    font-family: 'SF Mono', 'Fira Code', monospace;
  }
  .msg-content pre {
    background: rgba(0,0,0,0.25);
    border-radius: 6px;
    margin: 6px 0;
    overflow-x: auto;
  }
  .msg-content pre code {
    display: block;
    padding: 10px 12px;
    background: none;
    font-size: 12px;
    line-height: 1.5;
  }
  .msg-content strong { color: #fff; }
  .msg-content em { color: rgba(233,237,239,0.85); }
  .msg-content ul, .msg-content ol { padding-left: 20px; margin: 4px 0; }
  .msg-content li { margin: 2px 0; }
  .msg-content a { color: var(--wa-blue-check); text-decoration: none; }
  .msg-content a:hover { text-decoration: underline; }
  .msg-content table { border-collapse: collapse; margin: 6px 0; width: 100%; font-size: 13px; }
  .msg-content th, .msg-content td {
    border: 1px solid var(--wa-border);
    padding: 4px 8px;
    text-align: left;
  }
  .msg-content th { background: rgba(0,168,132,0.1); font-weight: 600; }

  /* ===== BRIDGE STATUS ===== */
  .bridge-status {
    display: flex;
    align-items: center;
    gap: 6px;
    cursor: pointer;
    padding: 4px 10px;
    border-radius: 16px;
    background: rgba(255,255,255,0.05);
    border: 1px solid var(--wa-border);
    transition: background 0.2s;
    font-size: 11px;
    color: var(--wa-text-secondary);
    white-space: nowrap;
  }
  .bridge-status:hover { background: rgba(255,255,255,0.1); }
  .bridge-dot {
    width: 8px; height: 8px;
    border-radius: 50%;
    flex-shrink: 0;
  }
  .bridge-dot.connected { background: #25d366; }
  .bridge-dot.qr_pending { background: #f5c842; animation: pulse-dot 1.5s ease-in-out infinite; }
  .bridge-dot.disconnected, .bridge-dot.bridge_offline { background: #ea4335; }

  /* ===== QR MODAL ===== */
  .qr-overlay {
    display: none;
    position: fixed;
    inset: 0;
    background: rgba(0,0,0,0.7);
    z-index: 100;
    align-items: center;
    justify-content: center;
  }
  .qr-overlay.active { display: flex; }
  .qr-modal {
    background: var(--wa-bg-panel);
    border-radius: 12px;
    padding: 28px;
    max-width: 380px;
    width: 90%;
    text-align: center;
    box-shadow: 0 8px 32px rgba(0,0,0,0.4);
  }
  .qr-modal h3 {
    font-size: 18px;
    font-weight: 500;
    margin-bottom: 6px;
    color: var(--wa-text);
  }
  .qr-modal p {
    font-size: 13px;
    color: var(--wa-text-secondary);
    margin-bottom: 20px;
    line-height: 1.5;
  }
  .qr-container {
    background: #fff;
    border-radius: 8px;
    padding: 12px;
    display: inline-block;
    margin-bottom: 16px;
    min-height: 300px;
    min-width: 300px;
    display: flex;
    align-items: center;
    justify-content: center;
  }
  .qr-container img { width: 276px; height: 276px; }
  .qr-container .qr-loading {
    color: #666;
    font-size: 13px;
  }
  .qr-close-btn {
    background: var(--wa-bg-input);
    border: 1px solid var(--wa-border);
    color: var(--wa-text);
    padding: 8px 24px;
    border-radius: 20px;
    cursor: pointer;
    font-size: 13px;
    transition: background 0.2s;
  }
  .qr-close-btn:hover { background: var(--wa-bg-header); }

  /* ===== RESPONSIVE ===== */
  @media (max-width: 768px) {
    .messages { padding: 10px 12px 8px; }
    .msg { max-width: 85%; }
    .view-toggle button { padding: 5px 10px; font-size: 10px; }
  }
</style>
</head>
<body>
<!-- HEADER -->
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
    <div class="bridge-status" id="bridge-status" onclick="onBridgeStatusClick()" title="WhatsApp Bridge connection status">
      <div class="bridge-dot disconnected" id="bridge-dot"></div>
      <span id="bridge-label">Bridge offline</span>
    </div>
    <div class="view-toggle" id="view-toggle">
      <button class="active" data-view="user" onclick="setView('user')">User</button>
      <button data-view="dev" onclick="setView('dev')">Dev</button>
    </div>
    <button class="header-btn" onclick="refreshDB()" title="Refresh WhatsApp data">
      <svg viewBox="0 0 24 24"><path d="M17.65 6.35A7.958 7.958 0 0012 4c-4.42 0-7.99 3.58-7.99 8s3.57 8 7.99 8c3.73 0 6.84-2.55 7.73-6h-2.08A5.99 5.99 0 0112 18c-3.31 0-6-2.69-6-6s2.69-6 6-6c1.66 0 3.14.69 4.22 1.78L13 11h7V4l-2.35 2.35z"/></svg>
    </button>
    <button class="header-btn" onclick="clearChat()" title="Clear conversation">
      <svg viewBox="0 0 24 24"><path d="M6 19c0 1.1.9 2 2 2h8c1.1 0 2-.9 2-2V7H6v12zM19 4h-3.5l-1-1h-5l-1 1H5v2h14V4z"/></svg>
    </button>
  </div>
</div>

<!-- MESSAGES AREA -->
<div class="messages-wrapper">
  <div class="messages" id="messages">
    <div class="welcome" id="welcome-screen">
      <div class="welcome-icon">
        <svg viewBox="0 0 24 24"><path d="M17.498 14.382c-.301-.15-1.767-.867-2.04-.966-.273-.101-.473-.15-.673.15-.197.295-.771.964-.944 1.162-.175.195-.349.21-.646.075-.3-.15-1.263-.465-2.403-1.485-.888-.795-1.484-1.77-1.66-2.07-.174-.3-.019-.465.13-.615.136-.135.301-.345.451-.523.146-.181.194-.301.297-.496.1-.21.049-.375-.025-.524-.075-.15-.672-1.62-.922-2.206-.24-.584-.487-.51-.672-.51-.172-.015-.371-.015-.571-.015-.2 0-.523.074-.797.359-.273.3-1.045 1.02-1.045 2.475s1.07 2.865 1.219 3.075c.149.195 2.105 3.195 5.1 4.485.714.3 1.27.48 1.704.629.714.227 1.365.195 1.88.121.574-.091 1.767-.721 2.016-1.426.255-.691.255-1.29.18-1.425-.074-.135-.27-.21-.57-.345z"/><path d="M20.52 3.449C12.831-3.984.106 1.407.101 11.893c0 2.096.549 4.14 1.595 5.945L0 24l6.335-1.652c7.905 4.27 17.661-1.4 17.665-10.449 0-2.8-1.092-5.434-3.08-7.406l-.4-.044zm-8.52 18.2c-1.792 0-3.546-.48-5.076-1.385l-.363-.216-3.776.99 1.008-3.676-.235-.374A9.846 9.846 0 012.1 11.893C2.1 6.443 6.543 2.001 12 2.001c2.647 0 5.133 1.03 7.002 2.899a9.825 9.825 0 012.898 6.993c-.003 5.45-4.437 9.756-9.9 9.756z"/></svg>
      </div>
      <h3>WhatsApp Assistant</h3>
      <div class="e2e-badge">
        <svg viewBox="0 0 24 24"><path d="M18 8h-1V6c0-2.76-2.24-5-5-5S7 3.24 7 6v2H6c-1.1 0-2 .9-2 2v10c0 1.1.9 2 2 2h12c1.1 0 2-.9 2-2V10c0-1.1-.9-2-2-2zm-6 9c-1.1 0-2-.9-2-2s.9-2 2-2 2 .9 2 2-.9 2-2 2zm3.1-9H8.9V6c0-1.71 1.39-3.1 3.1-3.1s3.1 1.39 3.1 3.1v2z"/></svg>
        Local access to your WhatsApp data
      </div>
      <p>Ask me anything about your chats, contacts, and messages. Your data stays on your device.</p>
      <div class="examples">
        <div class="example-item" onclick="askExample(this.querySelector('.example-text').textContent)">
          <div class="example-icon">💬</div>
          <span class="example-text">What were my recent chats?</span>
        </div>
        <div class="example-item" onclick="askExample(this.querySelector('.example-text').textContent)">
          <div class="example-icon">🔍</div>
          <span class="example-text">Find contact Priya and show our recent conversation</span>
        </div>
        <div class="example-item" onclick="askExample(this.querySelector('.example-text').textContent)">
          <div class="example-icon">📨</div>
          <span class="example-text">Search for messages about "meeting" across all chats</span>
        </div>
        <div class="example-item" onclick="askExample(this.querySelector('.example-text').textContent)">
          <div class="example-icon">📊</div>
          <span class="example-text">Show me stats for my most active group</span>
        </div>
      </div>
    </div>
  </div>
</div>

<!-- QR MODAL -->
<div class="qr-overlay" id="qr-overlay" onclick="closeQRModal(event)">
  <div class="qr-modal">
    <h3>Connect WhatsApp</h3>
    <p>Scan the QR code with your phone:<br>WhatsApp &gt; Settings &gt; Linked Devices &gt; Link a Device</p>
    <div class="qr-container" id="qr-container">
      <span class="qr-loading">Loading QR code...</span>
    </div>
    <br>
    <button class="qr-close-btn" onclick="closeQRModal()">Close</button>
  </div>
</div>

<!-- INPUT AREA -->
<div class="input-area">
  <div class="input-wrapper">
    <textarea id="input" rows="1" placeholder="Type a message" autofocus></textarea>
  </div>
  <button class="send-btn" id="send-btn" onclick="sendMessage()">
    <svg viewBox="0 0 24 24"><path d="M2.01 21L23 12 2.01 3 2 10l15 2-15 2z"/></svg>
  </button>
</div>

<script>
// ===== STATE =====
let conversationId = null;
let sending = false;
let currentView = 'user'; // 'user' or 'dev'
let allToolData = []; // Store tool data per message for view switching

const messagesEl = document.getElementById('messages');
const inputEl = document.getElementById('input');
const sendBtn = document.getElementById('send-btn');
const statusText = document.getElementById('status-text');
const statusDot = document.getElementById('status-dot');

// ===== WHATSAPP-THEMED STATUS MESSAGES =====
const statusMessages = {
  connecting: [
    'Connecting...',
    'Establishing connection...',
    'Dialing in...',
    'Syncing...',
    'Handshaking...',
  ],
  searching: [
    'Scrolling through chats...',
    'Flipping through messages...',
    'Browsing contacts...',
    'Scanning conversations...',
    'Sifting through archives...',
    'Leafing through history...',
    'Skimming threads...',
    'Combing through chats...',
    'Peeking into inboxes...',
    'Rummaging through messages...',
  ],
  processing: [
    'Decrypting insights...',
    'Piecing together the story...',
    'Connecting the dots...',
    'Assembling the timeline...',
    'Reading between the lines...',
    'Untangling threads...',
    'Cross-referencing chats...',
    'Mapping conversations...',
    'Weaving the narrative...',
    'Sorting the signal from noise...',
  ],
  finishing: [
    'Composing reply...',
    'Typing up the answer...',
    'Drafting response...',
    'Putting it all together...',
    'Wrapping things up...',
    'Polishing the message...',
    'Almost there...',
    'Final touches...',
    'Sealing the envelope...',
    'Ready to send...',
  ]
};

let statusPhase = 'connecting';
let statusInterval = null;

function getRandomStatus(phase) {
  const msgs = statusMessages[phase] || statusMessages.connecting;
  return msgs[Math.floor(Math.random() * msgs.length)];
}

function startStatusCycle() {
  statusPhase = 'connecting';
  updateStatusDisplay(getRandomStatus('connecting'));
  let tick = 0;
  statusInterval = setInterval(() => {
    tick++;
    if (tick < 2) statusPhase = 'connecting';
    else if (tick < 5) statusPhase = 'searching';
    else if (tick < 10) statusPhase = 'processing';
    else statusPhase = 'finishing';
    updateStatusDisplay(getRandomStatus(statusPhase));
  }, 2500);
}

function stopStatusCycle() {
  clearInterval(statusInterval);
  statusInterval = null;
}

function updateStatusDisplay(text) {
  statusText.textContent = text;
  statusDot.classList.add('busy');
}

function setOnline() {
  statusText.textContent = 'online';
  statusDot.classList.remove('busy');
}

// ===== VIEW TOGGLE =====
function setView(view) {
  currentView = view;
  document.querySelectorAll('.view-toggle button').forEach(b => {
    b.classList.toggle('active', b.dataset.view === view);
  });
  // Toggle visibility of all tool panels
  document.querySelectorAll('.dev-tool-panel').forEach(el => {
    el.style.display = view === 'dev' ? 'block' : 'none';
  });
}

// ===== INPUT HANDLING =====
inputEl.addEventListener('keydown', e => {
  if (e.key === 'Enter' && !e.shiftKey && !sending) {
    e.preventDefault();
    sendMessage();
  }
});

// Auto-resize textarea
inputEl.addEventListener('input', () => {
  inputEl.style.height = 'auto';
  inputEl.style.height = Math.min(inputEl.scrollHeight, 100) + 'px';
});

function askExample(text) {
  inputEl.value = text;
  inputEl.style.height = 'auto';
  inputEl.style.height = Math.min(inputEl.scrollHeight, 100) + 'px';
  sendMessage();
}

// ===== MESSAGE RENDERING =====
function addDateSeparator() {
  // Add date separator if this is the first message
  if (!messagesEl.querySelector('.date-separator')) {
    const sep = document.createElement('div');
    sep.className = 'date-separator';
    const now = new Date();
    const today = now.toLocaleDateString('en-US', { weekday: 'long', month: 'long', day: 'numeric' });
    sep.innerHTML = `<span>TODAY</span>`;
    messagesEl.appendChild(sep);
  }
}

function addMessage(role, content, toolCalls, toolResults) {
  const welcome = document.getElementById('welcome-screen');
  if (welcome) welcome.remove();
  addDateSeparator();

  const div = document.createElement('div');
  div.className = `msg ${role}`;

  const contentDiv = document.createElement('div');
  contentDiv.className = 'msg-content';
  contentDiv.innerHTML = renderMarkdown(content);
  div.appendChild(contentDiv);

  // Developer tool panel (hidden in user view)
  if (role === 'assistant' && toolCalls && toolCalls.length > 0) {
    const toolPanel = document.createElement('div');
    toolPanel.className = 'dev-tool-panel';
    toolPanel.style.display = currentView === 'dev' ? 'block' : 'none';

    // Summary line
    const summary = document.createElement('div');
    summary.className = 'dev-tool-summary';
    summary.innerHTML = `<span class="tool-count">${toolCalls.length} tool${toolCalls.length > 1 ? 's' : ''}</span> used to generate this response`;
    toolPanel.appendChild(summary);

    // Each tool call
    toolCalls.forEach((tc, i) => {
      const item = document.createElement('div');
      item.className = 'dev-tool-item';

      const header = document.createElement('div');
      header.className = 'dev-tool-header';
      header.innerHTML = `
        <span class="tool-badge">TOOL</span>
        <span class="tool-name">${escapeHtml(tc.name)}</span>
        <span class="tool-arrow">▶</span>
      `;
      header.onclick = () => {
        header.classList.toggle('open');
        body.classList.toggle('open');
      };
      item.appendChild(header);

      const body = document.createElement('div');
      body.className = 'dev-tool-body';

      // Arguments section
      const argsSection = document.createElement('div');
      argsSection.className = 'dev-tool-section';
      argsSection.innerHTML = `<div class="dev-tool-label">Arguments</div>`;
      const argsPre = document.createElement('pre');
      argsPre.textContent = JSON.stringify(tc.arguments, null, 2);
      argsSection.appendChild(argsPre);
      body.appendChild(argsSection);

      // Result section (if we have it)
      if (toolResults && toolResults[i]) {
        const resultSection = document.createElement('div');
        resultSection.className = 'dev-tool-section';
        resultSection.innerHTML = `<div class="dev-tool-label">Response</div>`;
        const resultPre = document.createElement('pre');
        try {
          const parsed = JSON.parse(toolResults[i]);
          resultPre.textContent = JSON.stringify(parsed, null, 2);
        } catch {
          resultPre.textContent = toolResults[i];
        }
        resultSection.appendChild(resultPre);
        body.appendChild(resultSection);
      }

      item.appendChild(body);
      toolPanel.appendChild(item);
    });

    div.appendChild(toolPanel);
  }

  // Meta row (time + checkmarks)
  const meta = document.createElement('div');
  meta.className = 'meta-row';
  const timeSpan = document.createElement('span');
  timeSpan.className = 'time';
  timeSpan.textContent = new Date().toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
  meta.appendChild(timeSpan);

  if (role === 'user') {
    const check = document.createElement('span');
    check.className = 'check-marks';
    check.innerHTML = '<svg viewBox="0 0 16 11"><path d="M11.071.653a.457.457 0 0 0-.304-.102.493.493 0 0 0-.381.178l-6.19 7.636-2.405-2.272a.463.463 0 0 0-.336-.146.47.47 0 0 0-.343.146l-.311.31a.445.445 0 0 0-.14.337c0 .136.047.25.14.343l2.996 2.996a.724.724 0 0 0 .501.203.697.697 0 0 0 .546-.266l6.646-8.417a.497.497 0 0 0 .108-.299.441.441 0 0 0-.14-.337l-.387-.31zm-2.26 7.636l.387.387a.732.732 0 0 0 .514.178.697.697 0 0 0 .546-.266l6.646-8.417a.497.497 0 0 0 .108-.299.441.441 0 0 0-.14-.337l-.387-.31a.457.457 0 0 0-.304-.102.493.493 0 0 0-.381.178l-6.19 7.636"/></svg>';
    meta.appendChild(check);
  }
  div.appendChild(meta);

  messagesEl.appendChild(div);
  messagesEl.scrollTop = messagesEl.scrollHeight;
  return div;
}

function addTypingIndicator() {
  const welcome = document.getElementById('welcome-screen');
  if (welcome) welcome.remove();
  addDateSeparator();

  const div = document.createElement('div');
  div.className = 'typing-bubble';
  div.id = 'typing-indicator';
  div.innerHTML = `
    <div class="typing-dots"><span></span><span></span><span></span></div>
    <div class="typing-status" id="typing-status"><div class="status-icon"></div> <span id="typing-text">Connecting...</span></div>
  `;
  messagesEl.appendChild(div);
  messagesEl.scrollTop = messagesEl.scrollHeight;
  return div;
}

function updateTypingText(text) {
  const el = document.getElementById('typing-text');
  if (el) el.textContent = text;
  messagesEl.scrollTop = messagesEl.scrollHeight;
}

function removeTypingIndicator() {
  const el = document.getElementById('typing-indicator');
  if (el) el.remove();
}

// ===== SEND MESSAGE =====
async function sendMessage() {
  const text = inputEl.value.trim();
  if (!text || sending) return;

  sending = true;
  sendBtn.disabled = true;
  inputEl.value = '';
  inputEl.style.height = 'auto';

  addMessage('user', text);
  addTypingIndicator();
  startStatusCycle();

  try {
    const res = await fetch('/api/chat/stream', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ message: text, conversation_id: conversationId }),
    });

    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buffer = '';
    let toolCalls = [];
    let toolResults = [];
    let finalContent = '';
    let toolCallIndex = 0;

    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });

      const lines = buffer.split('\n');
      buffer = lines.pop();

      for (const line of lines) {
        if (!line.startsWith('data: ')) continue;
        const data = line.slice(6).trim();
        if (data === '[DONE]') continue;

        try {
          const event = JSON.parse(data);
          if (event.type === 'conv_id') {
            conversationId = event.conversation_id;
          } else if (event.type === 'tool_call') {
            toolCalls.push({ name: event.name, arguments: event.arguments });
            toolCallIndex = toolCalls.length - 1;
            // User view: fun status. Dev view: actual tool name
            if (currentView === 'user') {
              statusPhase = 'searching';
              updateTypingText(getRandomStatus('searching'));
            } else {
              updateTypingText(`Calling ${event.name}...`);
            }
            updateStatusDisplay(currentView === 'user' ? getRandomStatus('searching') : `Using: ${event.name}`);
          } else if (event.type === 'tool_result') {
            toolResults[toolCallIndex] = event.result || '';
            if (currentView === 'user') {
              statusPhase = 'processing';
              updateTypingText(getRandomStatus('processing'));
            } else {
              updateTypingText(`${event.name} returned data`);
            }
          } else if (event.type === 'message') {
            finalContent = event.content;
            if (currentView === 'user') {
              statusPhase = 'finishing';
              updateTypingText(getRandomStatus('finishing'));
            }
          } else if (event.type === 'error') {
            finalContent = `Error: ${event.content}`;
          }
        } catch(e) {}
      }
    }

    removeTypingIndicator();
    stopStatusCycle();
    if (finalContent) {
      addMessage('assistant', finalContent, toolCalls, toolResults);
    }
  } catch (err) {
    removeTypingIndicator();
    stopStatusCycle();
    addMessage('assistant', `Something went wrong: ${err.message}`);
  }

  sending = false;
  sendBtn.disabled = false;
  setOnline();
  inputEl.focus();
}

// ===== UTILITY FUNCTIONS =====
async function refreshDB() {
  try {
    await fetch('/api/refresh', { method: 'POST' });
  } catch {}
}

async function clearChat() {
  if (conversationId) {
    try {
      await fetch(`/api/conversation/${conversationId}`, { method: 'DELETE' });
    } catch {}
  }
  conversationId = null;
  messagesEl.innerHTML = '';

  // Re-add welcome screen
  messagesEl.innerHTML = `
    <div class="welcome" id="welcome-screen">
      <div class="welcome-icon">
        <svg viewBox="0 0 24 24"><path d="M17.498 14.382c-.301-.15-1.767-.867-2.04-.966-.273-.101-.473-.15-.673.15-.197.295-.771.964-.944 1.162-.175.195-.349.21-.646.075-.3-.15-1.263-.465-2.403-1.485-.888-.795-1.484-1.77-1.66-2.07-.174-.3-.019-.465.13-.615.136-.135.301-.345.451-.523.146-.181.194-.301.297-.496.1-.21.049-.375-.025-.524-.075-.15-.672-1.62-.922-2.206-.24-.584-.487-.51-.672-.51-.172-.015-.371-.015-.571-.015-.2 0-.523.074-.797.359-.273.3-1.045 1.02-1.045 2.475s1.07 2.865 1.219 3.075c.149.195 2.105 3.195 5.1 4.485.714.3 1.27.48 1.704.629.714.227 1.365.195 1.88.121.574-.091 1.767-.721 2.016-1.426.255-.691.255-1.29.18-1.425-.074-.135-.27-.21-.57-.345z"/><path d="M20.52 3.449C12.831-3.984.106 1.407.101 11.893c0 2.096.549 4.14 1.595 5.945L0 24l6.335-1.652c7.905 4.27 17.661-1.4 17.665-10.449 0-2.8-1.092-5.434-3.08-7.406l-.4-.044zm-8.52 18.2c-1.792 0-3.546-.48-5.076-1.385l-.363-.216-3.776.99 1.008-3.676-.235-.374A9.846 9.846 0 012.1 11.893C2.1 6.443 6.543 2.001 12 2.001c2.647 0 5.133 1.03 7.002 2.899a9.825 9.825 0 012.898 6.993c-.003 5.45-4.437 9.756-9.9 9.756z"/></svg>
      </div>
      <h3>WhatsApp Assistant</h3>
      <div class="e2e-badge">
        <svg viewBox="0 0 24 24"><path d="M18 8h-1V6c0-2.76-2.24-5-5-5S7 3.24 7 6v2H6c-1.1 0-2 .9-2 2v10c0 1.1.9 2 2 2h12c1.1 0 2-.9 2-2V10c0-1.1-.9-2-2-2zm-6 9c-1.1 0-2-.9-2-2s.9-2 2-2 2 .9 2 2-.9 2-2 2zm3.1-9H8.9V6c0-1.71 1.39-3.1 3.1-3.1s3.1 1.39 3.1 3.1v2z"/></svg>
        Local access to your WhatsApp data
      </div>
      <p>Ask me anything about your chats, contacts, and messages. Your data stays on your device.</p>
      <div class="examples">
        <div class="example-item" onclick="askExample(this.querySelector('.example-text').textContent)">
          <div class="example-icon">💬</div>
          <span class="example-text">What were my recent chats?</span>
        </div>
        <div class="example-item" onclick="askExample(this.querySelector('.example-text').textContent)">
          <div class="example-icon">🔍</div>
          <span class="example-text">Find contact Priya and show our recent conversation</span>
        </div>
        <div class="example-item" onclick="askExample(this.querySelector('.example-text').textContent)">
          <div class="example-icon">📨</div>
          <span class="example-text">Search for messages about "meeting" across all chats</span>
        </div>
        <div class="example-item" onclick="askExample(this.querySelector('.example-text').textContent)">
          <div class="example-icon">📊</div>
          <span class="example-text">Show me stats for my most active group</span>
        </div>
      </div>
    </div>
  `;
}

function renderMarkdown(text) {
  if (!text) return '';
  let html = escapeHtml(text);
  // Code blocks
  html = html.replace(/```([\s\S]*?)```/g, '<pre><code>$1</code></pre>');
  // Inline code
  html = html.replace(/`([^`]+)`/g, '<code>$1</code>');
  // Bold
  html = html.replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>');
  // Italic
  html = html.replace(/\*(.+?)\*/g, '<em>$1</em>');
  // Headers
  html = html.replace(/^### (.+)$/gm, '<br><strong>$1</strong>');
  html = html.replace(/^## (.+)$/gm, '<br><strong style="font-size:15px">$1</strong>');
  html = html.replace(/^# (.+)$/gm, '<br><strong style="font-size:16px">$1</strong>');
  // Unordered lists
  html = html.replace(/^[-*] (.+)$/gm, '&bull; $1');
  // Numbered lists
  html = html.replace(/^\d+\. (.+)$/gm, function(match, p1, offset, string) {
    return '&bull; ' + p1;
  });
  // Line breaks
  html = html.replace(/\n/g, '<br>');
  // Clean up excessive breaks
  html = html.replace(/(<br>){3,}/g, '<br><br>');
  return html;
}

function escapeHtml(text) {
  const d = document.createElement('div');
  d.textContent = text;
  return d.innerHTML;
}

// ===== BRIDGE STATUS =====
let bridgeStatus = 'bridge_offline';
let bridgePollingInterval = null;
let qrPollingInterval = null;
let qrModalOpen = false;

const bridgeDot = document.getElementById('bridge-dot');
const bridgeLabel = document.getElementById('bridge-label');

const bridgeLabels = {
  connected: 'Connected',
  qr_pending: 'Scan QR',
  disconnected: 'Disconnected',
  bridge_offline: 'Bridge offline',
};

async function pollBridgeStatus() {
  try {
    const res = await fetch('/api/bridge/status');
    const data = await res.json();
    const newStatus = data.status || 'bridge_offline';
    if (newStatus !== bridgeStatus) {
      bridgeStatus = newStatus;
      updateBridgeUI();
    }
  } catch {
    if (bridgeStatus !== 'bridge_offline') {
      bridgeStatus = 'bridge_offline';
      updateBridgeUI();
    }
  }
}

function updateBridgeUI() {
  bridgeDot.className = 'bridge-dot ' + bridgeStatus;
  bridgeLabel.textContent = bridgeLabels[bridgeStatus] || bridgeStatus;

  // If connected while QR modal is open, close it
  if (bridgeStatus === 'connected' && qrModalOpen) {
    closeQRModal();
  }
}

function onBridgeStatusClick() {
  if (bridgeStatus === 'connected') return;
  openQRModal();
}

function openQRModal() {
  qrModalOpen = true;
  document.getElementById('qr-overlay').classList.add('active');
  fetchQR();
  // Poll QR more frequently while modal is open
  qrPollingInterval = setInterval(fetchQR, 3000);
}

function closeQRModal(event) {
  if (event && event.target !== document.getElementById('qr-overlay') && !event.target.classList.contains('qr-close-btn')) return;
  qrModalOpen = false;
  document.getElementById('qr-overlay').classList.remove('active');
  clearInterval(qrPollingInterval);
  qrPollingInterval = null;
}

async function fetchQR() {
  const container = document.getElementById('qr-container');
  try {
    const res = await fetch('/api/bridge/qr');
    const data = await res.json();
    if (data.qr) {
      container.innerHTML = `<img src="${data.qr}" alt="QR Code">`;
    } else if (data.status === 'connected') {
      container.innerHTML = '<span class="qr-loading" style="color:#25d366">Connected!</span>';
      setTimeout(closeQRModal, 1500);
    } else {
      container.innerHTML = '<span class="qr-loading">Waiting for QR code...<br>Make sure the bridge is running.</span>';
    }
  } catch {
    container.innerHTML = '<span class="qr-loading">Bridge not reachable.<br>Start it with ./run.sh</span>';
  }
}

// Start polling bridge status
pollBridgeStatus();
bridgePollingInterval = setInterval(pollBridgeStatus, 10000);
</script>
</body>
</html>"""
