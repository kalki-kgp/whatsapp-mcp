"""WhatsApp tool implementations."""

import json
import re
from datetime import datetime, timezone, timedelta
from typing import Optional

import httpx

from whatsapp_mcp.config import BRIDGE_URL
from whatsapp_mcp.db import (
    get_chat_db,
    get_contacts_db,
    refresh_db,
    apple_ts_to_datetime,
    datetime_to_apple_ts,
    format_dt,
)

# Message type labels
MESSAGE_TYPES = {
    0: "text",
    1: "image",
    2: "video",
    3: "voice_note",
    4: "contact",
    5: "location",
    6: "system",
    7: "link",
    8: "document",
    10: "deleted",
    14: "deleted_by_admin",
    15: "sticker",
}


# ZWAMESSAGE.ZPUSHNAME holds encoded Core Data blobs, not readable names, so
# sender names come from the profile push-name table instead. In groups the
# sender is ZGROUPMEMBER (ZFROMJID is the group itself); in DMs it's ZFROMJID.
SENDER_JOINS = """
            LEFT JOIN ZWAGROUPMEMBER gm ON gm.Z_PK = m.ZGROUPMEMBER
            LEFT JOIN ZWAPROFILEPUSHNAME gpn ON gpn.ZJID = gm.ZMEMBERJID
            LEFT JOIN ZWAPROFILEPUSHNAME fpn ON fpn.ZJID = m.ZFROMJID
"""

SENDER_COLUMNS = """
                gm.ZMEMBERJID as member_jid,
                gm.ZCONTACTNAME as member_name,
                gpn.ZPUSHNAME as group_push_name,
                fpn.ZPUSHNAME as from_push_name,
"""


def _resolve_sender(row, partner_name: Optional[str] = None) -> str:
    """Best readable name for a message's sender."""
    if row["is_from_me"]:
        return "You"
    for key in ("group_push_name", "member_name", "from_push_name"):
        if row[key]:
            return row[key]
    # DMs: the chat partner is the sender
    if partner_name:
        return partner_name
    return row["member_jid"] or row["from_jid"] or "Unknown"


def _sender_jid(row) -> Optional[str]:
    """The sender's own JID - for groups that's the member, not the group."""
    if row["is_from_me"]:
        return None
    return row["member_jid"] or row["from_jid"]


