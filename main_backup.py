
import os

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from twilio.rest import Client
from twilio.base.exceptions import TwilioRestException
from fastapi.responses import Response
import base64
from sarvamai import SarvamAI
from fastapi import WebSocket, WebSocketDisconnect


load_dotenv()

app = FastAPI(title="AI Voice Agent API")


class CallRequest(BaseModel):
    to_number: str


@app.get("/")
def health():
    return {
        "status": "ok",
        "service": "AI Voice Agent",
    }

@app.get("/audio.wav")
def generate_telugu_audio():
    api_key = os.getenv("SARVAM_API_KEY")
    if not api_key:
        raise HTTPException(
            status_code=500,
            detail="SARVAM_API_KEY is missing",
        )

    client = SarvamAI(api_subscription_key=api_key)

    result = client.text_to_speech.convert(
        text="నమస్కారం! మీకు స్వాగతం.",
        language_code="te-IN",
        model="bulbul:v3",
    )

    audio_bytes = base64.b64decode(result.audios[0])

    return Response(
        content=audio_bytes,
        media_type="audio/wav",
        headers={"Cache-Control": "no-store"},
    )



@app.post("/voice")
def voice():
    base_url = os.getenv("PUBLIC_BASE_URL", "").rstrip("/")

    twiml = f"""
    <Response>
        <Play>{base_url}/audio.wav</Play>
        <Pause length="5"/>
        <Play>{base_url}/audio.wav</Play>
    </Response>
    """

    return Response(
        content=twiml,
        media_type="application/xml",
    )


@app.post("/call")
def make_call(request: CallRequest):
    account_sid = os.getenv("TWILIO_ACCOUNT_SID")
    auth_token = os.getenv("TWILIO_AUTH_TOKEN")
    from_number = os.getenv("TWILIO_PHONE_NUMBER")

    if not all([account_sid, auth_token, from_number]):
        raise HTTPException(
            status_code=500,
            detail="Twilio credentials are missing from .env",
        )

    try:
        client = Client(account_sid, auth_token)

        call = client.calls.create(
            to=request.to_number,
            from_=from_number,
            url=f"{os.getenv('PUBLIC_BASE_URL')}/voice",
        )

        return {
            "message": "Call request submitted",
            "call_sid": call.sid,
            "status": call.status,
        }

    except TwilioRestException as exc:
        raise HTTPException(
            status_code=502,
            detail=f"Twilio error {exc.code}: {exc.msg}",
        ) from exc


@app.websocket("/media")
async def media_stream(websocket: WebSocket):
    await websocket.accept()
    print("Twilio WebSocket connected")

    try:
        while True:
            message = await websocket.receive_json()
            event = message.get("event")

            if event == "connected":
                print("Twilio stream connected")

            elif event == "start":
                print("Call audio stream started")

            elif event == "media":
                payload = message.get("media", {}).get("payload", "")
                if payload:
                    print("Received audio chunk")

            elif event == "stop":
                print("Call audio stream stopped")
                break

    except WebSocketDisconnect:
        print("Twilio WebSocket disconnected")
