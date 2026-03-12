import json
import re
from datetime import datetime, timezone, timedelta
from typing import Optional

import httpx

from app.config import BRIDGE_URL
from app import scheduler
from app.db import (
    get_chat_db,
    get_contacts_db,
    get_lid_db,
    refresh_db,
    apple_ts_to_datetime,
    datetime_to_apple_ts,
    format_dt,
)
from app.transcription import transcribe_bridge_voice_message

# LID to phone/name cache (populated on first use)
_lid_cache: dict[str, dict] = {}
_lid_cache_loaded = False
RECENT_VOICE_LOOKBACK_HOURS = 72


def _is_readable_text(text: Optional[str]) -> bool:
    """Check if text looks like real readable text (not binary/protobuf)."""
    if not text:
        return False
    # If it's mostly printable and doesn't look like base64/binary
    non_printable = sum(1 for c in text if ord(c) > 127 and not _is_emoji_char(c))
    if len(text) > 10 and non_printable / len(text) > 0.3:
        return False
    return True


def _is_emoji_char(c: str) -> bool:
    """Rough check for emoji/unicode characters that are valid in messages."""
    cp = ord(c)
    return cp > 0x1F000 or (0x2600 <= cp <= 0x27BF) or (0xFE00 <= cp <= 0xFEFF)


def _clean_sender(name: Optional[str]) -> Optional[str]:
    """Clean up sender name — filter out binary-looking values."""
    if not name:
        return None
    # Base64-ish or binary data
    if re.match(r'^[A-Za-z0-9+/=]{3,}$', name) and '=' in name:
        return None
    return name

# ---------------------------------------------------------------------------
# Message type labels
# ---------------------------------------------------------------------------
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


