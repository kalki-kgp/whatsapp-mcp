# WhatsApp MCP for macOS

<!-- mcp-name: io.github.kalki-kgp/whatsapp-macos -->

A [Model Context Protocol](https://modelcontextprotocol.io) server that connects Claude to your WhatsApp. Read messages, search contacts, send replies — all through natural conversation.

<p align="center">
  <img src="https://img.shields.io/badge/platform-macOS-blue" alt="macOS">
  <img src="https://img.shields.io/badge/MCP-1.0-green" alt="MCP 1.0">
  <img src="https://img.shields.io/badge/license-MIT-lightgrey" alt="MIT License">
</p>

## Features

- **Search contacts** — Find anyone by name or phone number
- **Read messages** — Get chat history with date filtering and search
- **List chats** — See recent conversations with unread counts
- **Send messages** — Reply directly through Claude (with QR authentication)
- **Real-time incoming** — Get messages as they arrive

## Requirements

- **macOS** with WhatsApp desktop app installed and logged in
- **Python 3.10+**
- **Node.js 18+** (for sending messages)

## Installation

### Using pip

```bash
pip install whatsapp-mcp-macos
```

### From source

```bash
git clone https://github.com/kalki-kgp/whatsapp-mcp.git
cd whatsapp-mcp
pip install -e .
```

## Connect to Claude Desktop

1. Open config file:
   ```bash
   open ~/Library/Application\ Support/Claude/claude_desktop_config.json
   ```
   If it doesn't exist, create it.

2. Add the WhatsApp MCP server:
   ```json
   {
     "mcpServers": {
       "whatsapp": {
         "command": "python3",
         "args": ["-m", "whatsapp_mcp"]
       }
     }
   }
   ```

3. **Restart Claude Desktop** (Cmd+Q, then reopen)

4. Look for the **MCP tools icon** (🔨) in the chat input — click it to verify "whatsapp" is listed

5. Start chatting:
   - "Show my recent WhatsApp chats"
   - "Search messages for dinner plans"

## Connect to Cursor

Add to `.cursor/mcp.json` in your project:

```json
{
  "mcpServers": {
    "whatsapp": {
      "command": "python3",
      "args": ["-m", "whatsapp_mcp"]
    }
  }
}
```

Restart Cursor and use WhatsApp tools in the AI chat.

## Usage

### Reading messages (works immediately)

Just ask Claude:
- "Show my recent WhatsApp chats"
- "Search for messages about dinner"
- "What did John say yesterday?"
- "Catch me up on unread messages"

### Sending messages (requires bridge)

1. Start the WhatsApp bridge:
   ```bash
   cd bridge && npm install && npm start
   ```

2. Ask Claude to check connection:
   - "Check WhatsApp status"
   
3. If it shows a QR code, open the data URL in a browser and scan with your phone

4. Once connected, you can send:
   - "Send a message to Mom saying I'll be late"
   - "Reply to John with 'sounds good'"

## Tools

| Tool | Description | Requires Bridge |
|------|-------------|-----------------|
| `whatsapp_status` | Check connection, get QR if needed | No |
| `whatsapp_search_contacts` | Search contacts by name/phone | No |
| `whatsapp_list_chats` | List recent conversations | No |
| `whatsapp_get_messages` | Get messages from a chat | No |
| `whatsapp_search_messages` | Search across all chats | No |
| `whatsapp_unread` | Get unread message summary | No |
| `whatsapp_send` | Send a message | Yes |
| `whatsapp_incoming` | Get real-time incoming messages | Yes |

## How it works

```
Claude ──MCP──▶ WhatsApp MCP Server
                       │
                       ├──▶ Local SQLite DBs (read messages)
                       │    ~/Library/Group Containers/group.net.whatsapp.WhatsApp.shared/
                       │
                       └──▶ WhatsApp Bridge (:3010) ──▶ WhatsApp Web
                            (for sending)
```

**Read operations** query the local WhatsApp database directly — fast and works offline.

**Send operations** go through the bridge, which connects to WhatsApp Web using [Baileys](https://github.com/WhiskeySockets/Baileys).

## Development

```bash
# Clone
git clone https://github.com/kalki-kgp/whatsapp-mcp.git
cd whatsapp-mcp

# Install in dev mode
pip install -e ".[dev]"

# Run server
python -m whatsapp_mcp
```

## Privacy

- All data stays local — messages are read from your own WhatsApp database
- No data is sent to external servers (except WhatsApp Web when sending)
- The MCP server runs locally on your machine

## License

MIT
