"""
WhatsApp MCP Server — a standards-compliant Model Context Protocol server
that exposes WhatsApp capabilities (contacts, messages, sending, scheduling,
voice-note transcription, etc.) to any MCP client such as Claude Desktop,
Cursor, or the OpenAI Agents SDK.

Run with:
    python mcp_server.py            # stdio transport (default)
    python mcp_server.py --sse      # SSE transport for network access

Requires the WhatsApp Bridge (Node.js) running on port 3010 for send/receive
functionality.  Read-only operations against the local WhatsApp database work
without the bridge.
"""

from __future__ import annotations

import json
import logging
import sys
from typing import Optional

from mcp.server.fastmcp import FastMCP

from app.config import BRIDGE_URL
from app.db import refresh_db
from app.tools import (
    search_contacts,
    list_recent_chats,
    get_messages,
    get_group_info,
    search_messages,
    get_starred_messages,
    get_chat_statistics,
    check_whatsapp_status,
    send_message,
    get_incoming_messages,
    transcribe_voice_message,
    get_unread_summary,
    schedule_message_tool,
    list_scheduled_messages,
    cancel_scheduled_message,
    schedule_broadcast_tool,
)
from app.rewriter import rewrite
from app.settings import get_settings, update_settings

logging.basicConfig(level=logging.INFO, stream=sys.stderr)
logger = logging.getLogger("whatsapp-mcp")

mcp = FastMCP(
    "WhatsApp",
    instructions=(
        "WhatsApp MCP server — search contacts, read/send messages, "
        "manage groups, schedule messages, transcribe voice notes, and more. "
        "The WhatsApp Bridge must be running on localhost:3010 for send/receive; "
        "read-only DB queries work without it."
    ),
)

# ──────────────────────────────────────────────────────────────────────────────
# Tools
# ──────────────────────────────────────────────────────────────────────────────


@mcp.tool()
def whatsapp_search_contacts(query: str) -> str:
    """Search WhatsApp contacts by name or phone number.

    Returns matching contacts with their JID (unique identifier),
    display name, and phone number.  Use this to find a contact
    before reading their messages or sending them a message.
    """
    return search_contacts(query)


@mcp.tool()
def whatsapp_list_recent_chats(
    limit: int = 20,
    chat_type: str = "all",
) -> str:
    """List recent WhatsApp chats ordered by last message time.

    Args:
        limit: Number of chats to return (max 50).
        chat_type: Filter — 'dm' for direct messages, 'group' for groups, 'all' for both.
    """
    return list_recent_chats(limit=limit, chat_type=chat_type)


@mcp.tool()
def whatsapp_get_messages(
    chat_jid: str,
    after: Optional[str] = None,
    before: Optional[str] = None,
    limit: int = 50,
    search_text: Optional[str] = None,
) -> str:
    """Get messages from a specific WhatsApp chat.

    Supports filtering by date range and in-chat text search.
    The chat_jid comes from whatsapp_search_contacts or whatsapp_list_recent_chats.
    Messages are returned in chronological order.

    Args:
        chat_jid: JID of the chat (e.g. '919876543210@s.whatsapp.net' or '120363…@g.us').
        after: Only messages after this datetime (ISO 8601). Defaults to 24 h ago.
        before: Only messages before this datetime (ISO 8601). Defaults to now.
        limit: Max messages to return (max 200).
        search_text: Optional text to search for within messages (case-insensitive).
    """
    refresh_db()
    return get_messages(
        chat_jid=chat_jid,
        after=after,
        before=before,
        limit=limit,
        search_text=search_text,
    )


@mcp.tool()
def whatsapp_get_group_info(chat_jid: str) -> str:
    """Get details about a WhatsApp group including members, creation date, and admins.

    Args:
        chat_jid: Group JID (must end in @g.us).
    """
    return get_group_info(chat_jid)


@mcp.tool()
def whatsapp_search_messages(
    query: str,
    chat_jid: Optional[str] = None,
    limit: int = 20,
) -> str:
    """Search for messages containing specific text across all chats or within one chat.

    Args:
        query: Text to search for (case-insensitive).
        chat_jid: Optional — restrict search to a specific chat.
        limit: Max results (max 50).
    """
    refresh_db()
    return search_messages(query=query, chat_jid=chat_jid, limit=limit)


