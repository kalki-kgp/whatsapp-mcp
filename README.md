# WhatsApp MCP for macOS

<!-- mcp-name: io.github.kalki-kgp/whatsapp-macos -->

A [Model Context Protocol](https://modelcontextprotocol.io) server that connects Claude to your WhatsApp. Ask Claude to catch you up on unread chats, search your message history, or send a reply.

Reading works in about a minute with no QR scan and no Node.js. Sending is optional and needs one extra step.

<p align="center">
  <img src="https://img.shields.io/badge/platform-macOS-blue" alt="macOS">
  <img src="https://img.shields.io/badge/MCP-1.0-green" alt="MCP 1.0">
  <img src="https://img.shields.io/badge/license-MIT-lightgrey" alt="MIT License">
</p>

## Features

Search contacts by name or phone number. Read chat history with date filtering. List recent conversations with unread counts. Summarize everything you haven't read yet. Send replies and receive incoming messages once the bridge is connected.

## Requirements

- macOS with the WhatsApp desktop app installed and logged in
- Node.js 18+, only if you want to send messages

You don't need to install Python. `uvx` fetches a suitable version on its own.

## Installation

Install [uv](https://docs.astral.sh/uv/) if you don't have it:

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

That's the whole install. `uvx` downloads the server on first launch, so there's no virtualenv to create and no `pip install` step. Go to the next section to point Claude at it.

### From source

```bash
git clone https://github.com/kalki-kgp/whatsapp-mcp.git
cd whatsapp-mcp
uv venv && uv pip install -e .
```

## Connect to Claude Desktop

1. Open config file:
   ```bash
   open ~/Library/Application\ Support/Claude/claude_desktop_config.json
   ```
   If it doesn't exist, create it.

2. Find the full path to `uvx`:
   ```bash
   which uvx
   ```

3. Add the WhatsApp MCP server, pasting that path as the command:
   ```json
   {
     "mcpServers": {
       "whatsapp": {
         "command": "/Users/YOU/.local/bin/uvx",
         "args": ["whatsapp-mcp-macos"]
       }
     }
   }
   ```

   Use the absolute path, not bare `uvx`. Claude Desktop starts with a minimal PATH that doesn't include `~/.local/bin`, so a bare command name is the most common reason the server never appears.

4. Restart Claude Desktop with Cmd+Q, then reopen. Closing the window is not enough.

5. Click the MCP tools icon in the chat input and check that "whatsapp" is listed.

6. Start chatting:
   - "Show my recent WhatsApp chats"
   - "Search messages for dinner plans"

## Connect to Cursor

Add to `.cursor/mcp.json` in your project:

```json
{
  "mcpServers": {
    "whatsapp": {
      "command": "/Users/YOU/.local/bin/uvx",
      "args": ["whatsapp-mcp-macos"]
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

Read operations query the local WhatsApp database directly, so they work offline and need no bridge.

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

- All data stays local. The server reads messages from your own WhatsApp database
- No data is sent to external servers (except WhatsApp Web when sending)
- The MCP server runs locally on your machine

## License

MIT
