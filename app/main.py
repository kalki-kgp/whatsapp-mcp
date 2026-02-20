import json
import logging
import uuid
from datetime import datetime, timezone

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from app.agent import chat, chat_sync
from app.db import refresh_db
from app.config import SERVER_PORT

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
# Inline HTML page — single-file chat UI
# ---------------------------------------------------------------------------
HTML_PAGE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>WhatsApp Assistant</title>
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body {
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
    background: #0b141a;
    color: #e9edef;
    height: 100vh;
    display: flex;
    flex-direction: column;
  }
  .header {
    background: #202c33;
    padding: 12px 20px;
    display: flex;
    align-items: center;
    gap: 12px;
    border-bottom: 1px solid #2a3942;
  }
  .header .avatar {
    width: 40px; height: 40px;
    background: #00a884;
    border-radius: 50%;
    display: flex; align-items: center; justify-content: center;
    font-size: 20px;
  }
  .header .info h2 { font-size: 16px; font-weight: 500; }
  .header .info p { font-size: 12px; color: #8696a0; }
  .messages {
    flex: 1;
    overflow-y: auto;
    padding: 20px;
    display: flex;
    flex-direction: column;
    gap: 8px;
    background: #0b141a url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='200' height='200'%3E%3Crect fill='%230b141a'/%3E%3Cg opacity='.03' fill='%23fff'%3E%3Ccircle cx='20' cy='20' r='2'/%3E%3Ccircle cx='100' cy='60' r='1.5'/%3E%3Ccircle cx='160' cy='30' r='1'/%3E%3Ccircle cx='60' cy='120' r='2'/%3E%3Ccircle cx='140' cy='150' r='1.5'/%3E%3C/g%3E%3C/svg%3E");
  }
  .msg {
    max-width: 65%;
    padding: 8px 12px;
    border-radius: 8px;
    font-size: 14px;
    line-height: 1.5;
    position: relative;
    word-wrap: break-word;
  }
  .msg.user {
    background: #005c4b;
    align-self: flex-end;
    border-bottom-right-radius: 0;
  }
  .msg.assistant {
    background: #202c33;
    align-self: flex-start;
    border-bottom-left-radius: 0;
  }
  .msg .time {
    font-size: 11px;
    color: rgba(255,255,255,0.45);
    text-align: right;
    margin-top: 4px;
  }
  .msg .tool-indicator {
    font-size: 12px;
    color: #00a884;
    padding: 4px 0;
    display: flex;
    align-items: center;
    gap: 6px;
  }
  .msg .tool-indicator .spinner {
    width: 12px; height: 12px;
    border: 2px solid #00a884;
    border-top-color: transparent;
    border-radius: 50%;
    animation: spin 0.8s linear infinite;
  }
  @keyframes spin { to { transform: rotate(360deg); } }
  .tool-calls {
    margin-top: 8px;
    padding: 8px;
    background: rgba(0,168,132,0.1);
    border-radius: 6px;
    font-size: 12px;
    color: #8696a0;
  }
  .tool-calls summary {
    cursor: pointer;
    color: #00a884;
    font-size: 12px;
  }
  .tool-calls pre {
    margin-top: 6px;
    white-space: pre-wrap;
    font-size: 11px;
    max-height: 200px;
    overflow-y: auto;
  }
  .input-area {
    background: #202c33;
    padding: 10px 20px;
    display: flex;
    gap: 10px;
    align-items: center;
  }
  .input-area input {
    flex: 1;
    background: #2a3942;
    border: none;
    outline: none;
    color: #e9edef;
    padding: 10px 16px;
    border-radius: 8px;
    font-size: 14px;
  }
  .input-area input::placeholder { color: #8696a0; }
  .input-area button {
    background: #00a884;
    border: none;
    color: #111;
    width: 42px; height: 42px;
    border-radius: 50%;
    cursor: pointer;
    font-size: 18px;
    display: flex; align-items: center; justify-content: center;
    transition: background 0.2s;
  }
  .input-area button:hover { background: #06cf9c; }
  .input-area button:disabled { background: #2a3942; cursor: not-allowed; }
  .msg-content { white-space: pre-wrap; }
  .msg-content p { margin: 0.4em 0; }
  .msg-content code {
    background: rgba(255,255,255,0.1);
    padding: 1px 4px;
    border-radius: 3px;
    font-size: 13px;
  }
  .msg-content pre code {
    display: block;
    padding: 8px;
    overflow-x: auto;
  }
  .msg-content strong { color: #fff; }
  .msg-content ul, .msg-content ol { padding-left: 20px; margin: 4px 0; }
  .welcome {
    text-align: center;
    color: #8696a0;
    margin: auto;
    max-width: 400px;
  }
  .welcome h3 { color: #e9edef; margin-bottom: 8px; }
  .welcome p { font-size: 14px; margin-bottom: 16px; }
  .welcome .examples { text-align: left; font-size: 13px; }
  .welcome .examples div {
    background: #202c33;
    padding: 10px 14px;
    border-radius: 8px;
    margin: 6px 0;
    cursor: pointer;
    transition: background 0.2s;
  }
  .welcome .examples div:hover { background: #2a3942; }
</style>
</head>
<body>
<div class="header">
  <div class="avatar">W</div>
  <div class="info">
    <h2>WhatsApp Assistant</h2>
    <p id="status-text">Ready</p>
  </div>
</div>
<div class="messages" id="messages">
  <div class="welcome">
    <h3>WhatsApp Assistant</h3>
    <p>Ask me anything about your WhatsApp chats, contacts, and messages.</p>
    <div class="examples">
      <div onclick="askExample(this.innerText)">What were my recent chats?</div>
      <div onclick="askExample(this.innerText)">Find contact Priya and show our recent conversation</div>
      <div onclick="askExample(this.innerText)">Search for messages about "meeting" across all chats</div>
      <div onclick="askExample(this.innerText)">Show me stats for my most active group</div>
    </div>
  </div>
</div>
<div class="input-area">
  <input type="text" id="input" placeholder="Ask about your WhatsApp..." autofocus>
  <button id="send-btn" onclick="sendMessage()">&#10148;</button>
</div>
<script>
let conversationId = null;
let sending = false;

const messagesEl = document.getElementById('messages');
const inputEl = document.getElementById('input');
const sendBtn = document.getElementById('send-btn');
const statusText = document.getElementById('status-text');

inputEl.addEventListener('keydown', e => { if (e.key === 'Enter' && !sending) sendMessage(); });

function askExample(text) { inputEl.value = text; sendMessage(); }

function addMessage(role, content, toolCalls) {
  // Remove welcome screen if present
  const welcome = messagesEl.querySelector('.welcome');
  if (welcome) welcome.remove();

  const div = document.createElement('div');
  div.className = `msg ${role}`;

  const contentDiv = document.createElement('div');
  contentDiv.className = 'msg-content';
  contentDiv.innerHTML = renderMarkdown(content);
  div.appendChild(contentDiv);

  if (toolCalls && toolCalls.length > 0) {
    const toolDiv = document.createElement('div');
    toolDiv.className = 'tool-calls';
    const details = document.createElement('details');
    const summary = document.createElement('summary');
    summary.textContent = `${toolCalls.length} tool call(s) used`;
    details.appendChild(summary);
    toolCalls.forEach(tc => {
      const pre = document.createElement('pre');
      let text = `${tc.name}(${JSON.stringify(tc.arguments, null, 2)})`;
      pre.textContent = text;
      details.appendChild(pre);
    });
    toolDiv.appendChild(details);
    div.appendChild(toolDiv);
  }

  const timeDiv = document.createElement('div');
  timeDiv.className = 'time';
  timeDiv.textContent = new Date().toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
  div.appendChild(timeDiv);

  messagesEl.appendChild(div);
  messagesEl.scrollTop = messagesEl.scrollHeight;
  return div;
}

function addThinking() {
  const welcome = messagesEl.querySelector('.welcome');
  if (welcome) welcome.remove();

  const div = document.createElement('div');
  div.className = 'msg assistant';
  div.id = 'thinking-msg';
  div.innerHTML = '<div class="tool-indicator"><div class="spinner"></div> Thinking...</div>';
  messagesEl.appendChild(div);
  messagesEl.scrollTop = messagesEl.scrollHeight;
  return div;
}

function updateThinking(text) {
  const el = document.getElementById('thinking-msg');
  if (el) {
    el.innerHTML = `<div class="tool-indicator"><div class="spinner"></div> ${escapeHtml(text)}</div>`;
    messagesEl.scrollTop = messagesEl.scrollHeight;
  }
}

function removeThinking() {
  const el = document.getElementById('thinking-msg');
  if (el) el.remove();
}

async function sendMessage() {
  const text = inputEl.value.trim();
  if (!text || sending) return;

  sending = true;
  sendBtn.disabled = true;
  inputEl.value = '';
  statusText.textContent = 'Thinking...';

  addMessage('user', text);
  addThinking();

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
    let finalContent = '';

    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });

      const lines = buffer.split('\\n');
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
            updateThinking(`Calling ${event.name}...`);
            statusText.textContent = `Using tool: ${event.name}`;
          } else if (event.type === 'tool_result') {
            updateThinking('Processing results...');
          } else if (event.type === 'message') {
            finalContent = event.content;
          } else if (event.type === 'error') {
            finalContent = `Error: ${event.content}`;
          }
        } catch(e) {}
      }
    }

    removeThinking();
    if (finalContent) {
      addMessage('assistant', finalContent, toolCalls);
    }
  } catch (err) {
    removeThinking();
    addMessage('assistant', `Error: ${err.message}`);
  }

  sending = false;
  sendBtn.disabled = false;
  statusText.textContent = 'Ready';
  inputEl.focus();
}

function renderMarkdown(text) {
  if (!text) return '';
  // Basic markdown rendering
  let html = escapeHtml(text);
  // Code blocks
  html = html.replace(/```([\\s\\S]*?)```/g, '<pre><code>$1</code></pre>');
  // Inline code
  html = html.replace(/`([^`]+)`/g, '<code>$1</code>');
  // Bold
  html = html.replace(/\\*\\*(.+?)\\*\\*/g, '<strong>$1</strong>');
  // Italic
  html = html.replace(/\\*(.+?)\\*/g, '<em>$1</em>');
  // Headers
  html = html.replace(/^### (.+)$/gm, '<strong>$1</strong>');
  html = html.replace(/^## (.+)$/gm, '<strong>$1</strong>');
  html = html.replace(/^# (.+)$/gm, '<strong>$1</strong>');
  // Line breaks
  html = html.replace(/\\n/g, '<br>');
  return html;
}

function escapeHtml(text) {
  const d = document.createElement('div');
  d.textContent = text;
  return d.innerHTML;
}
</script>
</body>
</html>"""