@mcp.tool()
def whatsapp_get_starred_messages(
    chat_jid: Optional[str] = None,
    limit: int = 20,
) -> str:
    """Get starred/important WhatsApp messages, optionally filtered by chat.

    Args:
        chat_jid: Optional — restrict to starred messages in a specific chat.
        limit: Max results (default 20).
    """
    return get_starred_messages(chat_jid=chat_jid, limit=limit)


@mcp.tool()
def whatsapp_get_chat_statistics(chat_jid: str) -> str:
    """Get statistics about a WhatsApp chat: message counts, date range, media breakdown, top senders.

    Args:
        chat_jid: The JID of the chat.
    """
    return get_chat_statistics(chat_jid)


@mcp.tool()
def whatsapp_check_status() -> str:
    """Check the WhatsApp bridge connection status.

    Returns 'connected', 'qr_pending' (needs QR scan), 'disconnected',
    or 'bridge_offline'.  Always call this before sending a message.
    """
    return check_whatsapp_status()


@mcp.tool()
def whatsapp_send_message(
    recipient_jid: str,
    recipient_name: str,
    message: str,
) -> str:
    """Send a WhatsApp text message via the bridge.

    IMPORTANT: Before calling this, verify the recipient by calling
    whatsapp_search_contacts and use the EXACT jid and name from the result.
    The server performs a safety check to prevent sending to the wrong person.

    Args:
        recipient_jid: The EXACT JID from whatsapp_search_contacts.
        recipient_name: The contact name from whatsapp_search_contacts (used for verification).
        message: The text message to send.
    """
    return send_message(
        recipient_jid=recipient_jid,
        recipient_name=recipient_name,
        message=message,
    )


@mcp.tool()
def whatsapp_get_incoming_messages(since_minutes: int = 5) -> str:
    """Get recent incoming WhatsApp messages received via the live bridge.

    These are real-time messages, not from the local database.

    Args:
        since_minutes: Look back N minutes (default 5, max 4320).
    """
    return get_incoming_messages(since_minutes=since_minutes)


@mcp.tool()
def whatsapp_transcribe_voice_message(
    message_id: Optional[str] = None,
    chat_jid: Optional[str] = None,
    participant_jid: Optional[str] = None,
    sender_name: Optional[str] = None,
    after: Optional[str] = None,
    latest: bool = True,
    language: Optional[str] = None,
) -> str:
    """Transcribe a recent WhatsApp voice note into text.

    Works best with a message_id from whatsapp_get_incoming_messages.
    Can also find the latest voice note by chat/sender/time window.

    Args:
        message_id: Preferred — the voice-note message ID from get_incoming_messages.
        chat_jid: Restrict lookup to a specific chat.
        participant_jid: For groups, restrict to a specific sender.
        sender_name: Sender name hint when no message_id is available.
        after: ISO 8601 datetime — only consider voice notes after this time.
        latest: If True, transcribe the most recent matching voice note.
        language: Language hint for transcription (e.g. 'en', 'hi').
    """
    return transcribe_voice_message(
        message_id=message_id,
        chat_jid=chat_jid,
        participant_jid=participant_jid,
        sender_name=sender_name,
        after=after,
        latest=latest,
        language=language,
    )


@mcp.tool()
def whatsapp_get_unread_summary(
    max_chats: int = 10,
    messages_per_chat: int = 5,
) -> str:
    """Get a summary of all chats with unread messages, including recent message previews.

    Great for "catch me up" or "what did I miss" requests.

    Args:
        max_chats: Maximum unread chats to include (max 20).
        messages_per_chat: Recent messages to include per chat (max 10).
    """
    refresh_db()
    return get_unread_summary(max_chats=max_chats, messages_per_chat=messages_per_chat)


@mcp.tool()
def whatsapp_schedule_message(
    recipient_jid: str,
    recipient_name: str,
    message: str,
    send_at: str,
) -> str:
    """Schedule a WhatsApp message to be sent at a future time.

    Same safety rules as whatsapp_send_message — use exact JID/name from
    whatsapp_search_contacts.  The send_at time should be in ISO 8601 format
    using the user's local timezone.

    Args:
        recipient_jid: The EXACT JID from whatsapp_search_contacts.
        recipient_name: The contact name from whatsapp_search_contacts.
        message: The text message to send.
        send_at: When to send (ISO 8601 local time, e.g. '2025-03-15T09:00:00').
    """
    return schedule_message_tool(
        recipient_jid=recipient_jid,
        recipient_name=recipient_name,
        message=message,
        send_at=send_at,
    )