def _parse_iso_datetime(dt_str: Optional[str]) -> Optional[datetime]:
    """Parse an ISO 8601 datetime string."""
    if not dt_str:
        return None
    try:
        dt = datetime.fromisoformat(dt_str.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Contact tools
# ---------------------------------------------------------------------------


def search_contacts(query: str) -> str:
    """Search WhatsApp contacts by name or phone number."""
    query = query.strip().lower()
    if not query:
        return json.dumps({"error": "Empty search query"})

    results = []
    try:
        conn = get_contacts_db()
        cursor = conn.execute("""
            SELECT ZWHATSAPPID, ZFULLNAME, ZPHONENUMBER, ZBUSINESSNAME
            FROM ZWAADDRESSBOOKCONTACT
            WHERE ZFULLNAME LIKE ? OR ZPHONENUMBER LIKE ? OR ZBUSINESSNAME LIKE ?
            LIMIT 20
        """, (f"%{query}%", f"%{query}%", f"%{query}%"))

        for row in cursor:
            jid = row["ZWHATSAPPID"]
            if not jid:
                continue
            results.append({
                "jid": jid if "@" in jid else f"{jid}@s.whatsapp.net",
                "name": row["ZFULLNAME"] or row["ZBUSINESSNAME"] or "Unknown",
                "phone": row["ZPHONENUMBER"],
            })
        conn.close()
    except Exception as e:
        return json.dumps({"error": f"Database error: {str(e)}"})

    return json.dumps({"contacts": results, "count": len(results)})


# ---------------------------------------------------------------------------
# Chat tools
# ---------------------------------------------------------------------------


def list_recent_chats(limit: int = 20, chat_type: str = "all") -> str:
    """List recent WhatsApp chats ordered by last message time."""
    limit = max(1, min(limit, 50))

    try:
        conn = get_chat_db()
        cursor = conn.execute("""
            SELECT
                cs.ZCONTACTJID as jid,
                cs.ZPARTNERNAME as name,
                cs.ZLASTMESSAGEDATE as last_msg_date,
                cs.ZUNREADCOUNT as unread,
                m.ZTEXT as last_msg,
                m.ZMESSAGETYPE as last_msg_type
            FROM ZWACHATSESSION cs
            LEFT JOIN ZWAMESSAGE m ON m.Z_PK = cs.ZLASTMESSAGE
            WHERE cs.ZCONTACTJID IS NOT NULL
            ORDER BY cs.ZLASTMESSAGEDATE DESC
            LIMIT ?
        """, (limit * 2,))  # Fetch extra for filtering

        chats = []
        for row in cursor:
            jid = row["jid"]
            if not jid:
                continue

            is_group = jid.endswith("@g.us")
            if chat_type == "dm" and is_group:
                continue
            if chat_type == "group" and not is_group:
                continue

            last_dt = apple_ts_to_datetime(row["last_msg_date"])

            # Media messages carry no text - label them by type instead
            last_msg = row["last_msg"] or ""
            if not last_msg and row["last_msg_type"] is not None:
                last_type = MESSAGE_TYPES.get(
                    row["last_msg_type"], f"type_{row['last_msg_type']}"
                )
                if last_type != "text":
                    last_msg = f"[{last_type}]"

            chats.append({
                "jid": jid,
                "name": row["name"] or "Unknown",
                "type": "group" if is_group else "dm",
                "unread_count": row["unread"] or 0,
                "last_message": last_msg[:100],
                "last_message_time": format_dt(last_dt),
            })

            if len(chats) >= limit:
                break

        conn.close()
    except Exception as e:
        return json.dumps({"error": f"Database error: {str(e)}"})

    return json.dumps({"chats": chats, "count": len(chats)})


def get_messages(
    chat_jid: str,
    after: Optional[str] = None,
    before: Optional[str] = None,
    limit: int = 50,
    search_text: Optional[str] = None,
) -> str:
    """Get messages from a specific WhatsApp chat."""
    limit = max(1, min(limit, 200))

    # Parse date filters
    after_dt = _parse_iso_datetime(after)
    before_dt = _parse_iso_datetime(before)

    if after_dt is None:
        after_dt = datetime.now(tz=timezone.utc) - timedelta(days=1)
    if before_dt is None:
        before_dt = datetime.now(tz=timezone.utc)

    after_ts = datetime_to_apple_ts(after_dt)
    before_ts = datetime_to_apple_ts(before_dt)

    try:
        conn = get_chat_db()

        # Get chat session info
        session = conn.execute("""
            SELECT ZPARTNERNAME FROM ZWACHATSESSION WHERE ZCONTACTJID = ?
        """, (chat_jid,)).fetchone()
        chat_name = session["ZPARTNERNAME"] if session else "Unknown"
        # In a group, ZPARTNERNAME is the group name - not a usable sender name
        dm_partner = None if chat_jid.endswith("@g.us") else chat_name

        # Get messages
        query = """
            SELECT
                m.ZMESSAGEDATE as msg_date,
                m.ZTEXT as text,
                m.ZMESSAGETYPE as msg_type,
                m.ZFROMJID as from_jid,
                m.ZISFROMME as is_from_me,
""" + SENDER_COLUMNS + """
                m.ZSTARRED as starred
            FROM ZWAMESSAGE m
            JOIN ZWACHATSESSION cs ON m.ZCHATSESSION = cs.Z_PK
""" + SENDER_JOINS + """
            WHERE cs.ZCONTACTJID = ?
              AND m.ZMESSAGEDATE >= ?
              AND m.ZMESSAGEDATE <= ?
        """
        params = [chat_jid, after_ts, before_ts]

        if search_text:
            query += " AND m.ZTEXT LIKE ?"
            params.append(f"%{search_text}%")

        query += " ORDER BY m.ZMESSAGEDATE ASC LIMIT ?"
        params.append(limit + 1)  # +1 to check if there are more

        cursor = conn.execute(query, params)
        messages = []

        for row in cursor:
            msg_dt = apple_ts_to_datetime(row["msg_date"])
            msg_type = MESSAGE_TYPES.get(row["msg_type"], f"type_{row['msg_type']}")
            text = row["text"]

            # Clean up text for non-text types
            if msg_type != "text" and not text:
                text = f"[{msg_type}]"

            messages.append({
                "time": format_dt(msg_dt),
                "timestamp": int(msg_dt.timestamp()) if msg_dt else 0,
                "sender": _resolve_sender(row, dm_partner),
                "sender_jid": _sender_jid(row),
                "type": msg_type,
                "starred": bool(row["starred"]),
                "text": text,
            })

        has_more = len(messages) > limit
        if has_more:
            messages = messages[:limit]

        conn.close()
    except Exception as e:
        return json.dumps({"error": f"Database error: {str(e)}"})

    return json.dumps({
        "chat_name": chat_name,
        "chat_jid": chat_jid,
        "chat_type": "group" if chat_jid.endswith("@g.us") else "dm",
        "time_range": {
            "after": format_dt(after_dt),
            "before": format_dt(before_dt),
        },
        "messages": messages,
        "count": len(messages),
        "has_more": has_more,
    })


def search_messages(query: str, chat_jid: Optional[str] = None, limit: int = 20) -> str:
    """Search for messages containing specific text."""
    query = query.strip()
    if not query:
        return json.dumps({"error": "Empty search query"})

    limit = max(1, min(limit, 50))

    try:
        conn = get_chat_db()

        sql = """
            SELECT
                m.ZMESSAGEDATE as msg_date,
                m.ZTEXT as text,
                m.ZFROMJID as from_jid,
                m.ZISFROMME as is_from_me,
""" + SENDER_COLUMNS + """
                cs.ZCONTACTJID as chat_jid,
                cs.ZPARTNERNAME as chat_name
            FROM ZWAMESSAGE m
            JOIN ZWACHATSESSION cs ON m.ZCHATSESSION = cs.Z_PK
""" + SENDER_JOINS + """
            WHERE m.ZTEXT LIKE ?
        """
        params = [f"%{query}%"]

        if chat_jid:
            sql += " AND cs.ZCONTACTJID = ?"
            params.append(chat_jid)

        sql += " ORDER BY m.ZMESSAGEDATE DESC LIMIT ?"
        params.append(limit)

        cursor = conn.execute(sql, params)
        results = []

        for row in cursor:
            msg_dt = apple_ts_to_datetime(row["msg_date"])
            results.append({
                "chat_jid": row["chat_jid"],
                "chat_name": row["chat_name"] or "Unknown",
                "time": format_dt(msg_dt),
                "sender": _resolve_sender(
                    row,
                    None if (row["chat_jid"] or "").endswith("@g.us") else row["chat_name"],
                ),
                "text": row["text"],
            })

        conn.close()
    except Exception as e:
        return json.dumps({"error": f"Database error: {str(e)}"})

    return json.dumps({"query": query, "results": results, "count": len(results)})


def get_unread_summary(max_chats: int = 10, messages_per_chat: int = 5) -> str:
    """Get a summary of all chats with unread messages."""
    max_chats = max(1, min(max_chats, 20))
    messages_per_chat = max(1, min(messages_per_chat, 10))

    try:
        conn = get_chat_db()

        cursor = conn.execute("""
            SELECT
                cs.ZCONTACTJID as jid,
                cs.ZPARTNERNAME as name,
                cs.ZUNREADCOUNT as unread
            FROM ZWACHATSESSION cs
            WHERE cs.ZUNREADCOUNT > 0
            ORDER BY cs.ZLASTMESSAGEDATE DESC
            LIMIT ?
        """, (max_chats,))

        chats = []
        for row in cursor:
            jid = row["jid"]
            if not jid:
                continue

            # Get recent messages for this chat
            msg_cursor = conn.execute("""
                SELECT
                    m.ZMESSAGEDATE as msg_date,
                    m.ZTEXT as text,
                    m.ZMESSAGETYPE as msg_type,
                    m.ZFROMJID as from_jid,
                    m.ZISFROMME as is_from_me,
""" + SENDER_COLUMNS.rstrip().rstrip(",") + """
                FROM ZWAMESSAGE m
                JOIN ZWACHATSESSION cs ON m.ZCHATSESSION = cs.Z_PK
""" + SENDER_JOINS + """
                WHERE cs.ZCONTACTJID = ?
                ORDER BY m.ZMESSAGEDATE DESC
                LIMIT ?
            """, (jid, messages_per_chat))

            messages = []
            for msg in msg_cursor:
                msg_dt = apple_ts_to_datetime(msg["msg_date"])
                text = msg["text"] or ""
                if not text and msg["msg_type"] is not None:
                    msg_type = MESSAGE_TYPES.get(
                        msg["msg_type"], f"type_{msg['msg_type']}"
                    )
                    if msg_type != "text":
                        text = f"[{msg_type}]"

                messages.append({
                    "time": format_dt(msg_dt),
                    "sender": _resolve_sender(
                        msg, None if jid.endswith("@g.us") else row["name"]
                    ),
                    "text": text[:200],
                })

            chats.append({
                "jid": jid,
                "name": row["name"] or "Unknown",
                "type": "group" if jid.endswith("@g.us") else "dm",
                "unread_count": row["unread"],
                "recent_messages": list(reversed(messages)),
            })

        conn.close()
    except Exception as e:
        return json.dumps({"error": f"Database error: {str(e)}"})

    total_unread = sum(c["unread_count"] for c in chats)
    return json.dumps({
        "total_unread": total_unread,
        "chats": chats,
        "count": len(chats),
    })


# ---------------------------------------------------------------------------
# Bridge tools (require WhatsApp Web connection)
# ---------------------------------------------------------------------------


def get_bridge_status() -> dict:
    """Get bridge connection status."""
    try:
        resp = httpx.get(f"{BRIDGE_URL}/api/status", timeout=5)
        status = resp.json().get("status", "unknown")
    except Exception:
        return {"status": "bridge_offline"}

    result = {"status": status}

    if status == "qr_pending":
        try:
            qr_resp = httpx.get(f"{BRIDGE_URL}/api/qr", timeout=5)
            qr_data = qr_resp.json()
            if qr_data.get("qr"):
                result["qr_data_url"] = qr_data["qr"]
        except Exception:
            pass

    return result


def send_message(recipient_jid: str, message: str) -> dict:
    """Send a message through the bridge."""
    try:
        resp = httpx.post(
            f"{BRIDGE_URL}/api/send",
            json={"recipient": recipient_jid, "message": message},
            timeout=30,
        )
        return resp.json()
    except httpx.ConnectError:
        return {"success": False, "error": "Bridge not running"}
    except Exception as e:
        return {"success": False, "error": str(e)}


def get_incoming_messages(since_minutes: int = 5) -> dict:
    """Get recent incoming messages from the bridge."""
    since_minutes = max(1, min(since_minutes, 60))
    since_ts = int((datetime.now(tz=timezone.utc) - timedelta(minutes=since_minutes)).timestamp())

    try:
        status = httpx.get(f"{BRIDGE_URL}/api/status", timeout=5).json()
        if status.get("status") != "connected":
            return {
                "messages": [],
                "error": f"Bridge is {status.get('status', 'unavailable')}, not connected. "
                         "Incoming messages are unavailable until it reconnects.",
            }

        resp = httpx.get(
            f"{BRIDGE_URL}/api/incoming",
            params={"since": since_ts},
            timeout=10,
        )
        return resp.json()
    except httpx.ConnectError:
        return {"messages": [], "error": "Bridge not running"}
    except Exception as e:
        return {"messages": [], "error": str(e)}
