#!/usr/bin/env python3
"""
WhatsApp MCP — macOS Menu Bar Controller

Provides a menu bar icon with status and controls for the WhatsApp MCP.
Requires: pip install rumps

Launch via: wa menubar
"""

import json
import os
import subprocess
import threading
import urllib.request

import rumps

SERVER_URL = "http://127.0.0.1:3009"
BRIDGE_URL = "http://localhost:3010"
WA_HOME = os.path.expanduser("~/.wa")
PLIST_PATH = os.path.expanduser("~/Library/LaunchAgents/com.wa-assistant.menubar.plist")


def run_wa(*args):
    """Run a wa CLI command in background."""
    subprocess.Popen(
        ["wa", *args],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def fetch_json(url, timeout=2):
    """Fetch JSON from URL, return dict or None on failure."""
    try:
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read())
    except Exception:
        return None


class WAMenuBar(rumps.App):
    def __init__(self):
        super().__init__("WA", title="WA", quit_button=None)

        self.status_item = rumps.MenuItem("Status: Checking...")
        self.whatsapp_item = rumps.MenuItem("WhatsApp: Checking...")
        self.start_item = rumps.MenuItem("Start Server", callback=self.on_start)
        self.stop_item = rumps.MenuItem("Stop Server", callback=self.on_stop)
        self.restart_item = rumps.MenuItem("Restart Server", callback=self.on_restart)
        self.voice_start = rumps.MenuItem("Start Voice", callback=self.on_voice_start)
        self.voice_stop = rumps.MenuItem("Stop Voice", callback=self.on_voice_stop)
        self.open_browser = rumps.MenuItem("Open in Browser", callback=self.on_open_browser)
        self.open_logs = rumps.MenuItem("Open Logs", callback=self.on_open_logs)
        self.check_updates = rumps.MenuItem("Check for Updates", callback=self.on_check_updates)
        self.login_toggle = rumps.MenuItem("Start at Login", callback=self.on_toggle_login)
        self.quit_item = rumps.MenuItem("Quit", callback=self.on_quit)

        voice_menu = rumps.MenuItem("Voice Assistant")
        voice_menu.add(self.voice_start)
        voice_menu.add(self.voice_stop)

        self.menu = [
            self.status_item,
            self.whatsapp_item,
            None,  # separator
            self.open_browser,
            None,
            self.start_item,
            self.stop_item,
            self.restart_item,
            None,
            voice_menu,
            None,
            self.open_logs,
            self.check_updates,
            self.login_toggle,
            None,
            self.quit_item,
        ]

        # Check current login item state
        self.login_toggle.state = os.path.exists(PLIST_PATH)

        # Start polling
        self._poll_timer = rumps.Timer(self._poll_status, 5)
        self._poll_timer.start()
        # Immediate first poll
        threading.Thread(target=self._poll_status, args=(None,), daemon=True).start()

    def _poll_status(self, _):
        """Poll health and bridge status."""
        health = fetch_json(f"{SERVER_URL}/api/health")
        if health and health.get("status") == "ok":
            version = health.get("version", "?")
            self.status_item.title = f"Status: Running (v{version})"
            self.title = "WA"
        else:
            self.status_item.title = "Status: Stopped"
            self.title = "WA"

        bridge = fetch_json(f"{BRIDGE_URL}/api/status")
        if bridge:
            state = bridge.get("status", "unknown")
            labels = {
                "connected": "Connected",
                "qr_pending": "QR Pending",
                "disconnected": "Disconnected",
            }
            self.whatsapp_item.title = f"WhatsApp: {labels.get(state, state)}"
        else:
            self.whatsapp_item.title = "WhatsApp: Offline"

    def on_start(self, _):
        run_wa("start")
        rumps.notification("WhatsApp MCP", "", "Starting server...")

    def on_stop(self, _):
        run_wa("stop")
        rumps.notification("WhatsApp MCP", "", "Stopping server...")

    def on_restart(self, _):
        run_wa("restart")
        rumps.notification("WhatsApp MCP", "", "Restarting server...")

    def on_voice_start(self, _):
        """Open Terminal.app with wa voice for proper mic permissions."""
        script = 'tell application "Terminal" to do script "wa voice"'
        subprocess.Popen(["osascript", "-e", script])

    def on_voice_stop(self, _):
        run_wa("voice", "stop")

    def on_open_browser(self, _):
        subprocess.Popen(["open", f"{SERVER_URL}"])

    def on_open_logs(self, _):
        log_dir = os.path.join(WA_HOME, "logs")
        subprocess.Popen(["open", log_dir])

    def on_check_updates(self, _):
        rumps.notification("WhatsApp MCP", "", "Checking for updates...")
        threading.Thread(target=self._do_update, daemon=True).start()

    def _do_update(self):
        result = subprocess.run(
            ["wa", "update"],
            capture_output=True, text=True
        )
        output = result.stdout.strip().split("\n")[-1] if result.stdout.strip() else "Update check complete."
        rumps.notification("WhatsApp MCP", "", output)

    def on_toggle_login(self, sender):
        if sender.state:
            # Remove login item
            try:
                os.remove(PLIST_PATH)
            except FileNotFoundError:
                pass
            sender.state = False
        else:
            # Create LaunchAgent plist for menubar
            plist = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.wa-assistant.menubar</string>
    <key>ProgramArguments</key>
    <array>
        <string>/usr/local/bin/wa</string>
        <string>menubar</string>
    </array>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <false/>
</dict>
</plist>"""
            os.makedirs(os.path.dirname(PLIST_PATH), exist_ok=True)
            with open(PLIST_PATH, "w") as f:
                f.write(plist)
            sender.state = True

    def on_quit(self, _):
        rumps.quit_application()


if __name__ == "__main__":
    WAMenuBar().run()
