#!/usr/bin/env python3
"""
WhatsApp MCP Server

A Model Context Protocol server that connects Claude to your WhatsApp.
Read messages, search contacts, send replies - all through natural conversation.

Usage:
    python -m whatsapp_mcp                    # Run MCP server (stdio)
    python -m whatsapp_mcp --transport sse    # Run with SSE transport

Requires:
    - macOS with WhatsApp desktop app installed and logged in
    - For sending: WhatsApp bridge running (cd bridge && npm start)
"""

from __future__ import annotations

import json
import logging
import sys
from typing import Optional

from mcp.server.fastmcp import FastMCP

from whatsapp_mcp.db import refresh_db
from whatsapp_mcp.tools import (
    search_contacts,
    list_recent_chats,
    get_messages,
    search_messages,
    get_unread_summary,
    get_bridge_status,
    send_message,
    get_incoming_messages,
)

logging.basicConfig(level=logging.INFO, stream=sys.stderr)
logger = logging.getLogger("whatsapp-mcp")

mcp = FastMCP(
    "WhatsApp",
    instructions=(
        "WhatsApp MCP server. Search contacts, read messages, and send replies. "
        "Read-only tools work immediately. To send messages, first use whatsapp_status "
        "to check connection - if not connected, it returns a QR code to scan."
    ),
)


# ──────────────────────────────────────────────────────────────────────────────
# Connection
# ──────────────────────────────────────────────────────────────────────────────


@mcp.tool()
def whatsapp_status() -> str:
    """Check WhatsApp connection status.

    Returns current status: 'connected', 'qr_pending', 'disconnected', or 'bridge_offline'.
    If QR code is needed, returns a data URL you can open in a browser to scan.

    Call this before sending messages to ensure WhatsApp is connected.
    """
    result = get_bridge_status()

    lines = [f"Status: {result['status']}"]

    if result["status"] == "bridge_offline":
        lines.append("The WhatsApp bridge is not running.")
        lines.append("Start it with: cd bridge && npm start")
    elif result["status"] == "qr_pending":
        lines.append("WhatsApp needs to be connected. Scan the QR code with your phone.")
        if result.get("qr_data_url"):
            lines.append("")
            lines.append("QR Code (open this data URL in a browser):")
            lines.append(result["qr_data_url"])
    elif result["status"] == "connected":
        lines.append("WhatsApp is connected and ready.")
    elif result["status"] == "disconnected":
        lines.append("WhatsApp is disconnected. It will auto-reconnect or show a new QR.")

    return "\n".join(lines)


# ──────────────────────────────────────────────────────────────────────────────
# Read-only tools (work without bridge)
# ──────────────────────────────────────────────────────────────────────────────


@mcp.tool()
def whatsapp_search_contacts(query: str) -> str:
    """Search WhatsApp contacts by name or phone number.

    Returns matching contacts with their JID, display name, and phone number.
    Use this to find a contact before reading their messages or sending them a message.

    Args:
        query: Name or phone number to search for (partial match supported).
    """
    refresh_db()
    return search_contacts(query)


@mcp.tool()
def whatsapp_list_chats(limit: int = 20, chat_type: str = "all") -> str:
    """List recent WhatsApp chats ordered by last message time.

    Args:
        limit: Number of chats to return (max 50).
        chat_type: Filter by 'dm', 'group', or 'all'.
    """
    refresh_db()
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

    Args:
        chat_jid: JID of the chat (from whatsapp_search_contacts or whatsapp_list_chats).
        after: Only messages after this datetime (ISO 8601). Defaults to 24h ago.
        before: Only messages before this datetime (ISO 8601). Defaults to now.
        limit: Max messages to return (max 200).
        search_text: Optional text to filter messages.
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
def whatsapp_search_messages(
    query: str,
    chat_jid: Optional[str] = None,
    limit: int = 20,
) -> str:
    """Search for messages containing specific text across all chats.

    Args:
        query: Text to search for (case-insensitive).
        chat_jid: Optional - restrict search to a specific chat.
        limit: Max results (max 50).
    """
    refresh_db()
    return search_messages(query=query, chat_jid=chat_jid, limit=limit)


@mcp.tool()
def whatsapp_unread() -> str:
    """Get a summary of all unread WhatsApp messages.

    Returns chats with unread messages and recent message previews.
    Great for "catch me up" or "what did I miss" requests.
    """
    refresh_db()
    return get_unread_summary()


# ──────────────────────────────────────────────────────────────────────────────
# Send tools (require bridge connection)
# ──────────────────────────────────────────────────────────────────────────────


@mcp.tool()
def whatsapp_send(recipient_jid: str, message: str) -> str:
    """Send a WhatsApp message.

    IMPORTANT:
    1. First use whatsapp_status to ensure WhatsApp is connected.
    2. Use whatsapp_search_contacts to get the correct JID.
    3. Always confirm with the user before sending.

    Args:
        recipient_jid: The JID from whatsapp_search_contacts.
        message: The text message to send.
    """
    # Check connection
    status = get_bridge_status()
    if status["status"] != "connected":
        return json.dumps({
            "success": False,
            "error": f"WhatsApp not connected (status: {status['status']}). Use whatsapp_status first.",
        })

    result = send_message(recipient_jid, message)
    return json.dumps(result)


@mcp.tool()
def whatsapp_incoming(since_minutes: int = 5) -> str:
    """Get recent incoming WhatsApp messages from the live connection.

    These are real-time messages, not from the local database.
    Useful for checking new messages that just arrived.

    Args:
        since_minutes: Look back N minutes (default 5, max 60).
    """
    result = get_incoming_messages(since_minutes)
    return json.dumps(result)


# ──────────────────────────────────────────────────────────────────────────────
# Resources
# ──────────────────────────────────────────────────────────────────────────────


@mcp.resource("whatsapp://status")
def resource_status() -> str:
    """Current WhatsApp connection status."""
    return json.dumps(get_bridge_status())


@mcp.resource("whatsapp://unread")
def resource_unread() -> str:
    """Summary of unread WhatsApp messages."""
    refresh_db()
    return get_unread_summary()


@mcp.resource("whatsapp://chats")
def resource_chats() -> str:
    """Recent WhatsApp chats."""
    refresh_db()
    return list_recent_chats(limit=20)


# ──────────────────────────────────────────────────────────────────────────────
# Entry point
# ──────────────────────────────────────────────────────────────────────────────


def main():
    """Run the MCP server."""
    import argparse

    parser = argparse.ArgumentParser(description="WhatsApp MCP Server")
    parser.add_argument(
        "--transport",
        choices=["stdio", "sse"],
        default="stdio",
        help="Transport protocol (default: stdio)",
    )
    args = parser.parse_args()

    mcp.run(transport=args.transport)


if __name__ == "__main__":
    main()
