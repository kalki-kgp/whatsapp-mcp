import json
import logging
import subprocess
from pathlib import Path

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, StreamingResponse

from app.agent import chat, chat_sync
from app.db import refresh_db
from app.config import SERVER_PORT, BRIDGE_URL
from app.scheduler import start_scheduler, list_scheduled
from app.settings import get_settings, update_settings
from app import store
from app import rewriter

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="WhatsApp MCP", version="0.3.0")

# Voice event stream — voice assistant pushes events here, UI polls them
voice_events: list[dict] = []
voice_event_id: int = 0


@app.on_event("startup")
async def startup_event():
    store.init_db()
    start_scheduler()


@app.get("/api/health")
async def health():
    return {"status": "ok", "version": app.version}


_STATIC_DIR = Path(__file__).parent / "static"

@app.get("/", response_class=HTMLResponse)
async def index():
    return (_STATIC_DIR / "index.html").read_text()


# ---------------------------------------------------------------------------
# Conversation CRUD
# ---------------------------------------------------------------------------

@app.get("/api/conversations")
async def api_list_conversations():
    return {"conversations": store.list_conversations()}


@app.post("/api/conversations")
async def api_create_conversation(request: Request):
    body = await request.json()
    title = body.get("title", "New Chat")
    conv_id = store.create_conversation(title)
    return {"id": conv_id, "title": title}


@app.get("/api/conversations/{conv_id}")
async def api_get_conversation(conv_id: str):
    if not store.conversation_exists(conv_id):
        return {"error": "Not found"}, 404
    messages = store.get_messages(conv_id)
    return {"conversation_id": conv_id, "messages": messages}


@app.delete("/api/conversations/{conv_id}")
async def api_delete_conversation(conv_id: str):
    store.delete_conversation(conv_id)
    return {"status": "ok"}


@app.patch("/api/conversations/{conv_id}")
async def api_rename_conversation(conv_id: str, request: Request):
    body = await request.json()
    title = body.get("title", "").strip()
    if title:
        store.rename_conversation(conv_id, title)
    return {"status": "ok"}


# ---------------------------------------------------------------------------
# Chat endpoints (now backed by store)
# ---------------------------------------------------------------------------

MAX_MESSAGE_LENGTH = 10000

@app.post("/api/chat")
async def api_chat(request: Request):
    """Chat endpoint. Accepts {"message": str, "conversation_id": str?}"""
    body = await request.json()
    user_message = body.get("message", "").strip()
    conv_id = body.get("conversation_id")

    if not user_message:
        return {"error": "Empty message"}
    if len(user_message) > MAX_MESSAGE_LENGTH:
        return {"error": f"Message too long (max {MAX_MESSAGE_LENGTH} characters)"}

    if not conv_id or not store.conversation_exists(conv_id):
        conv_id = store.create_conversation(store.auto_title(user_message))

    store.save_message(conv_id, {"role": "user", "content": user_message})
    history = store.get_messages(conv_id)

    result = chat_sync(history)

    store.save_messages(conv_id, result.get("persist_messages", []))
    if result["response"]:
        store.save_message(conv_id, {"role": "assistant", "content": result["response"]})

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
    conv_id = body.get("conversation_id")

    if not user_message:
        return {"error": "Empty message"}
    if len(user_message) > MAX_MESSAGE_LENGTH:
        return {"error": f"Message too long (max {MAX_MESSAGE_LENGTH} characters)"}

    is_new = not conv_id or not store.conversation_exists(conv_id)
    if is_new:
        conv_id = store.create_conversation(store.auto_title(user_message))

    store.save_message(conv_id, {"role": "user", "content": user_message})
    history = store.get_messages(conv_id)

    def generate():
        yield f"data: {json.dumps({'type': 'conv_id', 'conversation_id': conv_id, 'is_new': is_new})}\n\n"
        final_content = ""

        for event in chat(history):
            if event["type"] == "persist":
                store.save_message(conv_id, event["message"])
            else:
                yield f"data: {json.dumps(event)}\n\n"
                if event["type"] == "message":
                    final_content = event["content"]

        if final_content:
            store.save_message(conv_id, {"role": "assistant", "content": final_content})
        yield "data: [DONE]\n\n"

    return StreamingResponse(generate(), media_type="text/event-stream")


