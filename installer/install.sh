#!/bin/bash
# WhatsApp MCP — Installer for macOS
#
# Usage:
#   bash <(curl -fsSL https://raw.githubusercontent.com/kalki-kgp/whatsapp-assistant/main/installer/install.sh)
#
# Non-interactive:
#   NEBIUS_API_KEY=xxx bash <(curl -fsSL ...)

set -euo pipefail

WA_HOME="$HOME/.wa"
APP_DIR="$WA_HOME/app"
VENV_DIR="$WA_HOME/venv"
LOG_DIR="$WA_HOME/logs"
RUN_DIR="$WA_HOME/run"
REPO_URL="https://github.com/kalki-kgp/whatsapp-assistant.git"
BIN_LINK="/usr/local/bin/wa"

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[0;33m'
BLUE='\033[0;34m'
BOLD='\033[1m'
DIM='\033[2m'
NC='\033[0m'

log()  { echo -e "${BLUE}[wa]${NC} $*"; }
ok()   { echo -e "${GREEN}  ✓${NC} $*"; }
warn() { echo -e "${YELLOW}  !${NC} $*"; }
err()  { echo -e "${RED}  ✗${NC} $*" >&2; }
step() { echo -e "\n${BOLD}$*${NC}"; }

# --- banner ---

echo ""
echo -e "${BOLD}╭─────────────────────────────────────╮${NC}"
echo -e "${BOLD}│     WhatsApp MCP Installer      │${NC}"
echo -e "${BOLD}│              macOS only               │${NC}"
echo -e "${BOLD}╰─────────────────────────────────────╯${NC}"
echo ""

# --- preflight ---

step "1/9  Checking system..."

if [ "$(uname)" != "Darwin" ]; then
    err "This installer only supports macOS."
    exit 1
fi
ok "macOS detected ($(sw_vers -productVersion))"

# --- Xcode CLI Tools ---

step "2/9  Xcode Command Line Tools..."

if xcode-select -p &>/dev/null; then
    ok "Already installed"
else
    log "Installing Xcode Command Line Tools..."
    log "A dialog may appear — click 'Install' and wait."
    xcode-select --install 2>/dev/null || true
    # Wait for installation
    until xcode-select -p &>/dev/null; do
        sleep 5
    done
    ok "Installed"
fi

# --- Homebrew ---

step "3/9  Homebrew..."

if command -v brew &>/dev/null; then
    ok "Already installed"
else
    log "Installing Homebrew..."
    /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
    # Add to PATH for this session
    if [ -f "/opt/homebrew/bin/brew" ]; then
        eval "$(/opt/homebrew/bin/brew shellenv)"
    elif [ -f "/usr/local/bin/brew" ]; then
        eval "$(/usr/local/bin/brew shellenv)"
    fi
    ok "Installed"
fi

# --- brew deps ---

step "4/9  System dependencies..."

for pkg in python@3 node portaudio; do
    if brew list "$pkg" &>/dev/null; then
        ok "$pkg already installed"
    else
        log "Installing $pkg..."
        brew install "$pkg"
        ok "$pkg installed"
    fi
done

# --- migrate from old ~/.tanu installation ---

OLD_HOME="$HOME/.tanu"
if [ -d "$OLD_HOME" ] && [ ! -d "$WA_HOME" ]; then
    log "Migrating from ~/.tanu to ~/.wa..."
    # Stop old services
    if command -v tanu &>/dev/null; then
        tanu stop 2>/dev/null || true
    fi
    mv "$OLD_HOME" "$WA_HOME"
    # Remove old symlink
    rm -f /usr/local/bin/tanu 2>/dev/null || true
    rm -f "$HOME/.local/bin/tanu" 2>/dev/null || true
    # Remove old LaunchAgent
    rm -f ~/Library/LaunchAgents/com.tanu.*.plist 2>/dev/null || true
    ok "Migrated ~/.tanu → ~/.wa"
elif [ -d "$OLD_HOME" ] && [ -d "$WA_HOME" ]; then
    # Both exist — clean up old one
    warn "Found old ~/.tanu alongside ~/.wa — removing old installation"
    if command -v tanu &>/dev/null; then
        tanu stop 2>/dev/null || true
    fi
    rm -rf "$OLD_HOME"
    rm -f /usr/local/bin/tanu 2>/dev/null || true
    rm -f "$HOME/.local/bin/tanu" 2>/dev/null || true
    rm -f ~/Library/LaunchAgents/com.tanu.*.plist 2>/dev/null || true
    ok "Cleaned up old ~/.tanu"
fi

# --- directory structure ---

step "5/9  Setting up ~/.wa/..."

mkdir -p "$WA_HOME" "$LOG_DIR" "$RUN_DIR"
ok "Created $WA_HOME"

# --- clone or update repo ---

step "6/9  Application code..."

if [ -d "$APP_DIR/.git" ]; then
    log "Updating existing installation..."
    cd "$APP_DIR"
    git pull origin main
    ok "Updated to latest"
else
    if [ -d "$APP_DIR" ]; then
        rm -rf "$APP_DIR"
    fi
    log "Cloning repository..."
    git clone "$REPO_URL" "$APP_DIR"
    ok "Cloned"
fi

cd "$APP_DIR"
git rev-parse --short HEAD > "$WA_HOME/version"

# --- Python venv + deps ---

step "7/9  Python environment..."

if [ ! -d "$VENV_DIR" ]; then
    log "Creating virtual environment..."
    python3 -m venv "$VENV_DIR"
    ok "Created venv"
else
    ok "Venv already exists"
fi

log "Installing Python dependencies..."
"$VENV_DIR/bin/pip" install --upgrade pip -q
"$VENV_DIR/bin/pip" install -r requirements.txt -q
ok "Python dependencies installed"

# --- Node deps ---

step "8/9  Bridge dependencies..."

if [ ! -d "$APP_DIR/bridge/node_modules" ]; then
    log "Installing npm dependencies..."
    (cd "$APP_DIR/bridge" && npm install --silent)
    ok "npm dependencies installed"
else
    ok "npm dependencies already installed"
fi

# --- Compile Swift STT (optional) ---

log "Compiling Apple STT helper..."
if swiftc "$APP_DIR/voice/apple_stt.swift" -o "$APP_DIR/voice/apple_stt" \
    -framework Speech -framework AVFoundation 2>/dev/null; then
    ok "Apple STT compiled"
else
    warn "Swift compilation failed (voice will use Google STT fallback)"
fi

# --- API Key ---

step "9/9  Configuration..."

ENV_FILE="$WA_HOME/.env"

if [ -n "${NEBIUS_API_KEY:-}" ]; then
    echo "NEBIUS_API_KEY=$NEBIUS_API_KEY" > "$ENV_FILE"
    ok "API key set from environment"
elif [ -f "$ENV_FILE" ] && grep -q "NEBIUS_API_KEY=." "$ENV_FILE"; then
    ok "API key already configured"
else
    echo ""
    echo -e "  ${BOLD}Enter your Nebius API key${NC}"
    echo -e "  ${DIM}(Get one at https://studio.nebius.com)${NC}"
    echo ""
    read -rp "  NEBIUS_API_KEY: " api_key
    if [ -z "$api_key" ]; then
        warn "No API key provided. Set it later in $ENV_FILE"
        echo "NEBIUS_API_KEY=" > "$ENV_FILE"
    else
        echo "NEBIUS_API_KEY=$api_key" > "$ENV_FILE"
        ok "API key saved"
    fi
fi

# Symlink .env into app directory so dotenv can find it
ln -sf "$ENV_FILE" "$APP_DIR/.env"

# --- Symlink CLI ---

log "Installing 'wa' command..."
chmod +x "$APP_DIR/launcher/wa"

if ln -sf "$APP_DIR/launcher/wa" "$BIN_LINK" 2>/dev/null; then
    ok "Installed: wa → $BIN_LINK"
elif sudo ln -sf "$APP_DIR/launcher/wa" "$BIN_LINK" 2>/dev/null; then
    ok "Installed: wa → $BIN_LINK (via sudo)"
else
    # Fallback: use ~/.local/bin
    mkdir -p "$HOME/.local/bin"
    ln -sf "$APP_DIR/launcher/wa" "$HOME/.local/bin/wa"
    ok "Installed: wa → ~/.local/bin/wa"
    if [[ ":$PATH:" != *":$HOME/.local/bin:"* ]]; then
        warn "Add this to your shell profile:  export PATH=\"\$HOME/.local/bin:\$PATH\""
    fi
fi

# --- Mic permission prompt ---

log "Triggering microphone permission prompt..."
# Brief recording attempt to trigger macOS permission dialog
"$VENV_DIR/bin/python" -c "
import pyaudio, time
try:
    p = pyaudio.PyAudio()
    s = p.open(format=pyaudio.paInt16, channels=1, rate=16000, input=True, frames_per_buffer=1024)
    s.read(1024)
    s.stop_stream()
    s.close()
    p.terminate()
except Exception:
    pass
" 2>/dev/null || true
ok "Microphone permission requested"

# --- Done ---

echo ""
echo -e "${BOLD}╭─────────────────────────────────────╮${NC}"
echo -e "${BOLD}│          Installation Complete!       │${NC}"
echo -e "${BOLD}╰─────────────────────────────────────╯${NC}"
echo ""
echo -e "  ${BOLD}Get started:${NC}"
echo ""
echo -e "    ${GREEN}wa${NC}              Start (opens browser)"
echo -e "    ${GREEN}wa status${NC}       Check what's running"
echo -e "    ${GREEN}wa voice${NC}        Start voice assistant"
echo -e "    ${GREEN}wa menubar${NC}      Launch menu bar icon"
echo -e "    ${GREEN}wa help${NC}         See all commands"
echo ""
echo -e "  ${DIM}Installed to: $WA_HOME${NC}"
echo -e "  ${DIM}Version: $(cat "$WA_HOME/version")${NC}"
echo ""
