#!/usr/bin/env python3
"""
Voice assistant for WhatsApp MCP.

Run in a separate terminal alongside the main server (./run.sh).
Say the wake word (default "hey whatsapp") followed by a command, and hear
the assistant's spoken response via macOS TTS.

Usage:
    source venv/bin/activate
    python3 voice/assistant.py
    python3 voice/assistant.py --server http://localhost:3009

Requires:
    brew install portaudio
    pip3 install SpeechRecognition pyaudio
"""

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import urllib.request
import uuid

try:
    import speech_recognition as sr
except ImportError:
    print("ERROR: SpeechRecognition not installed. Run: pip install SpeechRecognition pyaudio")
    sys.exit(1)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def fetch_settings(server: str) -> dict:
    """Load settings from the running server."""
    defaults = {
        "wake_word": "hey whatsapp",
        "stt_engine": "google",
        "tts_voice": "Samantha",
        "tts_speed": 190,
        "auto_listen": True,
        "sound_feedback": True,
    }
    try:
        req = urllib.request.Request(f"{server}/api/settings")
        with urllib.request.urlopen(req, timeout=3) as resp:
            data = json.loads(resp.read().decode())
            return {**defaults, **data}
    except Exception:
        return defaults


def extract_tts(text: str) -> tuple[str, str]:
    """
    Extract <tts>...</tts> content from response.
    Returns (tts_text, display_text).
    - tts_text: what to speak (from <tts> tags, or full text if no tags)
    - display_text: full response with <tts> tags stripped for UI display
    """
    match = re.search(r'<tts>(.*?)</tts>', text, re.DOTALL)
    # Strip <tts> tags from display text
    display = re.sub(r'\s*<tts>.*?</tts>\s*', '', text, flags=re.DOTALL).strip()
    if match:
        return match.group(1).strip(), display or text
    # No <tts> tags — speak the full text
    return text, text


def _clean_for_tts(text: str) -> str:
    """Clean text for TTS: strip markdown, JIDs, emoji."""
    clean = text
    clean = re.sub(r'\s*\(?[\w.+-]+@[\w.]+\)?\s*', ' ', clean)
    clean = clean.replace("**", "").replace("`", "")
    clean = re.sub(r'^\s*>\s?', '', clean, flags=re.MULTILINE)
    clean = clean.replace("*", "")
    clean = re.sub(r'[^\x00-\x7F\u00C0-\u024F\u0900-\u097F]+', ' ', clean)
    clean = re.sub(r'[ \t]+', ' ', clean).strip()
    if len(clean) > 2000:
        clean = clean[:2000] + "... I've truncated the rest for brevity."
    return clean


def speak(text: str, voice: str = "Samantha", speed: int = 190):
    """Speak text using macOS `say` command."""
    clean = _clean_for_tts(text)
    if clean:
        subprocess.run(["say", "-v", voice, "-r", str(speed), clean])


def beep():
    """Play a short beep sound to indicate wake word detected."""
    subprocess.run(
        ["afplay", "/System/Library/Sounds/Tink.aiff"],
        stderr=subprocess.DEVNULL,
    )


def push_voice_event(server: str, event: dict):
    """Push a voice event to the server for the UI to display."""
    try:
        payload = json.dumps(event).encode()
        req = urllib.request.Request(
            f"{server}/api/voice/event",
            data=payload,
            headers={"Content-Type": "application/json"},
        )
        urllib.request.urlopen(req, timeout=3)
    except Exception:
        pass  # Non-critical — UI display is best-effort


def send_chat(server: str, message: str, conversation_id: str) -> tuple[str, str]:
    """Stream /api/chat/stream, print live progress, push events to UI."""
    # Push user message to UI
    push_voice_event(server, {"type": "voice_user", "text": message})

    payload = json.dumps({
        "message": message,
        "conversation_id": conversation_id,
    }).encode()
    req = urllib.request.Request(
        f"{server}/api/chat/stream",
        data=payload,
        headers={"Content-Type": "application/json"},
    )
    resp = urllib.request.urlopen(req, timeout=120)
    conv_id = conversation_id
    final_content = ""
    tool_count = 0
    tool_calls = []
    tool_results = []

    for raw_line in resp:
        line = raw_line.decode("utf-8").strip()
        if not line.startswith("data: "):
            continue
        data_str = line[6:].strip()
        if data_str == "[DONE]":
            break
        try:
            ev = json.loads(data_str)
        except json.JSONDecodeError:
            continue

        etype = ev.get("type")
        if etype == "conv_id":
            conv_id = ev.get("conversation_id", conv_id)
        elif etype == "tool_call":
            tool_count += 1
            name = ev.get("name", "?")
            args = ev.get("arguments", {})
            tool_calls.append({"name": name, "arguments": args})
            arg_summary = ", ".join(f"{k}={_short(v)}" for k, v in args.items()) if args else ""
            print(f"  [{tool_count}] Calling {name}({arg_summary})")
            push_voice_event(server, {"type": "voice_tool_call", "name": name, "arguments": args})
        elif etype == "tool_result":
            name = ev.get("name", "?")
            result = ev.get("result", "")
            tool_results.append(result)
            print(f"      -> {name} returned {len(result)} chars")
            push_voice_event(server, {"type": "voice_tool_result", "name": name})
        elif etype == "message":
            final_content = ev.get("content", "")
        elif etype == "error":
            final_content = f"Error: {ev.get('content', 'unknown error')}"

    resp.close()
    if not final_content:
        final_content = "Sorry, I got an empty response."

    # Separate TTS content from display content
    tts_text, display_text = extract_tts(final_content)

    # Push clean display text to UI (no <tts> tags)
    push_voice_event(server, {
        "type": "voice_assistant",
        "text": display_text,
        "tool_calls": tool_calls,
    })

    return tts_text, display_text, conv_id