@mcp.tool()
def whatsapp_list_scheduled_messages() -> str:
    """List all pending scheduled WhatsApp messages that haven't been sent yet."""
    return list_scheduled_messages()


@mcp.tool()
def whatsapp_cancel_scheduled_message(message_id: int) -> str:
    """Cancel a pending scheduled message by its ID.

    Args:
        message_id: The ID from whatsapp_list_scheduled_messages.
    """
    return cancel_scheduled_message(message_id)


@mcp.tool()
def whatsapp_schedule_broadcast(
    recipients: list[dict],
    send_at: str,
    stagger_seconds: int = 45,
) -> str:
    """Schedule the same or similar message to multiple recipients with staggered send times.

    Each recipient object needs: recipient_jid, recipient_name, message.

    Args:
        recipients: List of {recipient_jid, recipient_name, message} objects.
        send_at: Start time in ISO 8601 local time.
        stagger_seconds: Seconds between messages (15–300, default 45).
    """
    return schedule_broadcast_tool(
        recipients=recipients,
        send_at=send_at,
        stagger_seconds=stagger_seconds,
    )


@mcp.tool()
def whatsapp_rewrite_message(text: str, tone: str = "formal") -> str:
    """Rewrite a message draft with a different tone using LLM.

    Useful for adjusting messages before sending.

    Args:
        text: The original message text (max 2000 chars).
        tone: Target tone — 'formal', 'friendly', 'shorter', 'funnier', or any custom tone.
    """
    if len(text) > 2000:
        return json.dumps({"error": "Text too long (max 2000 characters)"})
    try:
        result = rewrite(text, tone)
        return json.dumps({"rewritten": result, "tone": tone})
    except Exception as e:
        return json.dumps({"error": f"Rewrite failed: {str(e)}"})


# ──────────────────────────────────────────────────────────────────────────────
# Resources
# ──────────────────────────────────────────────────────────────────────────────


@mcp.resource("whatsapp://status")
def resource_bridge_status() -> str:
    """Current WhatsApp bridge connection status."""
    return check_whatsapp_status()


@mcp.resource("whatsapp://unread")
def resource_unread_summary() -> str:
    """Summary of all unread WhatsApp chats with recent message previews."""
    refresh_db()
    return get_unread_summary()


@mcp.resource("whatsapp://scheduled")
def resource_scheduled_messages() -> str:
    """All pending scheduled WhatsApp messages."""
    return list_scheduled_messages()


@mcp.resource("whatsapp://settings")
def resource_settings() -> str:
    """Current WhatsApp assistant settings."""
    return json.dumps(get_settings())


@mcp.resource("whatsapp://chats/recent")
def resource_recent_chats() -> str:
    """The 20 most recent WhatsApp chats."""
    refresh_db()
    return list_recent_chats(limit=20)


# ──────────────────────────────────────────────────────────────────────────────
# Prompts
# ──────────────────────────────────────────────────────────────────────────────


@mcp.prompt()
def catch_me_up() -> str:
    """Summarize all unread WhatsApp messages."""
    return (
        "Use whatsapp_get_unread_summary to fetch all unread chats. "
        "Present a clean summary organized by chat, most active first. "
        "For each chat, briefly summarize the unread messages. "
        "Offer to dive deeper into any specific chat."
    )


@mcp.prompt()
def send_a_message(contact_name: str, message_text: str) -> str:
    """Draft and send a WhatsApp message to a contact."""
    return (
        f"1. Search for the contact '{contact_name}' using whatsapp_search_contacts.\n"
        f"2. Check bridge status with whatsapp_check_status.\n"
        f"3. Show the draft message to me for confirmation:\n"
        f"   To: {contact_name}\n"
        f"   Message: {message_text}\n"
        f"4. After I confirm, send it with whatsapp_send_message using the exact JID and name."
    )


# ──────────────────────────────────────────────────────────────────────────────
# Entry point
# ──────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    transport = "stdio"
    if "--sse" in sys.argv:
        transport = "sse"
    mcp.run(transport=transport)
