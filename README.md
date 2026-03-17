# WhatsApp MCP

An AI-powered MCP (Model Context Protocol) layer for WhatsApp on macOS. Gives an LLM full tool-calling access to your WhatsApp — read messages, search contacts, send replies, schedule messages — through **any MCP-compatible AI** (Claude Desktop, Cursor, OpenAI Agents SDK, etc.), the bundled browser UI, or voice.

## Features

- **Standards-compliant MCP server** — Connect from Claude Desktop, Cursor, or any MCP client via stdio
- **Tool-calling AI** — LLM with tools to search contacts, read chats, send messages, check unread, schedule sends
- **WhatsApp Bridge** — Connects via WhatsApp Web (Baileys) for real-time send/receive
- **Local DB access** — Reads your macOS WhatsApp SQLite databases directly
- **Voice control** — Wake word detection, speech-to-text (Apple/Google), text-to-speech
- **Browser UI** — Chat interface at `localhost:3009` with live status
- **Menu Bar App** — macOS menu bar icon for quick controls

## Install

```bash
bash <(curl -fsSL https://raw.githubusercontent.com/kalki-kgp/whatsapp-assistant/main/installer/install.sh)
```

The installer will:
1. Install Homebrew, Python, Node.js, and PortAudio (if missing)
2. Clone the repo to `~/.wa/app/`
3. Set up a Python virtual environment and install dependencies
4. Compile the Apple STT helper
5. Prompt for your [Nebius API key](https://studio.nebius.com)
6. Add the `wa` command to your PATH

**Non-interactive install:**
```bash
NEBIUS_API_KEY=your-key-here bash <(curl -fsSL https://raw.githubusercontent.com/kalki-kgp/whatsapp-assistant/main/installer/install.sh)
```

## Usage

```bash
wa                # Start server + bridge, opens browser
wa stop           # Stop all services
wa restart        # Restart everything
wa status         # Show what's running
wa voice          # Start voice assistant
wa menubar        # Launch menu bar controller
wa logs           # Tail all logs
wa logs server    # Tail server logs only
wa update         # Pull latest code, reinstall deps if changed
wa uninstall      # Remove everything
wa help           # Show all commands
```

## MCP Server

The MCP server exposes all WhatsApp capabilities as standard MCP tools, resources, and prompts. Any MCP-compatible client can connect over stdio.

### Quick start

```bash
# Start the WhatsApp Bridge first (needed for send/receive)
cd bridge && npx tsx src/server.ts &

# Run the MCP server (stdio transport)
python mcp_server.py
```

### Claude Desktop

Add to `~/Library/Application Support/Claude/claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "whatsapp": {
      "command": "python",
      "args": ["/path/to/whatsapp-mcp/mcp_server.py"],
      "env": {
        "NEBIUS_API_KEY": "your-key-here"
      }
    }
  }
}
```

### Cursor

Add to `.cursor/mcp.json` in your project (or global settings):

```json
{
  "mcpServers": {
    "whatsapp": {
      "command": "python",
      "args": ["/path/to/whatsapp-mcp/mcp_server.py"],
      "env": {
        "NEBIUS_API_KEY": "your-key-here"
      }
    }
  }
}
```

### Available tools

| Tool | Description |
|------|-------------|
| `whatsapp_search_contacts` | Search contacts by name or phone number |
| `whatsapp_list_recent_chats` | List recent chats with last message preview |
| `whatsapp_get_messages` | Get messages from a chat with date/text filters |
| `whatsapp_get_group_info` | Get group details and member list |
| `whatsapp_search_messages` | Full-text search across all chats |
| `whatsapp_get_starred_messages` | Get starred/important messages |
| `whatsapp_get_chat_statistics` | Chat stats (counts, date range, top senders) |
| `whatsapp_check_status` | Check bridge connection status |
| `whatsapp_send_message` | Send a WhatsApp message |
| `whatsapp_get_incoming_messages` | Get recent real-time incoming messages |
| `whatsapp_transcribe_voice_message` | Transcribe a voice note to text |
| `whatsapp_get_unread_summary` | Summary of all unread chats |
| `whatsapp_schedule_message` | Schedule a message for future delivery |
| `whatsapp_list_scheduled_messages` | List pending scheduled messages |
| `whatsapp_cancel_scheduled_message` | Cancel a scheduled message |
| `whatsapp_schedule_broadcast` | Schedule messages to multiple recipients |
| `whatsapp_rewrite_message` | Rewrite a message with a different tone |

### Resources

| URI | Description |
|-----|-------------|
| `whatsapp://status` | Bridge connection status |
| `whatsapp://unread` | Unread messages summary |
| `whatsapp://scheduled` | Pending scheduled messages |
| `whatsapp://settings` | Assistant settings |
| `whatsapp://chats/recent` | 20 most recent chats |

### Prompts

| Prompt | Description |
|--------|-------------|
| `catch_me_up` | Summarize all unread messages |
| `send_a_message` | Guided message-sending workflow |

## How It Works

```
Claude Desktop / Cursor / any MCP client
         │
         │ stdio (JSON-RPC)
         ▼
     MCP Server (mcp_server.py)
         │
         ├──→ WhatsApp Bridge (:3010) ──→ WhatsApp Web
         │
         └──→ SQLite DBs (local WhatsApp data)


Browser UI ──→ FastAPI Server (:3009) ──→ Nebius LLM
         │                                    │
         │                              Tool calls
         │
         ├──→ WhatsApp Bridge (:3010) ──→ WhatsApp Web
         │
         └──→ SQLite DBs (local WhatsApp data)

Voice ──→ Mic → STT → Server → LLM + Tools → TTS → Speaker
```

The server reads your local WhatsApp database (macOS WhatsApp app required) and connects to WhatsApp Web via the Baileys bridge for sending messages. The LLM uses tool calling to search contacts, read conversations, send replies, and schedule messages.

## Voice

The voice mode listens for a configurable wake word (default: "hey whatsapp") and processes spoken commands through the AI with full tool access.

**Settings** (configurable in the browser UI):
- **Assistant Name** — Give it a name (e.g., Jarvis, Friday)
- **Wake Word** — Trigger phrase (e.g., "hey whatsapp", "ok jarvis")
- **STT Engine** — Google Web Speech, Apple On-Device, or Whisper
- **TTS Voice** — Any macOS system voice
- **Auto-listen** — Keep listening after responding

## Requirements

- macOS (Apple Silicon or Intel) for local WhatsApp DB access
- WhatsApp desktop app installed and logged in
- [Nebius API key](https://studio.nebius.com) (for LLM and message rewriting)
- Python 3.10+ with `mcp` package (for MCP server mode)

## Project Structure

```
~/.wa/                          # Installation directory
├── app/                        # Git clone of this repo
│   ├── mcp_server.py           # MCP server (stdio/SSE)
│   ├── app/                    # Python FastAPI backend
│   │   ├── main.py             # Server + embedded UI
│   │   ├── agent.py            # LLM agent with tool calling
│   │   ├── tools.py            # WhatsApp tools (search, send, etc.)
│   │   ├── db.py               # SQLite database access
│   │   ├── config.py           # Configuration
│   │   ├── scheduler.py        # Scheduled messages
│   │   └── settings.py         # User settings
│   ├── bridge/                 # Node.js WhatsApp bridge (Baileys)
│   │   └── src/server.ts       # Express API for WhatsApp Web
│   ├── voice/                  # Voice mode
│   │   ├── assistant.py        # Main voice loop
│   │   └── apple_stt.swift     # Native macOS speech-to-text
│   ├── launcher/               # CLI and menu bar
│   │   ├── wa                  # CLI launcher script
│   │   └── wa-menubar.py       # Menu bar controller (rumps)
│   └── installer/              # Install/uninstall scripts
│       ├── install.sh          # Installer
│       └── uninstall.sh        # Uninstaller
├── venv/                       # Python virtual environment
├── .env                        # API key
├── logs/                       # server.log, bridge.log, voice.log
├── run/                        # PID files
└── version                     # Installed version
```

## Uninstall

```bash
wa uninstall
```

Or manually:
```bash
rm -f /usr/local/bin/wa
rm -rf ~/.wa
rm -rf /tmp/whatsapp-assistant-db
rm -f ~/Library/LaunchAgents/com.wa-assistant.*.plist
```