# ---------------------------------------------------------------------------
# Tool definitions (OpenAI function-calling format)
# ---------------------------------------------------------------------------
TOOL_DEFINITIONS = [
    {
        "type": "function",
        "function": {
            "name": "search_contacts",
            "description": (
                "Search WhatsApp contacts by name or phone number. "
                "Returns matching contacts with their JID (unique identifier), "
                "display name, and phone number. Use this to find a contact "
                "before reading their messages. If multiple matches are found, "
                "present the options to the user to choose."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Name or phone number to search for (partial match supported)",
                    }
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_recent_chats",
            "description": (
                "List recent WhatsApp chats/conversations ordered by last message time. "
                "Shows chat name, type (DM or group), unread count, and last message preview. "
                "Use this to get an overview of recent conversations."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "limit": {
                        "type": "integer",
                        "description": "Number of chats to return (default 20, max 50)",
                        "default": 20,
                    },
                    "chat_type": {
                        "type": "string",
                        "enum": ["all", "dm", "group"],
                        "description": "Filter by chat type: 'dm' for direct messages, 'group' for groups, 'all' for both (default: all)",
                        "default": "all",
                    },
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_messages",
            "description": (
                "Get messages from a specific WhatsApp chat. Supports filtering by date range. "
                "The chat_jid parameter is the unique identifier for the chat — get it from "
                "search_contacts or list_recent_chats first. "
                "Messages are returned in chronological order. "
                "If you need older messages, call again with an earlier 'before' date. "
                "If the conversation seems to start abruptly, fetch earlier messages to get full context."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "chat_jid": {
                        "type": "string",
                        "description": "The JID of the chat (e.g., '919876543210@s.whatsapp.net' for DM, '120363...@g.us' for group)",
                    },
                    "after": {
                        "type": "string",
                        "description": "Only messages after this datetime (ISO 8601 format, e.g., '2025-02-01T00:00:00'). Defaults to 24 hours ago.",
                    },
                    "before": {
                        "type": "string",
                        "description": "Only messages before this datetime (ISO 8601 format). Defaults to now.",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Max messages to return (default 50, max 200)",
                        "default": 50,
                    },
                    "search_text": {
                        "type": "string",
                        "description": "Optional text to search for within messages (case-insensitive)",
                    },
                },
                "required": ["chat_jid"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_group_info",
            "description": (
                "Get details about a WhatsApp group including its members, "
                "creation date, and admin list. The chat_jid must be a group JID "
                "(ending in @g.us)."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "chat_jid": {
                        "type": "string",
                        "description": "The group JID (e.g., '120363...@g.us')",
                    }
                },
                "required": ["chat_jid"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_messages",
            "description": (
                "Search for messages containing specific text across all chats or within a specific chat. "
                "Useful for finding when something was discussed. Returns messages with chat context."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Text to search for in message content (case-insensitive)",
                    },
                    "chat_jid": {
                        "type": "string",
                        "description": "Optional: limit search to a specific chat JID",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Max results to return (default 20, max 50)",
                        "default": 20,
                    },
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_starred_messages",
            "description": (
                "Get starred/important messages, optionally filtered by chat. "
                "Starred messages are ones the user has marked as important."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "chat_jid": {
                        "type": "string",
                        "description": "Optional: limit to starred messages in a specific chat",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Max results to return (default 20)",
                        "default": 20,
                    },
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_chat_statistics",
            "description": (
                "Get statistics about a chat: total message count, messages per participant, "
                "date range, media counts, etc. Useful for understanding chat activity."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "chat_jid": {
                        "type": "string",
                        "description": "The JID of the chat to get statistics for",
                    }
                },
                "required": ["chat_jid"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "check_whatsapp_status",
            "description": (
                "Check the connection status of the WhatsApp bridge. "
                "Returns 'connected', 'qr_pending' (needs QR scan), or 'disconnected'. "
                "Always call this before attempting to send a message."
            ),
            "parameters": {
                "type": "object",
                "properties": {},
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "send_message",
            "description": (
                "Send a WhatsApp text message via the bridge. "
                "IMPORTANT: Before calling this tool, you MUST first show the user a draft of the message "
                "including the recipient name and message text, and ask for their explicit confirmation. "
                "Only call this tool AFTER the user has confirmed they want to send the message. "
                "The recipient_jid MUST be the EXACT JID returned by search_contacts — do NOT modify it or use a different one. "
                "The recipient_name MUST match the contact name from search_contacts — the server will verify this."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "recipient_jid": {
                        "type": "string",
                        "description": "The EXACT JID returned by search_contacts (e.g., '919876543210@s.whatsapp.net'). Copy this verbatim from the search result.",
                    },
                    "recipient_name": {
                        "type": "string",
                        "description": "The contact name as returned by search_contacts. Used for server-side verification to prevent sending to the wrong person.",
                    },
                    "message": {
                        "type": "string",
                        "description": "The text message to send",
                    },
                },
                "required": ["recipient_jid", "recipient_name", "message"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_incoming_messages",
            "description": (
                "Get recent incoming WhatsApp messages received via the live bridge connection. "
                "These are real-time messages, not from the local database. "
                "Useful for checking what just came in or alerting the user about new messages."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "since_minutes": {
                        "type": "integer",
                        "description": "Get messages from the last N minutes (default 5, max 4320 / 72 hours)",
                        "default": 5,
                    },
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "transcribe_voice_message",
            "description": (
                "Transcribe a recent WhatsApp voice note into text using the configured speech-to-text service. "
                "Use this when a relevant recent message is a voice note and the user asks what someone said. "
                "Works best with a message_id from get_incoming_messages, but can also find the latest recent voice note "
                "by chat, sender, and time window."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "message_id": {
                        "type": "string",
                        "description": "Preferred: the recent voice-note message ID returned by get_incoming_messages",
                    },
                    "chat_jid": {
                        "type": "string",
                        "description": "Optional: limit the lookup to a specific chat JID",
                    },
                    "participant_jid": {
                        "type": "string",
                        "description": "Optional: for groups, limit the lookup to a specific sender JID",
                    },
                    "sender_name": {
                        "type": "string",
                        "description": "Optional sender name to help pick the right recent voice note when no message_id is available",
                    },
                    "after": {
                        "type": "string",
                        "description": "Optional ISO 8601 datetime. Only consider recent voice notes after this time.",
                    },
                    "latest": {
                        "type": "boolean",
                        "description": "If true, transcribe the most recent matching voice note (default true)",
                        "default": True,
                    },
                    "language": {
                        "type": "string",
                        "description": "Optional language hint for transcription, such as 'en' or 'hi'",
                    },
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_unread_summary",
            "description": (
                "Get a summary of all chats with unread messages, including preview of recent messages. "
                "Great for 'catch me up' or 'what did I miss' requests. Returns unread chats with "
                "message previews so you can give the user a comprehensive summary."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "max_chats": {
                        "type": "integer",
                        "description": "Maximum number of unread chats to include (default 10)",
                        "default": 10,
                    },
                    "messages_per_chat": {
                        "type": "integer",
                        "description": "Number of recent messages to include per chat (default 5)",
                        "default": 5,
                    },
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "schedule_message",
            "description": (
                "Schedule a WhatsApp message to be sent at a future time. "
                "IMPORTANT: Same rules as send_message — you MUST show the user a draft and get confirmation first. "
                "The recipient_jid and recipient_name MUST come from search_contacts. "
                "The send_at time must be in ISO 8601 format using the user's LOCAL timezone (not UTC)."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "recipient_jid": {
                        "type": "string",
                        "description": "The EXACT JID from search_contacts",
                    },
                    "recipient_name": {
                        "type": "string",
                        "description": "The contact name from search_contacts",
                    },
                    "message": {
                        "type": "string",
                        "description": "The text message to send",
                    },
                    "send_at": {
                        "type": "string",
                        "description": "When to send the message, in ISO 8601 format using the user's local timezone (e.g., '2025-03-15T09:00:00'). Naive datetimes (no offset) are treated as local time.",
                    },
                },
                "required": ["recipient_jid", "recipient_name", "message", "send_at"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_scheduled_messages",
            "description": "List all pending scheduled messages that haven't been sent yet.",
            "parameters": {
                "type": "object",
                "properties": {},
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "cancel_scheduled_message",
            "description": "Cancel a pending scheduled message by its ID.",
            "parameters": {
                "type": "object",
                "properties": {
                    "message_id": {
                        "type": "integer",
                        "description": "The ID of the scheduled message to cancel (from list_scheduled_messages)",
                    },
                },
                "required": ["message_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "schedule_broadcast",
            "description": (
                "Schedule the SAME or similar message to MULTIPLE recipients at once, with staggered send times "
                "so they look natural (not all at once). Great for holiday wishes, announcements, event invites. "
                "IMPORTANT: Same rules as send_message — draft ALL messages first, show the user the full list, "
                "and get explicit confirmation before calling this tool. "
                "Use search_contacts and list_recent_chats to find recipients first. "
                "Personalize each message based on the relationship (formal for colleagues, casual for friends)."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "recipients": {
                        "type": "array",
                        "description": "List of recipients with personalized messages",
                        "items": {
                            "type": "object",
                            "properties": {
                                "recipient_jid": {"type": "string", "description": "The EXACT JID from search_contacts"},
                                "recipient_name": {"type": "string", "description": "Contact name from search_contacts"},
                                "message": {"type": "string", "description": "Personalized message for this recipient"},
                            },
                            "required": ["recipient_jid", "recipient_name", "message"],
                        },
                    },
                    "send_at": {
                        "type": "string",
                        "description": "When to start sending, in ISO 8601 local time (e.g., '2025-03-15T09:00:00')",
                    },
                    "stagger_seconds": {
                        "type": "integer",
                        "description": "Seconds between each message (default 45, min 15, max 300). Makes sends look natural.",
                        "default": 45,
                    },
                },
                "required": ["recipients", "send_at"],
            },
        },
    },
]


# ---------------------------------------------------------------------------
# Tool implementations
# ---------------------------------------------------------------------------


def search_contacts(query: str) -> str:
    """Search contacts by name or phone number."""
    results = []

    # Search in ContactsV2 database
    try:
        conn = get_contacts_db()
        cursor = conn.execute(
            """
            SELECT ZWHATSAPPID, ZFULLNAME, ZPHONENUMBER, ZABOUTTEXT, ZPHONENUMBERLABEL
            FROM ZWAADDRESSBOOKCONTACT
            WHERE (ZFULLNAME LIKE ? OR ZPHONENUMBER LIKE ? OR ZWHATSAPPID LIKE ?)
            AND ZWHATSAPPID IS NOT NULL
            ORDER BY ZFULLNAME
            LIMIT 20
            """,
            (f"%{query}%", f"%{query}%", f"%{query}%"),
        )
        for row in cursor:
            results.append(
                {
                    "jid": f"{row['ZWHATSAPPID']}@s.whatsapp.net" if row["ZWHATSAPPID"] and "@" not in row["ZWHATSAPPID"] else row["ZWHATSAPPID"],
                    "name": row["ZFULLNAME"],
                    "phone": row["ZPHONENUMBER"],
                    "about": row["ZABOUTTEXT"],
                }
            )
        conn.close()
    except Exception:
        pass

    # Also search in chat sessions for names not in contacts
    try:
        conn = get_chat_db()
        cursor = conn.execute(
            """
            SELECT ZCONTACTJID, ZPARTNERNAME
            FROM ZWACHATSESSION
            WHERE ZPARTNERNAME LIKE ? AND ZREMOVED = 0
            ORDER BY ZLASTMESSAGEDATE DESC
            LIMIT 20
            """,
            (f"%{query}%",),
        )
        existing_jids = {r["jid"] for r in results}
        for row in cursor:
            jid = row["ZCONTACTJID"]
            if jid and jid not in existing_jids:
                is_group = "@g.us" in jid if jid else False
                results.append(
                    {
                        "jid": jid,
                        "name": row["ZPARTNERNAME"],
                        "phone": None,
                        "type": "group" if is_group else "contact",
                    }
                )
        conn.close()
    except Exception:
        pass

    if not results:
        return json.dumps({"matches": [], "message": f"No contacts found matching '{query}'"})

    return json.dumps({"matches": results, "count": len(results)})


def list_recent_chats(limit: int = 20, chat_type: str = "all") -> str:
    """List recent chats ordered by last message time."""
    limit = min(limit, 50)
    conn = get_chat_db()

    type_filter = ""
    if chat_type == "dm":
        type_filter = "AND c.ZSESSIONTYPE = 0"
    elif chat_type == "group":
        type_filter = "AND c.ZSESSIONTYPE = 1"

    # Use a subquery to get the actual latest message date and text from ZWAMESSAGE
    # because ZLASTMESSAGEDATE can have corrupted values for some chats
    cursor = conn.execute(
        f"""
        SELECT c.ZCONTACTJID, c.ZPARTNERNAME, c.ZSESSIONTYPE, c.ZUNREADCOUNT,
               latest.msg_text, latest.msg_date
        FROM ZWACHATSESSION c
        LEFT JOIN (
            SELECT ZCHATSESSION,
                   MAX(ZMESSAGEDATE) as msg_date,
                   ZTEXT as msg_text
            FROM ZWAMESSAGE
            WHERE ZMESSAGETYPE IN (0, 1, 2, 3, 7, 8, 15)
            GROUP BY ZCHATSESSION
        ) latest ON latest.ZCHATSESSION = c.Z_PK
        WHERE c.ZREMOVED = 0
          AND c.ZSESSIONTYPE IN (0, 1)
          AND latest.msg_date IS NOT NULL
          {type_filter}
        ORDER BY latest.msg_date DESC
        LIMIT ?
        """,
        (limit,),
    )

    chats = []
    for row in cursor:
        last_dt = apple_ts_to_datetime(row["msg_date"])
        if last_dt is None:
            continue
        last_msg = row["msg_text"]
        if not _is_readable_text(last_msg):
            last_msg = None
        chats.append(
            {
                "jid": row["ZCONTACTJID"],
                "name": row["ZPARTNERNAME"],
                "type": "group" if row["ZSESSIONTYPE"] == 1 else "dm",
                "unread_count": row["ZUNREADCOUNT"] or 0,
                "last_message": last_msg,
                "last_message_time": format_dt(last_dt),
            }
        )
    conn.close()
    return json.dumps({"chats": chats, "count": len(chats)})


def get_messages(
    chat_jid: str,
    after: Optional[str] = None,
    before: Optional[str] = None,
    limit: int = 50,
    search_text: Optional[str] = None,
) -> str:
    """Get messages from a specific chat with date filters."""
    limit = min(limit, 200)
    now = datetime.now(tz=timezone.utc)

    # Parse date filters
    if after:
        try:
            after_dt = datetime.fromisoformat(after.replace("Z", "+00:00"))
            if after_dt.tzinfo is None:
                after_dt = after_dt.replace(tzinfo=timezone.utc)
        except ValueError:
            after_dt = now - timedelta(hours=24)
    else:
        after_dt = now - timedelta(hours=24)

    if before:
        try:
            before_dt = datetime.fromisoformat(before.replace("Z", "+00:00"))
            if before_dt.tzinfo is None:
                before_dt = before_dt.replace(tzinfo=timezone.utc)
        except ValueError:
            before_dt = now
    else:
        before_dt = now

    after_apple = datetime_to_apple_ts(after_dt)
    before_apple = datetime_to_apple_ts(before_dt)

    conn = get_chat_db()

    # Get chat info
    chat_row = conn.execute(
        "SELECT ZPARTNERNAME, ZSESSIONTYPE FROM ZWACHATSESSION WHERE ZCONTACTJID = ?",
        (chat_jid,),
    ).fetchone()

    chat_name = chat_row["ZPARTNERNAME"] if chat_row else "Unknown"
    is_group = chat_row["ZSESSIONTYPE"] == 1 if chat_row else False

    # Build query
    text_filter = ""
    params = [chat_jid, after_apple, before_apple]
    if search_text:
        text_filter = "AND m.ZTEXT LIKE ?"
        params.append(f"%{search_text}%")
    params.append(limit)

    cursor = conn.execute(
        f"""
        SELECT m.ZTEXT, m.ZISFROMME, m.ZMESSAGEDATE, m.ZMESSAGETYPE,
               m.ZFROMJID, m.ZSTARRED, m.ZPUSHNAME,
               gm.ZMEMBERJID, gm.ZCONTACTNAME
        FROM ZWAMESSAGE m
        JOIN ZWACHATSESSION c ON m.ZCHATSESSION = c.Z_PK
        LEFT JOIN ZWAGROUPMEMBER gm ON m.ZGROUPMEMBER = gm.Z_PK
        WHERE c.ZCONTACTJID = ?
          AND m.ZMESSAGEDATE >= ?
          AND m.ZMESSAGEDATE <= ?
          {text_filter}
        ORDER BY m.ZMESSAGEDATE ASC
        LIMIT ?
        """,
        params,
    )

    messages = []
    for row in cursor:
        msg_dt = apple_ts_to_datetime(row["ZMESSAGEDATE"])
        msg_type = MESSAGE_TYPES.get(row["ZMESSAGETYPE"], f"type_{row['ZMESSAGETYPE']}")

        push_name = _clean_sender(row["ZPUSHNAME"])
        # For group messages, try group member info first
        member_name = None
        member_jid = None
        if is_group and not row["ZISFROMME"]:
            member_jid = row["ZMEMBERJID"] if "ZMEMBERJID" in row.keys() else None
            contact_name = row["ZCONTACTNAME"] if "ZCONTACTNAME" in row.keys() else None
            if contact_name and _is_readable_text(contact_name):
                member_name = contact_name
            elif member_jid:
                member_name = _jid_to_name(member_jid)
        sender = "You" if row["ZISFROMME"] else (member_name or push_name or _jid_to_name(row["ZFROMJID"]) or "them")

        # Skip system messages with binary content
        text = row["ZTEXT"]
        if msg_type == "system" and text and not _is_readable_text(text):
            continue

        msg = {
            "time": format_dt(msg_dt),
            "timestamp": int(msg_dt.timestamp()) if msg_dt else None,
            "sender": sender,
            "sender_jid": None if row["ZISFROMME"] else (member_jid or row["ZFROMJID"]),
            "type": msg_type,
            "starred": bool(row["ZSTARRED"]),
        }
        if msg_type == "voice_note":
            msg["transcription_available"] = bool(
                msg_dt and (datetime.now(tz=timezone.utc) - msg_dt) <= timedelta(hours=RECENT_VOICE_LOOKBACK_HOURS)
            )
        if text and _is_readable_text(text):
            msg["text"] = text
        elif msg_type != "text":
            msg["text"] = f"[{msg_type}]"
        else:
            msg["text"] = ""

        messages.append(msg)

    conn.close()

    return json.dumps(
        {
            "chat_name": chat_name,
            "chat_jid": chat_jid,
            "chat_type": "group" if is_group else "dm",
            "time_range": {
                "after": format_dt(after_dt),
                "before": format_dt(before_dt),
            },
            "messages": messages,
            "count": len(messages),
            "has_more": len(messages) == limit,
        }
    )


def get_group_info(chat_jid: str) -> str:
    """Get group details including members."""
    conn = get_chat_db()

    # Get group session
    chat = conn.execute(
        """
        SELECT c.Z_PK, c.ZPARTNERNAME, c.ZSESSIONTYPE, c.ZLASTMESSAGEDATE
        FROM ZWACHATSESSION c
        WHERE c.ZCONTACTJID = ?
        """,
        (chat_jid,),
    ).fetchone()

    if not chat:
        conn.close()
        return json.dumps({"error": f"Group not found: {chat_jid}"})

    # Get group info
    group_info = conn.execute(
        """
        SELECT g.ZCREATORJID, g.ZOWNERJID, g.ZCREATIONDATE
        FROM ZWAGROUPINFO g
        WHERE g.ZCHATSESSION = ?
        """,
        (chat["Z_PK"],),
    ).fetchone()

    # Get members
    members_cursor = conn.execute(
        """
        SELECT ZMEMBERJID, ZCONTACTNAME, ZISADMIN, ZISACTIVE
        FROM ZWAGROUPMEMBER
        WHERE ZCHATSESSION = ?
        ORDER BY ZCONTACTNAME
        """,
        (chat["Z_PK"],),
    )

    members = []
    for m in members_cursor:
        members.append(
            {
                "jid": m["ZMEMBERJID"],
                "name": m["ZCONTACTNAME"] or _jid_to_name(m["ZMEMBERJID"]),
                "is_admin": bool(m["ZISADMIN"]),
                "is_active": bool(m["ZISACTIVE"]),
            }
        )

    result = {
        "name": chat["ZPARTNERNAME"],
        "jid": chat_jid,
        "member_count": len(members),
        "members": members,
    }

    if group_info:
        creation_dt = apple_ts_to_datetime(group_info["ZCREATIONDATE"])
        result["created"] = format_dt(creation_dt)
        result["creator"] = _jid_to_name(group_info["ZCREATORJID"])
        result["owner"] = _jid_to_name(group_info["ZOWNERJID"])

    conn.close()
    return json.dumps(result)


def search_messages(query: str, chat_jid: Optional[str] = None, limit: int = 20) -> str:
    """Search messages by text content."""
    limit = min(limit, 50)
    conn = get_chat_db()

    jid_filter = ""
    params = [f"%{query}%"]
    if chat_jid:
        jid_filter = "AND c.ZCONTACTJID = ?"
        params.append(chat_jid)
    params.append(limit)

    cursor = conn.execute(
        f"""
        SELECT m.ZTEXT, m.ZISFROMME, m.ZMESSAGEDATE, m.ZMESSAGETYPE,
               m.ZFROMJID, m.ZPUSHNAME,
               c.ZPARTNERNAME, c.ZCONTACTJID, c.ZSESSIONTYPE
        FROM ZWAMESSAGE m
        JOIN ZWACHATSESSION c ON m.ZCHATSESSION = c.Z_PK
        WHERE m.ZTEXT LIKE ?
          {jid_filter}
        ORDER BY m.ZMESSAGEDATE DESC
        LIMIT ?
        """,
        params,
    )

    results = []
    for row in cursor:
        msg_dt = apple_ts_to_datetime(row["ZMESSAGEDATE"])
        sender = "You" if row["ZISFROMME"] else (row["ZPUSHNAME"] or _jid_to_name(row["ZFROMJID"]) or "them")
        results.append(
            {
                "chat_name": row["ZPARTNERNAME"],
                "chat_jid": row["ZCONTACTJID"],
                "chat_type": "group" if row["ZSESSIONTYPE"] == 1 else "dm",
                "sender": sender,
                "text": row["ZTEXT"],
                "time": format_dt(msg_dt),
            }
        )

    conn.close()
    return json.dumps({"query": query, "results": results, "count": len(results)})


def get_starred_messages(chat_jid: Optional[str] = None, limit: int = 20) -> str:
    """Get starred/important messages."""
    limit = min(limit, 50)
    conn = get_chat_db()

    jid_filter = ""
    params = []
    if chat_jid:
        jid_filter = "AND c.ZCONTACTJID = ?"
        params.append(chat_jid)
    params.append(limit)

    cursor = conn.execute(
        f"""
        SELECT m.ZTEXT, m.ZISFROMME, m.ZMESSAGEDATE, m.ZMESSAGETYPE,
               m.ZFROMJID, m.ZPUSHNAME,
               c.ZPARTNERNAME, c.ZCONTACTJID
        FROM ZWAMESSAGE m
        JOIN ZWACHATSESSION c ON m.ZCHATSESSION = c.Z_PK
        WHERE m.ZSTARRED = 1
          {jid_filter}
        ORDER BY m.ZMESSAGEDATE DESC
        LIMIT ?
        """,
        params,
    )

    results = []
    for row in cursor:
        msg_dt = apple_ts_to_datetime(row["ZMESSAGEDATE"])
        sender = "You" if row["ZISFROMME"] else (row["ZPUSHNAME"] or _jid_to_name(row["ZFROMJID"]) or "them")
        results.append(
            {
                "chat_name": row["ZPARTNERNAME"],
                "chat_jid": row["ZCONTACTJID"],
                "sender": sender,
                "text": row["ZTEXT"] or f"[{MESSAGE_TYPES.get(row['ZMESSAGETYPE'], 'media')}]",
                "time": format_dt(msg_dt),
        })

    conn.close()
    return json.dumps({"starred_messages": results, "count": len(results)})


def get_chat_statistics(chat_jid: str) -> str:
    """Get statistics about a chat."""
    conn = get_chat_db()

    chat = conn.execute(
        "SELECT Z_PK, ZPARTNERNAME, ZSESSIONTYPE FROM ZWACHATSESSION WHERE ZCONTACTJID = ?",
        (chat_jid,),
    ).fetchone()

    if not chat:
        conn.close()
        return json.dumps({"error": f"Chat not found: {chat_jid}"})

    chat_pk = chat["Z_PK"]

    # Total messages
    total = conn.execute(
        "SELECT COUNT(*) as cnt FROM ZWAMESSAGE WHERE ZCHATSESSION = ?",
        (chat_pk,),
    ).fetchone()["cnt"]

    # Sent vs received
    sent = conn.execute(
        "SELECT COUNT(*) as cnt FROM ZWAMESSAGE WHERE ZCHATSESSION = ? AND ZISFROMME = 1",
        (chat_pk,),
    ).fetchone()["cnt"]

    # Date range
    date_range = conn.execute(
        "SELECT MIN(ZMESSAGEDATE) as earliest, MAX(ZMESSAGEDATE) as latest FROM ZWAMESSAGE WHERE ZCHATSESSION = ?",
        (chat_pk,),
    ).fetchone()

    # Message types breakdown
    type_cursor = conn.execute(
        "SELECT ZMESSAGETYPE, COUNT(*) as cnt FROM ZWAMESSAGE WHERE ZCHATSESSION = ? GROUP BY ZMESSAGETYPE ORDER BY cnt DESC",
        (chat_pk,),
    )
    type_breakdown = {}
    for row in type_cursor:
        label = MESSAGE_TYPES.get(row["ZMESSAGETYPE"], f"type_{row['ZMESSAGETYPE']}")
        type_breakdown[label] = row["cnt"]

    # Top senders (for groups)
    top_senders = []
    if chat["ZSESSIONTYPE"] == 1:
        sender_cursor = conn.execute(
            """
            SELECT ZPUSHNAME, ZFROMJID, COUNT(*) as cnt
            FROM ZWAMESSAGE
            WHERE ZCHATSESSION = ? AND ZISFROMME = 0
            GROUP BY ZFROMJID
            ORDER BY cnt DESC
            LIMIT 10
            """,
            (chat_pk,),
        )
        for row in sender_cursor:
            top_senders.append(
                {
                    "name": row["ZPUSHNAME"] or _jid_to_name(row["ZFROMJID"]),
                    "message_count": row["cnt"],
                }
            )

    earliest_dt = apple_ts_to_datetime(date_range["earliest"])
    latest_dt = apple_ts_to_datetime(date_range["latest"])

    result = {
        "chat_name": chat["ZPARTNERNAME"],
        "chat_jid": chat_jid,
        "chat_type": "group" if chat["ZSESSIONTYPE"] == 1 else "dm",
        "total_messages": total,
        "sent_by_you": sent,
        "received": total - sent,
        "earliest_message": format_dt(earliest_dt),
        "latest_message": format_dt(latest_dt),
        "message_types": type_breakdown,
    }
    if top_senders:
        result["top_senders"] = top_senders

    conn.close()
    return json.dumps(result)


# ---------------------------------------------------------------------------
# Voice-note transcription helpers
# ---------------------------------------------------------------------------


def _parse_iso_datetime(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _sender_matches_query(message: dict, sender_name: Optional[str]) -> bool:
    if not sender_name:
        return True
    query = sender_name.strip().lower()
    if not query:
        return True
    candidates = [
        message.get("sender_name"),
        message.get("pushName"),
        _jid_to_name(message.get("senderJid")),
        message.get("senderJid"),
    ]
    for candidate in candidates:
        if isinstance(candidate, str):
            normalized = candidate.strip().lower()
            if normalized and (query in normalized or normalized in query):
                return True
    return False


def _fetch_recent_bridge_messages(since_ts: int) -> list[dict]:
    response = httpx.get(
        f"{BRIDGE_URL}/api/incoming",
        params={"since": max(since_ts, 0)},
        timeout=15,
    )
    if response.status_code != 200:
        raise RuntimeError(f"Bridge request failed with {response.status_code}")
    payload = response.json()
    messages = payload.get("messages")
    if not isinstance(messages, list):
        return []
    return messages


def _select_recent_voice_message(
    *,
    message_id: Optional[str] = None,
    chat_jid: Optional[str] = None,
    participant_jid: Optional[str] = None,
    sender_name: Optional[str] = None,
    after: Optional[str] = None,
    latest: bool = True,
) -> dict:
    if message_id:
        return {
            "id": message_id,
            "chatJid": chat_jid,
            "senderJid": participant_jid,
        }

    after_dt = _parse_iso_datetime(after)
    if after_dt:
        since_ts = int(after_dt.timestamp())
    else:
        since_ts = int((datetime.now(tz=timezone.utc) - timedelta(hours=RECENT_VOICE_LOOKBACK_HOURS)).timestamp())

    candidates = []
    for message in _fetch_recent_bridge_messages(since_ts):
        if message.get("messageType") != "voice_note":
            continue
        if chat_jid and message.get("chatJid") != chat_jid:
            continue
        if participant_jid and message.get("senderJid") != participant_jid:
            continue
        msg_ts = int(message.get("timestamp") or 0)
        if after_dt and msg_ts < int(after_dt.timestamp()):
            continue
        if not _sender_matches_query(message, sender_name):
            continue
        candidates.append(message)

    if not candidates:
        raise RuntimeError(
            "No recent voice note matched that chat or sender. "
            "Try a narrower request, ask about a newer message, or check that the bridge is running."
        )

    candidates.sort(key=lambda item: int(item.get("timestamp") or 0), reverse=latest)
    return candidates[0]


# ---------------------------------------------------------------------------
# Bridge tools (send messages via Baileys bridge)
# ---------------------------------------------------------------------------


def check_whatsapp_status() -> str:
    """Check the connection status of the WhatsApp bridge."""
    try:
        resp = httpx.get(f"{BRIDGE_URL}/api/status", timeout=5)
        data = resp.json()
        return json.dumps(data)
    except httpx.ConnectError:
        return json.dumps({"status": "bridge_offline", "message": "WhatsApp bridge is not running. Start it with run.sh or 'cd bridge && npx tsx src/server.ts'"})
    except Exception as e:
        return json.dumps({"status": "error", "message": str(e)})


def send_message(recipient_jid: str, recipient_name: str, message: str) -> str:
    """Send a text message via the WhatsApp bridge, with JID-name verification."""
    # Server-side safety check: verify the JID actually belongs to the claimed contact
    actual_name = _jid_to_name(recipient_jid)
    if actual_name and recipient_name:
        # Normalize for comparison: lowercase, strip whitespace
        actual_lower = actual_name.lower().strip()
        claimed_lower = recipient_name.lower().strip()
        # Check if there's a reasonable match (one contains the other, or they're equal)
        if actual_lower != claimed_lower and actual_lower not in claimed_lower and claimed_lower not in actual_lower:
            return json.dumps({
                "success": False,
                "error": f"SAFETY BLOCK: JID {recipient_jid} belongs to '{actual_name}', but you specified '{recipient_name}'. "
                         f"This looks like a JID mismatch. Please re-run search_contacts and use the exact JID from the result.",
            })

    try:
        resp = httpx.post(
            f"{BRIDGE_URL}/api/send",
            json={"recipient": recipient_jid, "message": message},
            timeout=15,
        )
        data = resp.json()
        if resp.status_code == 200:
            return json.dumps({
                "success": True,
                "recipient_jid": recipient_jid,
                "recipient_name": actual_name or recipient_name,
                "message_id": data.get("message_id"),
            })
        else:
            return json.dumps({"success": False, "error": data.get("error", "Unknown error"), "status_code": resp.status_code})
    except httpx.ConnectError:
        return json.dumps({"success": False, "error": "WhatsApp bridge is not running. Start it with run.sh or 'cd bridge && npx tsx src/server.ts'"})
    except Exception as e:
        return json.dumps({"success": False, "error": str(e)})


def get_incoming_messages(since_minutes: int = 5) -> str:
    """Get recent incoming messages from the live bridge."""
    since_minutes = min(max(since_minutes, 1), RECENT_VOICE_LOOKBACK_HOURS * 60)
    import time
    since_ts = int(time.time()) - (since_minutes * 60)
    try:
        resp = httpx.get(f"{BRIDGE_URL}/api/incoming", params={"since": since_ts}, timeout=5)
        data = resp.json()
        # Enrich with contact names
        for msg in data.get("messages", []):
            name = _jid_to_name(msg.get("senderJid")) or msg.get("pushName") or msg.get("senderJid")
            msg["sender_name"] = name
            chat_name = _jid_to_name(msg.get("chatJid")) or msg.get("chatJid")
            msg["chat_name"] = chat_name
            msg["message_id"] = msg.get("id")
        return json.dumps(data)
    except httpx.ConnectError:
        return json.dumps({"messages": [], "count": 0, "error": "Bridge not running"})
    except Exception as e:
        return json.dumps({"messages": [], "count": 0, "error": str(e)})


def transcribe_voice_message(
    message_id: Optional[str] = None,
    chat_jid: Optional[str] = None,
    participant_jid: Optional[str] = None,
    sender_name: Optional[str] = None,
    after: Optional[str] = None,
    latest: bool = True,
    language: Optional[str] = None,
) -> str:
    """Transcribe a recent voice note via the bridge media cache."""
    try:
        target = _select_recent_voice_message(
            message_id=message_id,
            chat_jid=chat_jid,
            participant_jid=participant_jid,
            sender_name=sender_name,
            after=after,
            latest=latest,
        )
        resolved_chat_jid = target.get("chatJid") or chat_jid
        resolved_participant_jid = target.get("senderJid") or participant_jid
        result = transcribe_bridge_voice_message(
            target["id"],
            chat_jid=resolved_chat_jid,
            participant_jid=resolved_participant_jid,
            language=language,
        )

        timestamp = result.get("timestamp")
        msg_dt = (
            datetime.fromtimestamp(timestamp, tz=timezone.utc)
            if isinstance(timestamp, (int, float))
            else None
        )
        sender_jid = result.get("senderJid") or resolved_participant_jid
        sender_display = (
            result.get("pushName")
            or _jid_to_name(sender_jid)
            or sender_name
            or sender_jid
            or "Unknown sender"
        )
        chat_display = _jid_to_name(result.get("chatJid")) or result.get("chatJid")

        return json.dumps(
            {
                "success": True,
                "message_id": result.get("id") or target["id"],
                "chat_jid": result.get("chatJid") or resolved_chat_jid,
                "chat_name": chat_display,
                "sender_jid": sender_jid,
                "sender_name": sender_display,
                "time": format_dt(msg_dt),
                "message_type": result.get("messageType", "voice_note"),
                "duration_seconds": result.get("duration_seconds") or result.get("durationSeconds"),
                "language": result.get("language"),
                "provider": result.get("provider"),
                "model": result.get("model"),
                "transcript": result.get("transcript", ""),
                "segments": result.get("segments", []),
            }
        )
    except Exception as e:
        return json.dumps({"success": False, "error": str(e)})


def get_unread_summary(max_chats: int = 10, messages_per_chat: int = 5) -> str:
    """Get a summary of all unread chats with recent message previews."""
    max_chats = min(max_chats, 20)
    messages_per_chat = min(messages_per_chat, 10)
    conn = get_chat_db()

    cursor = conn.execute(
        """
        SELECT c.Z_PK, c.ZCONTACTJID, c.ZPARTNERNAME, c.ZSESSIONTYPE, c.ZUNREADCOUNT
        FROM ZWACHATSESSION c
        WHERE c.ZREMOVED = 0
          AND c.ZUNREADCOUNT > 0
          AND c.ZSESSIONTYPE IN (0, 1)
        ORDER BY c.ZUNREADCOUNT DESC
        LIMIT ?
        """,
        (max_chats,),
    )

    chats = []
    for row in cursor:
        chat_pk = row["Z_PK"]
        # Get last N messages for this chat
        msg_cursor = conn.execute(
            """
            SELECT m.ZTEXT, m.ZISFROMME, m.ZMESSAGEDATE, m.ZMESSAGETYPE, m.ZPUSHNAME, m.ZFROMJID
            FROM ZWAMESSAGE m
            WHERE m.ZCHATSESSION = ?
            ORDER BY m.ZMESSAGEDATE DESC
            LIMIT ?
            """,
            (chat_pk, messages_per_chat),
        )
        messages = []
        for m in msg_cursor:
            msg_dt = apple_ts_to_datetime(m["ZMESSAGEDATE"])
            msg_type = MESSAGE_TYPES.get(m["ZMESSAGETYPE"], f"type_{m['ZMESSAGETYPE']}")
            text = m["ZTEXT"]
            if not _is_readable_text(text):
                text = f"[{msg_type}]"
            sender = "You" if m["ZISFROMME"] else (m["ZPUSHNAME"] or _jid_to_name(m["ZFROMJID"]) or "them")
            messages.append({
                "sender": sender,
                "text": text or f"[{msg_type}]",
                "time": format_dt(msg_dt),
                "type": msg_type,
            })
        messages.reverse()  # chronological order

        chats.append({
            "jid": row["ZCONTACTJID"],
            "name": row["ZPARTNERNAME"],
            "type": "group" if row["ZSESSIONTYPE"] == 1 else "dm",
            "unread_count": row["ZUNREADCOUNT"],
            "recent_messages": messages,
        })

    conn.close()

    return json.dumps({
        "unread_chats": chats,
        "total_unread_chats": len(chats),
        "total_unread_messages": sum(c["unread_count"] for c in chats),
    })


def schedule_message_tool(recipient_jid: str, recipient_name: str, message: str, send_at: str) -> str:
    """Schedule a message for future delivery."""
    # Same JID-name safety check as send_message
    actual_name = _jid_to_name(recipient_jid)
    if actual_name and recipient_name:
        actual_lower = actual_name.lower().strip()
        claimed_lower = recipient_name.lower().strip()
        if actual_lower != claimed_lower and actual_lower not in claimed_lower and claimed_lower not in actual_lower:
            return json.dumps({
                "success": False,
                "error": f"SAFETY BLOCK: JID {recipient_jid} belongs to '{actual_name}', not '{recipient_name}'.",
            })

    result = scheduler.schedule_message(recipient_jid, recipient_name, message, send_at)
    return json.dumps(result)


def list_scheduled_messages() -> str:
    """List all pending scheduled messages."""
    messages = scheduler.list_scheduled()
    return json.dumps({"scheduled_messages": messages, "count": len(messages)})


def cancel_scheduled_message(message_id: int) -> str:
    """Cancel a pending scheduled message."""
    result = scheduler.cancel_scheduled(message_id)
    return json.dumps(result)


def schedule_broadcast_tool(recipients: list[dict], send_at: str, stagger_seconds: int = 45) -> str:
    """Schedule a broadcast message to multiple recipients."""
    result = scheduler.schedule_broadcast(recipients, send_at, stagger_seconds)
    return json.dumps(result)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _load_lid_cache() -> None:
    """Load the LID-to-phone mapping from the LID database."""
    global _lid_cache, _lid_cache_loaded
    if _lid_cache_loaded:
        return
    try:
        conn = get_lid_db()
        rows = conn.execute(
            "SELECT ZIDENTIFIER, ZPHONENUMBER, ZDISPLAYNAME FROM ZWAZACCOUNT"
        ).fetchall()
        for row in rows:
            lid = row["ZIDENTIFIER"]
            if lid:
                _lid_cache[lid] = {
                    "phone": row["ZPHONENUMBER"],
                    "display_name": row["ZDISPLAYNAME"],
                }
        conn.close()
        _lid_cache_loaded = True
    except Exception:
        pass


def _resolve_lid(lid: Optional[str]) -> Optional[str]:
    """Resolve a LID to a phone number."""
    if not lid:
        return None
    _load_lid_cache()
    entry = _lid_cache.get(lid)
    if entry:
        return entry.get("phone")
    return None


def _jid_to_name(jid: Optional[str]) -> Optional[str]:
    """Try to resolve a JID to a display name."""
    if not jid:
        return None

    # Handle LID-based JIDs (e.g., '12345@lid')
    if "@lid" in jid:
        phone = _resolve_lid(jid)
        if phone:
            # Now resolve the phone to a contact name
            return _jid_to_name(f"{phone}@s.whatsapp.net")
        return None

    # Strip the @s.whatsapp.net suffix to get the phone number
    phone = jid.split("@")[0] if "@" in jid else jid
    # Try contacts DB
    try:
        conn = get_contacts_db()
        row = conn.execute(
            "SELECT ZFULLNAME FROM ZWAADDRESSBOOKCONTACT WHERE ZWHATSAPPID = ?",
            (phone,),
        ).fetchone()
        conn.close()
        if row and row["ZFULLNAME"]:
            return row["ZFULLNAME"]
    except Exception:
        pass
    # Try chat sessions
    try:
        conn = get_chat_db()
        row = conn.execute(
            "SELECT ZPARTNERNAME FROM ZWACHATSESSION WHERE ZCONTACTJID = ?",
            (jid,),
        ).fetchone()
        conn.close()
        if row and row["ZPARTNERNAME"]:
            return row["ZPARTNERNAME"]
    except Exception:
        pass
    return phone


# ---------------------------------------------------------------------------
# Tool dispatcher
# ---------------------------------------------------------------------------

TOOL_MAP = {
    "search_contacts": search_contacts,
    "list_recent_chats": list_recent_chats,
    "get_messages": get_messages,
    "get_group_info": get_group_info,
    "search_messages": search_messages,
    "get_starred_messages": get_starred_messages,
    "get_chat_statistics": get_chat_statistics,
    "check_whatsapp_status": check_whatsapp_status,
    "send_message": send_message,
    "get_incoming_messages": get_incoming_messages,
    "transcribe_voice_message": transcribe_voice_message,
    "get_unread_summary": get_unread_summary,
    "schedule_message": schedule_message_tool,
    "list_scheduled_messages": list_scheduled_messages,
    "cancel_scheduled_message": cancel_scheduled_message,
    "schedule_broadcast": schedule_broadcast_tool,
}


def execute_tool(name: str, arguments: dict) -> str:
    """Execute a tool by name with given arguments."""
    func = TOOL_MAP.get(name)
    if not func:
        return json.dumps({"error": f"Unknown tool: {name}"})
    try:
        return func(**arguments)
    except Exception as e:
        return json.dumps({"error": f"Tool '{name}' failed: {str(e)}"})
