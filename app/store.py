import json
import sqlite3
import uuid
import logging
from datetime import datetime, timezone

from app.config import CONVERSATIONS_DB

logger = logging.getLogger(__name__)


def _conn():
    CONVERSATIONS_DB.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(CONVERSATIONS_DB))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db():
    with _conn() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS conversations (
                id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                conversation_id TEXT NOT NULL,
                role TEXT NOT NULL,
                content TEXT,
                tool_calls TEXT,
                tool_call_id TEXT,
                created_at TEXT NOT NULL,
                FOREIGN KEY (conversation_id) REFERENCES conversations(id) ON DELETE CASCADE
            );
            CREATE INDEX IF NOT EXISTS idx_messages_conv
                ON messages(conversation_id);
        """)


def _now():
    return datetime.now(tz=timezone.utc).isoformat()


def create_conversation(title: str = "New Chat") -> str:
    conv_id = str(uuid.uuid4())
    now = _now()
    with _conn() as conn:
        conn.execute(
            "INSERT INTO conversations (id, title, created_at, updated_at) VALUES (?, ?, ?, ?)",
            (conv_id, title, now, now),
        )
    return conv_id


def list_conversations() -> list[dict]:
    with _conn() as conn:
        rows = conn.execute(
            """SELECT c.id, c.title, c.created_at, c.updated_at,
                      (SELECT content FROM messages
                       WHERE conversation_id = c.id AND role = 'user'
                       ORDER BY id DESC LIMIT 1) AS last_user_msg,
                      (SELECT COUNT(*) FROM messages
                       WHERE conversation_id = c.id AND role = 'user') AS msg_count
               FROM conversations c
               ORDER BY c.updated_at DESC"""
        ).fetchall()
    return [
        {
            "id": r["id"],
            "title": r["title"],
            "created_at": r["created_at"],
            "updated_at": r["updated_at"],
            "preview": (r["last_user_msg"] or "")[:80],
            "msg_count": r["msg_count"],
        }
        for r in rows
    ]


def get_messages(conversation_id: str) -> list[dict]:
    """Get all messages for a conversation in OpenAI API format."""
    with _conn() as conn:
        rows = conn.execute(
            "SELECT role, content, tool_calls, tool_call_id FROM messages "
            "WHERE conversation_id = ? ORDER BY id",
            (conversation_id,),
        ).fetchall()

    messages = []
    for r in rows:
        msg: dict = {"role": r["role"], "content": r["content"]}
        if r["tool_calls"]:
            msg["tool_calls"] = json.loads(r["tool_calls"])
        if r["tool_call_id"]:
            msg["tool_call_id"] = r["tool_call_id"]
        messages.append(msg)

    # Clean up incomplete tool-call chains at the tail (crash recovery)
    while messages and messages[-1].get("role") == "tool":
        messages.pop()
    while messages and messages[-1].get("role") == "assistant" and messages[-1].get("tool_calls"):
        messages.pop()

    return messages


def save_message(conversation_id: str, message: dict):
    now = _now()
    role = message.get("role", "")
    content = message.get("content")
    tool_calls = (
        json.dumps(message["tool_calls"])
        if message.get("tool_calls")
        else None
    )
    tool_call_id = message.get("tool_call_id")

    with _conn() as conn:
        conn.execute(
            "INSERT INTO messages (conversation_id, role, content, tool_calls, tool_call_id, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (conversation_id, role, content, tool_calls, tool_call_id, now),
        )
        conn.execute(
            "UPDATE conversations SET updated_at = ? WHERE id = ?",
            (now, conversation_id),
        )


def save_messages(conversation_id: str, messages: list[dict]):
    for msg in messages:
        save_message(conversation_id, msg)


def delete_conversation(conversation_id: str):
    with _conn() as conn:
        conn.execute("DELETE FROM messages WHERE conversation_id = ?", (conversation_id,))
        conn.execute("DELETE FROM conversations WHERE id = ?", (conversation_id,))


def rename_conversation(conversation_id: str, title: str):
    with _conn() as conn:
        conn.execute(
            "UPDATE conversations SET title = ?, updated_at = ? WHERE id = ?",
            (title, _now(), conversation_id),
        )


def conversation_exists(conversation_id: str) -> bool:
    with _conn() as conn:
        row = conn.execute(
            "SELECT 1 FROM conversations WHERE id = ?", (conversation_id,)
        ).fetchone()
    return row is not None


def auto_title(text: str) -> str:
    """Generate a conversation title from the first user message."""
    text = text.strip()
    for prefix in ("can you ", "could you ", "please ", "hey ", "hi ", "hello "):
        if text.lower().startswith(prefix):
            text = text[len(prefix):]
    text = (text[0].upper() + text[1:]) if text else "New Chat"
    if len(text) > 40:
        truncated = text[:40]
        last_space = truncated.rfind(" ")
        if last_space > 20:
            truncated = truncated[:last_space]
        text = truncated + "…"
    return text or "New Chat"