@app.post("/api/refresh")
async def api_refresh():
    """Force refresh the WhatsApp database copies."""
    refresh_db()
    return {"status": "ok", "message": "Database copies refreshed"}


@app.post("/api/rewrite")
async def api_rewrite(request: Request):
    """Rewrite a message with a different tone or translate it."""
    body = await request.json()
    text = body.get("text", "").strip()
    tone = body.get("tone", "formal")
    language = body.get("language")

    if not text:
        return {"error": "Empty text"}
    if len(text) > 2000:
        return {"error": "Text too long (max 2000 characters)"}

    try:
        result = rewriter.rewrite(text, tone, language)
        return {"rewritten": result, "tone": tone}
    except Exception as e:
        logger.error(f"Rewrite failed: {e}")
        return {"error": "Rewrite failed"}


# ---------------------------------------------------------------------------
# Bridge proxy endpoints
# ---------------------------------------------------------------------------

@app.get("/api/bridge/status")
async def bridge_status():
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
    messages = list_scheduled()
    return {"scheduled_messages": messages, "count": len(messages)}


# ---------------------------------------------------------------------------
# Settings endpoints
# ---------------------------------------------------------------------------

@app.get("/api/settings")
async def api_get_settings():
    return get_settings()


@app.put("/api/settings")
async def api_put_settings(request: Request):
    body = await request.json()
    updated = update_settings(body)
    return updated


@app.get("/api/tts-voices")
async def api_tts_voices():
    try:
        result = subprocess.run(
            ["say", "-v", "?"], capture_output=True, text=True, timeout=5
        )
        voices = []
        for line in result.stdout.strip().splitlines():
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
    body = await request.json()
    voice = body.get("voice", "Samantha")
    speed = body.get("speed", 190)
    text = body.get("text", f"Hi, I'm {voice}. How can I help you today?")
    import re
    if not isinstance(voice, str) or not re.match(r'^[A-Za-z .\-()]+$', voice) or len(voice) > 64:
        return {"status": "error", "message": "Invalid voice name"}
    if not isinstance(speed, (int, float)) or not (50 <= int(speed) <= 500):
        return {"status": "error", "message": "Speed must be between 50 and 500"}
    if not isinstance(text, str) or len(text) > 500:
        return {"status": "error", "message": "Text too long (max 500 characters)"}
    try:
        subprocess.Popen(
            ["say", "-v", voice, "-r", str(int(speed)), text],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        return {"status": "ok"}
    except Exception as e:
        return {"status": "error", "message": "TTS failed"}


# ---------------------------------------------------------------------------
# Voice event stream
# ---------------------------------------------------------------------------

VOICE_EVENT_TYPES = {"voice_user", "voice_assistant", "voice_tool_call", "voice_tool_result"}

@app.post("/api/voice/event")
async def api_voice_event(request: Request):
    global voice_event_id
    body = await request.json()
    if not isinstance(body, dict) or body.get("type") not in VOICE_EVENT_TYPES:
        return {"error": "Invalid event type"}
    event = {"type": body["type"]}
    if "text" in body and isinstance(body["text"], str):
        event["text"] = body["text"][:MAX_MESSAGE_LENGTH]
    if "name" in body and isinstance(body["name"], str):
        event["name"] = body["name"][:100]
    if "tool_calls" in body and isinstance(body["tool_calls"], list):
        event["tool_calls"] = body["tool_calls"][:20]
    voice_event_id += 1
    event["id"] = voice_event_id
    voice_events.append(event)
    if len(voice_events) > 200:
        del voice_events[:len(voice_events) - 200]
    return {"id": voice_event_id}


@app.get("/api/voice/events")
async def api_voice_events(after: int = 0):
    new = [e for e in voice_events if e["id"] > after]
    return {"events": new}


