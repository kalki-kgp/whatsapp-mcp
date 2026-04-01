"""Configuration for WhatsApp MCP Server."""

import os
from pathlib import Path

# WhatsApp database paths (macOS)
WHATSAPP_DATA_DIR = Path.home() / "Library" / "Group Containers" / "group.net.whatsapp.WhatsApp.shared"
CHAT_STORAGE_DB = WHATSAPP_DATA_DIR / "ChatStorage.sqlite"
CONTACTS_DB = WHATSAPP_DATA_DIR / "ContactsV2.sqlite"

# Temp directory for DB copies (avoids locking issues)
TEMP_DB_DIR = Path(os.environ.get("WHATSAPP_MCP_TEMP_DIR", "/tmp/whatsapp-mcp"))

# Bridge URL (for sending messages)
BRIDGE_URL = os.environ.get("WHATSAPP_BRIDGE_URL", "http://localhost:3010")

# Apple Core Data epoch offset (2001-01-01 00:00:00 UTC)
APPLE_EPOCH_OFFSET = 978307200
