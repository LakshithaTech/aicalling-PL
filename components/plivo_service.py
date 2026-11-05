# components/plivo_service.py

import os
import json
import asyncio
from pathlib import Path
from typing import Optional, Dict, Any
from dotenv import load_dotenv
import plivo
from fastapi import WebSocket
import database

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

ACTIVE_CALL_METADATA: Dict[str, Any] = {}

def get_base_url() -> str:
    return os.getenv("PUBLIC_BASE_URL", "").rstrip("/")

def hangup_plivo_call(call_uuid: str):
    """Hang up Plivo call via REST API to ensure telecom line cuts immediately."""
    auth_id = os.getenv("PLIVO_AUTH_ID")
    auth_token = os.getenv("PLIVO_AUTH_TOKEN")
    if not all([auth_id, auth_token, call_uuid]) or call_uuid.startswith("call_"):
        return
    try:
        client = plivo.RestClient(auth_id, auth_token)
        res = client.calls.hangup(call_uuid=call_uuid)
        print(f"Plivo telecom call hung up successfully for {call_uuid}: {res}")
    except Exception as exc:
        print(f"Plivo hangup notice: {exc}")

def start_plivo_recording(call_uuid: str):
    """Trigger Plivo native telecom recording for the call."""
    auth_id = os.getenv("PLIVO_AUTH_ID")
    auth_token = os.getenv("PLIVO_AUTH_TOKEN")
    base_url = get_base_url()
    if not all([auth_id, auth_token, base_url]):
        return
    try:
        client = plivo.RestClient(auth_id, auth_token)
        callback = f"{base_url}/plivo-record-callback"
        res = client.calls.record(
            call_uuid=call_uuid,
            file_format="mp3",
            time_limit=600,  # 10 minutes maximum duration
            callback_url=callback,
            callback_method="POST",
        )
        print(f"Plivo native call recording triggered for {call_uuid}: {res}")
    except Exception as exc:
        print(f"Plivo native recording trigger notice: {exc}")

async def send_audio_to_plivo(websocket: WebSocket, audio_base64: str):
    """Send base64 mu-law audio chunk to Plivo WebSocket."""
    await websocket.send_json({
        "event": "playAudio",
        "media": {
            "contentType": "audio/x-mulaw",
            "sampleRate": 8000,
            "payload": audio_base64,
        },
    })

async def clear_plivo_audio_queue(websocket: WebSocket):
    """Send clearAudio event to Plivo to immediately flush any buffered audio on hangup or reset."""
    try:
        await websocket.send_json({
            "event": "clearAudio"
        })
    except Exception as exc:
        pass

async def download_and_save_recording(call_uuid: str, record_url: str, recordings_dir: Optional[Path] = None):
    """Download Plivo MP3 call recording into allcallrecordings/ and save BYTEA to PostgreSQL."""
    auth_id = os.getenv("PLIVO_AUTH_ID")
    auth_token = os.getenv("PLIVO_AUTH_TOKEN")
    if recordings_dir is None:
        recordings_dir = Path(__file__).resolve().parent.parent / "allcallrecordings"
    recordings_dir.mkdir(exist_ok=True)

    def fetch():
        import requests
        resp = requests.get(record_url, auth=(auth_id, auth_token), timeout=45)
        if resp.status_code == 200 and resp.content:
            return resp.content
        return None

    try:
        audio_bytes = await asyncio.to_thread(fetch)
        if audio_bytes:
            mp3_path = recordings_dir / f"call_{call_uuid}.mp3"
            mp3_path.write_bytes(audio_bytes)
            print(f"Complete call recording saved to {mp3_path} ({len(audio_bytes)} bytes)")
            await asyncio.to_thread(database.save_recording_data, call_uuid, audio_bytes)
    except Exception as exc:
        print("Failed to download or save call recording:", repr(exc))
