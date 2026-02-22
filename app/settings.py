import json
from pathlib import Path

from app.config import SETTINGS_FILE

DEFAULTS = {
    "assistant_name": "",            # user-chosen name for the voice assistant
    "wake_word": "hey whatsapp",
    "stt_engine": "google",       # "google" | "apple" | "whisper"
    "tts_voice": "Samantha",      # macOS voice name
    "tts_speed": 190,             # words per minute
    "auto_listen": True,          # re-listen after speaking response
    "sound_feedback": True,       # beep on wake word detection
    "follow_up_timeout": 3,       # seconds to wait for follow-up before returning to wake word
}


def get_settings() -> dict:
    """Load settings from disk, falling back to defaults for missing keys."""
    SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
    if SETTINGS_FILE.exists():
        try:
            with open(SETTINGS_FILE) as f:
                stored = json.load(f)
            # Merge with defaults so new keys are always present
            merged = {**DEFAULTS, **stored}
            return merged
        except (json.JSONDecodeError, OSError):
            pass
    return dict(DEFAULTS)


def update_settings(partial: dict) -> dict:
    """Partial update — merges with existing settings and writes to disk."""
    current = get_settings()
    current.update(partial)
    SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(SETTINGS_FILE, "w") as f:
        json.dump(current, f, indent=2)
    return current


def get_setting(key: str):
    """Get a single setting value."""
    return get_settings().get(key, DEFAULTS.get(key))