def _short(v, max_len=30):
    """Shorten a value for display."""
    s = str(v)
    if len(s) > max_len:
        return s[:max_len] + "..."
    return s


def listen_and_transcribe(recognizer, mic, recognize_fn, timeout=None, phrase_limit=10):
    """Listen for speech and transcribe. Returns text or None."""
    try:
        with mic as source:
            audio = recognizer.listen(source, timeout=timeout, phrase_time_limit=phrase_limit)
    except sr.WaitTimeoutError:
        return None
    except KeyboardInterrupt:
        raise

    try:
        text = recognize_fn(recognizer, audio).strip()
        return text if text else None
    except sr.UnknownValueError:
        return None
    except sr.RequestError as e:
        print(f"  STT error: {e}")
        return None
    except Exception as e:
        print(f"  STT error: {e}")
        return None


# ---------------------------------------------------------------------------
# Fuzzy wake word matching
# ---------------------------------------------------------------------------

def _edit_distance(a: str, b: str) -> int:
    """Simple Levenshtein distance."""
    if len(a) < len(b):
        return _edit_distance(b, a)
    if not b:
        return len(a)
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a):
        curr = [i + 1]
        for j, cb in enumerate(b):
            curr.append(min(prev[j + 1] + 1, curr[j] + 1, prev[j] + (ca != cb)))
        prev = curr
    return prev[-1]


def match_wake_word(transcript: str, wake_word: str) -> tuple[bool, str]:
    """
    Fuzzy-match wake word at the start of transcript.
    Returns (matched, remaining_text).

    Handles common STT misheards via per-word edit distance tolerance.
    Uses per-word edit distance tolerance:
      - words <= 3 chars: exact or distance 1
      - words > 3 chars: distance <= 1
    """
    t_words = transcript.lower().split()
    w_words = wake_word.lower().split()

    if len(t_words) < len(w_words):
        return False, transcript

    matched = True
    for i, ww in enumerate(w_words):
        tw = t_words[i]
        if tw == ww:
            continue
        dist = _edit_distance(tw, ww)
        # Allow edit distance of 1 for any word
        if dist <= 1:
            continue
        matched = False
        break

    if not matched:
        return False, transcript

    # Reconstruct remaining text preserving original casing
    # Find where the wake word ends in the original transcript
    original_words = transcript.split()
    remaining = " ".join(original_words[len(w_words):]).strip().lstrip(",").strip()
    return True, remaining


# ---------------------------------------------------------------------------
# STT backends
# ---------------------------------------------------------------------------

def recognize_google(recognizer: "sr.Recognizer", audio: "sr.AudioData") -> str:
    """Google Web Speech API (free, sends audio to Google)."""
    return recognizer.recognize_google(audio)


def recognize_apple(recognizer: "sr.Recognizer", audio: "sr.AudioData") -> str:
    """Apple on-device STT via compiled Swift helper."""
    helper = os.path.join(os.path.dirname(__file__), "apple_stt")
    if not os.path.isfile(helper):
        raise FileNotFoundError(
            "Apple STT helper not compiled. Run:\n"
            "  swiftc voice/apple_stt.swift -o voice/apple_stt "
            "-framework Speech -framework AVFoundation"
        )
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
        f.write(audio.get_wav_data())
        tmp_path = f.name
    try:
        result = subprocess.run(
            [helper, tmp_path],
            capture_output=True, text=True, timeout=30,
        )
        text = result.stdout.strip()
        if result.returncode != 0 or not text:
            raise RuntimeError(result.stderr.strip() or "Apple STT returned no text")
        return text
    finally:
        os.unlink(tmp_path)


def recognize_whisper(recognizer: "sr.Recognizer", audio: "sr.AudioData") -> str:
    """Local Whisper model via speech_recognition."""
    return recognizer.recognize_whisper(audio, model="base", language="english")


