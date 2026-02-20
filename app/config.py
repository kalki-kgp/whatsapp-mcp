import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent.parent / ".env")

# WhatsApp database paths
WHATSAPP_DB_DIR = Path.home() / "Library" / "Group Containers" / "group.net.whatsapp.WhatsApp.shared"
CHAT_STORAGE_DB = WHATSAPP_DB_DIR / "ChatStorage.sqlite"
CONTACTS_DB = WHATSAPP_DB_DIR / "ContactsV2.sqlite"
LID_DB = WHATSAPP_DB_DIR / "LID.sqlite"

# Temp directory for DB copies (avoid lock issues with running WhatsApp)
TEMP_DB_DIR = Path("/tmp/whatsapp-assistant-db")

# Apple Core Data epoch offset (2001-01-01 00:00:00 UTC)
APPLE_EPOCH_OFFSET = 978307200

# LLM config
NEBIUS_BASE_URL = "https://api.tokenfactory.nebius.com/v1/"
NEBIUS_API_KEY = os.environ.get("NEBIUS_API_KEY", "")
LLM_MODEL = "moonshotai/Kimi-K2-Instruct"

# Server
SERVER_PORT = 3009

# WhatsApp Bridge (Baileys sidecar)
BRIDGE_URL = "http://localhost:3010"
