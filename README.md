# WhatsApp MCP

An AI-powered MCP (Model Context Protocol) layer for WhatsApp on macOS. Gives an LLM full tool-calling access to your WhatsApp — read messages, search contacts, send replies, schedule messages — all through a browser UI or voice.

## Features

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

## How It Works

```
Browser UI ──→ FastAPI Server (:3009) ──→ Nebius LLM (Kimi-K2)
                    │                          │
                    │                    Tool calls:
                    │                     - search_contacts
                    │                     - get_messages
                    │                     - send_message
                    │                     - search_messages
                    │                     - get_unread_summary
                    │                     - schedule_message
                    │                     - ...
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

- macOS (Apple Silicon or Intel)
- WhatsApp desktop app installed and logged in
- [Nebius API key](https://studio.nebius.com) (for LLM)

## Project Structure

```
~/.wa/                          # Installation directory
├── app/                        # Git clone of this repo
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