STT_BACKENDS = {
    "google": recognize_google,
    "apple": recognize_apple,
    "whisper": recognize_whisper,
}


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Voice assistant for WhatsApp MCP")
    parser.add_argument("--server", default="http://localhost:3009", help="Server URL")
    args = parser.parse_args()
    server = args.server.rstrip("/")

    # Check server is reachable
    print(f"Connecting to server at {server}...")
    try:
        req = urllib.request.Request(f"{server}/api/settings")
        urllib.request.urlopen(req, timeout=5)
    except Exception as e:
        print(f"ERROR: Cannot reach server at {server}: {e}")
        print("Make sure the server is running (./run.sh)")
        sys.exit(1)

    recognizer = sr.Recognizer()
    mic = sr.Microphone()

    # Calibrate for ambient noise
    print("Calibrating microphone for ambient noise...")
    with mic as source:
        recognizer.adjust_for_ambient_noise(source, duration=1)
    print("Calibration done.")
    print("Mic is only active when speech is detected (energy-based VAD).\n")

    conversation_id = str(uuid.uuid4())
    in_conversation = False  # True = follow-up mode (no wake word needed)

    while True:
        # Reload settings each cycle
        settings = fetch_settings(server)
        wake_word = settings["wake_word"].lower().strip()
        stt_engine = settings["stt_engine"]
        tts_voice = settings["tts_voice"]
        tts_speed = settings["tts_speed"]
        auto_listen = settings["auto_listen"]
        sound_feedback = settings["sound_feedback"]
        follow_up_timeout = settings.get("follow_up_timeout", 3)
        recognize_fn = STT_BACKENDS.get(stt_engine, recognize_google)

        # ----- FOLLOW-UP MODE (no wake word needed) -----
        if in_conversation and auto_listen:
            print(f"  Listening for follow-up ({follow_up_timeout}s)...")
            transcript = listen_and_transcribe(
                recognizer, mic, recognize_fn,
                timeout=follow_up_timeout, phrase_limit=15,
            )
            if transcript is None:
                # Silence — drop back to wake word mode
                print("  No follow-up heard, back to wake word mode.")
                in_conversation = False
                continue

            print(f"  Heard: {transcript}")
            command = transcript

            # Allow "nevermind" / "stop" / "bye" to exit conversation mode
            lower = command.lower().strip()
            if lower in ("never mind", "nevermind", "stop", "bye", "cancel", "that's all", "nothing"):
                print("  Ending conversation.")
                speak("Okay!", tts_voice, tts_speed)
                in_conversation = False
                continue

            # If they said the wake word again, strip it
            wk_match, wk_rest = match_wake_word(command, wake_word)
            if wk_match:
                command = wk_rest
                if not command:
                    speak("Yes?", tts_voice, tts_speed)
                    continue

        # ----- WAKE WORD MODE -----
        else:
            in_conversation = False
            print(f'Listening for "{wake_word}"... (STT: {stt_engine})')

            try:
                transcript = listen_and_transcribe(
                    recognizer, mic, recognize_fn,
                    timeout=None, phrase_limit=10,
                )
            except KeyboardInterrupt:
                print("\nBye!")
                break

            if transcript is None:
                continue

            print(f"  Heard: {transcript}")

            # Fuzzy check for wake word
            matched, command = match_wake_word(transcript, wake_word)
            if not matched:
                continue

            print(f"  Wake word detected!")

            if sound_feedback:
                beep()

            if not command:
                # Wake word only — listen for command (no TTS prompt to avoid
                # cutting off the user who might already be speaking)
                print("  Listening for command...")
                command = listen_and_transcribe(
                    recognizer, mic, recognize_fn,
                    timeout=5, phrase_limit=15,
                )
                if not command:
                    print("  No command heard, going back to listening.")
                    continue

        if not command:
            continue

        print(f"  Command: {command}")
        print("  Processing...")

        # Send to assistant
        try:
            tts_text, display_text, conversation_id = send_chat(server, command, conversation_id)
        except Exception as e:
            print(f"  Chat error: {e}")
            speak("Sorry, I couldn't reach the assistant.", tts_voice, tts_speed)
            in_conversation = False
            continue

        if tts_text != display_text:
            print(f"  Response (UI): {display_text[:80]}...")
            print(f"  Speaking: {tts_text[:120]}{'...' if len(tts_text) > 120 else ''}")
        else:
            print(f"  Response: {display_text[:120]}{'...' if len(display_text) > 120 else ''}")

        # Speak only the TTS portion
        speak(tts_text, tts_voice, tts_speed)

        # Enter follow-up mode — next listen won't need wake word
        if auto_listen:
            in_conversation = True
        else:
            in_conversation = False
            input("  Press Enter to listen again...")


if __name__ == "__main__":
    main()
