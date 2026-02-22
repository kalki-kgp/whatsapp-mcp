# WhatsApp Assistant

AI-powered WhatsApp assistant for macOS with voice control. Chat with an AI that can read your WhatsApp messages, send replies, search contacts, schedule messages, and more — all from your browser or by voice.

## Features

- **AI Chat** — Ask questions about your WhatsApp conversations, send messages, search contacts
- **WhatsApp Bridge** — Connects via WhatsApp Web (Baileys) for sending/receiving messages
- **Voice Assistant** — Wake word detection, speech-to-text (Apple/Google), text-to-speech via macOS
- **Scheduled Messages** — Schedule messages to be sent at a specific time
- **Browser UI** — Clean web interface at `localhost:3009`
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
wa voice          # Start voice assistant (say "hey assistant" + command)
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
                    │
                    ├──→ WhatsApp Bridge (:3010) ──→ WhatsApp Web
                    │
                    └──→ SQLite DBs (local WhatsApp data)

Voice Assistant ──→ Mic → STT → Server → LLM → TTS → Speaker
```

The server reads your local WhatsApp database (macOS WhatsApp app required) and connects to WhatsApp Web via the Baileys bridge for sending messages. The AI uses tool calling to search contacts, read messages, send replies, and schedule messages.

## Voice Assistant

The voice assistant listens for a wake word (default: "hey assistant") and then processes your spoken command through the AI.

**Settings** (configurable in the browser UI):
- **Assistant Name** — What you want to call it
- **Wake Word** — Trigger phrase (e.g., "hey assistant", "ok computer")
- **STT Engine** — Google Web Speech, Apple On-Device, or Whisper
- **TTS Voice** — Any macOS voice
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
│   │   ├── main.py             # Server + UI
│   │   ├── agent.py            # AI agent with tool calling
│   │   ├── tools.py            # WhatsApp tools (search, send, etc.)
│   │   ├── db.py               # SQLite database access
│   │   ├── config.py           # Configuration
│   │   ├── scheduler.py        # Scheduled messages
│   │   └── settings.py         # User settings
│   ├── bridge/                 # Node.js WhatsApp bridge (Baileys)
│   │   └── src/server.ts       # Express API for WhatsApp Web
│   ├── voice/                  # Voice assistant
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
