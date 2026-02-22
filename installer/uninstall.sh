#!/bin/bash
# WhatsApp MCP — Uninstaller
# Removes all WhatsApp MCP files and services.

set -euo pipefail

RED='\033[0;31m'
GREEN='\033[0;32m'
BOLD='\033[1m'
NC='\033[0m'

echo -e "${BOLD}Uninstalling WhatsApp MCP...${NC}"
echo ""

# Stop running services
if command -v wa &>/dev/null; then
    wa stop 2>/dev/null || true
fi

# Remove LaunchAgent plists
rm -f ~/Library/LaunchAgents/com.wa-assistant.*.plist 2>/dev/null || true
echo -e "${GREEN}  ✓${NC} Removed launch agents"

# Remove CLI symlink
rm -f /usr/local/bin/wa 2>/dev/null || true
rm -f "$HOME/.local/bin/wa" 2>/dev/null || true
echo -e "${GREEN}  ✓${NC} Removed wa command"

# Remove installation directory
rm -rf "$HOME/.wa"
echo -e "${GREEN}  ✓${NC} Removed ~/.wa"

# Remove temp database directory
rm -rf /tmp/whatsapp-assistant-db
echo -e "${GREEN}  ✓${NC} Removed temp files"

echo ""
echo -e "${GREEN}WhatsApp MCP has been uninstalled.${NC}"
echo -e "  Note: Homebrew packages (python, node, portaudio) were not removed."
echo -e "  To remove them: brew uninstall python@3 node portaudio"
