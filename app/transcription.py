import mimetypes
from typing import Optional

import httpx

from app.config import (
    BRIDGE_URL,
    DEFAULT_VOICE_NOTE_STT_MODEL,
    VOLX_API_KEY,
    VOLX_BASE_URL,
)
from app.settings import get_setting


def _get_stt_provider() -> str:
    provider = get_setting("voice_note_stt_provider")
    if isinstance(provider, str) and provider.strip():
        return provider.strip().lower()
    return "volx"


def _get_stt_model() -> str:
    model = get_setting("voice_note_stt_model")
    if isinstance(model, str) and model.strip():
        return model.strip()
    return DEFAULT_VOICE_NOTE_STT_MODEL


def _bridge_params(chat_jid: Optional[str], participant_jid: Optional[str]) -> dict:
    params: dict[str, str] = {}
    if chat_jid:
        params["chatJid"] = chat_jid
    if participant_jid:
        params["participantJid"] = participant_jid
    return params


def _raise_bridge_error(response: httpx.Response) -> None:
    try:
        payload = response.json()
    except Exception:
        payload = {}
    message = payload.get("error") if isinstance(payload, dict) else None
    detail = message or response.text or f"Bridge request failed with {response.status_code}"
    raise RuntimeError(detail)


def get_bridge_message_metadata(
    message_id: str,
    chat_jid: Optional[str] = None,
    participant_jid: Optional[str] = None,
) -> dict:
    response = httpx.get(
        f"{BRIDGE_URL}/api/messages/{message_id}",
        params=_bridge_params(chat_jid, participant_jid),
        timeout=15,
    )
    if response.status_code != 200:
        _raise_bridge_error(response)
    payload = response.json()
    message = payload.get("message")
    if not isinstance(message, dict):
        raise RuntimeError("Bridge did not return voice message metadata")
    return message


def download_bridge_message_media(
    message_id: str,
    chat_jid: Optional[str] = None,
    participant_jid: Optional[str] = None,
) -> tuple[bytes, str]:
    response = httpx.get(
        f"{BRIDGE_URL}/api/messages/{message_id}/media",
        params=_bridge_params(chat_jid, participant_jid),
        timeout=60,
    )
    if response.status_code != 200:
        _raise_bridge_error(response)
    content_type = response.headers.get("content-type", "application/octet-stream").split(";")[0].strip()
    return response.content, content_type


def transcribe_audio_bytes(
    audio_bytes: bytes,
    *,
    content_type: str,
    filename: str = "voice-note.ogg",
    language: Optional[str] = None,
) -> dict:
    provider = _get_stt_provider()
    if provider != "volx":
        raise RuntimeError(f"Unsupported voice-note STT provider: {provider}")

    url = f"{VOLX_BASE_URL.rstrip('/')}/v1/transcribe"
    headers = {"Authorization": f"Bearer {VOLX_API_KEY}"} if VOLX_API_KEY else {}
    data: dict[str, str] = {}
    model = _get_stt_model()
    if model:
        data["model"] = model
    if language:
        data["language"] = language

    response = httpx.post(
        url,
        headers=headers,
        data=data,
        files={"file": (filename, audio_bytes, content_type)},
        timeout=120,
    )
    if response.status_code != 200:
        try:
            payload = response.json()
        except Exception:
            payload = {}
        detail = payload.get("error") if isinstance(payload, dict) else None
        raise RuntimeError(detail or response.text or f"STT request failed with {response.status_code}")

    payload = response.json()
    transcript = payload.get("text")
    return {
        "provider": provider,
        "model": model or None,
        "transcript": transcript.strip() if isinstance(transcript, str) else "",
        "language": payload.get("language"),
        "duration_seconds": payload.get("duration"),
        "segments": payload.get("segments") if isinstance(payload.get("segments"), list) else [],
    }


def transcribe_bridge_voice_message(
    message_id: str,
    *,
    chat_jid: Optional[str] = None,
    participant_jid: Optional[str] = None,
    language: Optional[str] = None,
) -> dict:
    metadata = get_bridge_message_metadata(
        message_id,
        chat_jid=chat_jid,
        participant_jid=participant_jid,
    )
    audio_bytes, content_type = download_bridge_message_media(
        message_id,
        chat_jid=chat_jid,
        participant_jid=participant_jid,
    )

    filename = "voice-note"
    extension = mimetypes.guess_extension(content_type) or ".ogg"
    if not extension.startswith("."):
        extension = f".{extension}"

    transcription = transcribe_audio_bytes(
        audio_bytes,
        content_type=content_type,
        filename=f"{filename}{extension}",
        language=language,
    )
    return {
        **metadata,
        **transcription,
    }
