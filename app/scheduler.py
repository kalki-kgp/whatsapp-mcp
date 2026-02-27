import json
import logging
import sqlite3
import threading
import time
from datetime import datetime, timezone, timedelta

import httpx

from app.config import SCHEDULED_DB, BRIDGE_URL, TEMP_DB_DIR

logger = logging.getLogger(__name__)

_lock = threading.Lock()


def _get_db() -> sqlite3.Connection:
    """Get a connection to the scheduled messages database."""
    TEMP_DB_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(SCHEDULED_DB))
    conn.row_factory = sqlite3.Row
    conn.execute("""
        CREATE TABLE IF NOT EXISTS scheduled_messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            recipient_jid TEXT NOT NULL,
            recipient_name TEXT NOT NULL,
            message TEXT NOT NULL,
            send_at TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            status TEXT NOT NULL DEFAULT 'pending',
            error TEXT
        )
    """)
    conn.commit()
    return conn


def schedule_message(recipient_jid: str, recipient_name: str, message: str, send_at: str) -> dict:
    """Schedule a message for later delivery. send_at is ISO 8601 in local or any timezone."""
    try:
        dt = datetime.fromisoformat(send_at.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            local_tz = datetime.now().astimezone().tzinfo
            dt = dt.replace(tzinfo=local_tz)
    except ValueError:
        return {"success": False, "error": f"Invalid datetime format: {send_at}"}

    dt_utc = dt.astimezone(timezone.utc)
    now_utc = datetime.now(tz=timezone.utc)
    if dt_utc <= now_utc:
        return {"success": False, "error": "Scheduled time must be in the future"}

    with _lock:
        conn = _get_db()
        cursor = conn.execute(
            "INSERT INTO scheduled_messages (recipient_jid, recipient_name, message, send_at) VALUES (?, ?, ?, ?)",
            (recipient_jid, recipient_name, message, dt_utc.isoformat()),
        )
        msg_id = cursor.lastrowid
        conn.commit()
        conn.close()

    dt_local = dt_utc.astimezone()
    return {
        "success": True,
        "id": msg_id,
        "recipient_name": recipient_name,
        "send_at": dt_local.isoformat(),
    }


def list_scheduled() -> list[dict]:
    """List all pending scheduled messages with times in local timezone."""
    with _lock:
        conn = _get_db()
        rows = conn.execute(
            "SELECT id, recipient_jid, recipient_name, message, send_at, status FROM scheduled_messages WHERE status = 'pending' ORDER BY send_at ASC"
        ).fetchall()
        conn.close()
    results = []
    for row in rows:
        d = dict(row)
        try:
            dt_utc = datetime.fromisoformat(d["send_at"])
            d["send_at"] = dt_utc.astimezone().isoformat()
        except (ValueError, KeyError):
            pass
        results.append(d)
    return results


def cancel_scheduled(message_id: int) -> dict:
    """Cancel a pending scheduled message."""
    with _lock:
        conn = _get_db()
        row = conn.execute("SELECT status FROM scheduled_messages WHERE id = ?", (message_id,)).fetchone()
        if not row:
            conn.close()
            return {"success": False, "error": f"No scheduled message with id {message_id}"}
        if row["status"] != "pending":
            conn.close()
            return {"success": False, "error": f"Message {message_id} is already {row['status']}"}
        conn.execute("UPDATE scheduled_messages SET status = 'cancelled' WHERE id = ?", (message_id,))
        conn.commit()
        conn.close()
    return {"success": True, "id": message_id}


def schedule_broadcast(recipients: list[dict], send_at: str, stagger_seconds: int = 45) -> dict:
    """Schedule a personalized message to multiple recipients with staggered send times."""
    if not recipients:
        return {"success": False, "error": "No recipients provided"}
    if len(recipients) > 50:
        return {"success": False, "error": "Too many recipients (max 50)"}

    stagger_seconds = max(15, min(stagger_seconds, 300))

    try:
        dt = datetime.fromisoformat(send_at.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            local_tz = datetime.now().astimezone().tzinfo
            dt = dt.replace(tzinfo=local_tz)
    except ValueError:
        return {"success": False, "error": f"Invalid datetime format: {send_at}"}

    dt_utc = dt.astimezone(timezone.utc)
    now_utc = datetime.now(tz=timezone.utc)
    if dt_utc <= now_utc:
        return {"success": False, "error": "Scheduled time must be in the future"}

    scheduled = []
    with _lock:
        conn = _get_db()
        for i, r in enumerate(recipients):
            jid = r.get("recipient_jid", "")
            name = r.get("recipient_name", "")
            message = r.get("message", "")
            if not jid or not message:
                continue
            offset = timedelta(seconds=i * stagger_seconds)
            msg_time = (dt_utc + offset).isoformat()
            cursor = conn.execute(
                "INSERT INTO scheduled_messages (recipient_jid, recipient_name, message, send_at) VALUES (?, ?, ?, ?)",
                (jid, name, message, msg_time),
            )
            local_time = (dt_utc + offset).astimezone().isoformat()
            scheduled.append({"id": cursor.lastrowid, "recipient_name": name, "send_at": local_time})
        conn.commit()
        conn.close()

    return {
        "success": True,
        "count": len(scheduled),
        "stagger_seconds": stagger_seconds,
        "messages": scheduled,
    }


def _send_due_messages() -> None:
    """Check for and send any due messages."""
    now_utc = datetime.now(tz=timezone.utc)

    with _lock:
        conn = _get_db()
        pending = conn.execute(
            "SELECT id, recipient_jid, recipient_name, message, send_at FROM scheduled_messages WHERE status = 'pending'"
        ).fetchall()
        conn.close()

    rows = []
    for row in pending:
        try:
            send_dt = datetime.fromisoformat(row["send_at"])
            if send_dt.astimezone(timezone.utc) <= now_utc:
                rows.append(row)
        except (ValueError, TypeError):
            rows.append(row)

    for row in rows:
        msg_id = row["id"]
        try:
            resp = httpx.post(
                f"{BRIDGE_URL}/api/send",
                json={"recipient": row["recipient_jid"], "message": row["message"]},
                timeout=15,
            )
            if resp.status_code == 200:
                with _lock:
                    conn = _get_db()
                    conn.execute("UPDATE scheduled_messages SET status = 'sent' WHERE id = ?", (msg_id,))
                    conn.commit()
                    conn.close()
                logger.info(f"Scheduled message {msg_id} sent to {row['recipient_name']}")
            else:
                error = resp.json().get("error", "Unknown error")
                with _lock:
                    conn = _get_db()
                    conn.execute("UPDATE scheduled_messages SET status = 'failed', error = ? WHERE id = ?", (error, msg_id))
                    conn.commit()
                    conn.close()
                logger.error(f"Scheduled message {msg_id} failed: {error}")
        except Exception as e:
            with _lock:
                conn = _get_db()
                conn.execute("UPDATE scheduled_messages SET status = 'failed', error = ? WHERE id = ?", (str(e), msg_id))
                conn.commit()
                conn.close()
            logger.error(f"Scheduled message {msg_id} error: {e}")


_scheduler_thread: threading.Thread | None = None
_scheduler_running = False


def start_scheduler() -> None:
    """Start the background scheduler thread."""
    global _scheduler_thread, _scheduler_running
    if _scheduler_running:
        return

    _scheduler_running = True

    def _loop():
        # Initialize DB
        _get_db().close()
        while _scheduler_running:
            try:
                _send_due_messages()
            except Exception as e:
                logger.error(f"Scheduler error: {e}")
            time.sleep(30)

    _scheduler_thread = threading.Thread(target=_loop, daemon=True)
    _scheduler_thread.start()
    logger.info("Scheduled message checker started (every 30s)")


def stop_scheduler() -> None:
    """Stop the background scheduler thread."""
    global _scheduler_running
    _scheduler_running = False
