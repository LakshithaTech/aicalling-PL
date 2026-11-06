import os
import sys
import time
import base64
import uuid
import json
from pathlib import Path
from typing import Optional, Dict, Any, List
from datetime import datetime, timezone
from urllib.parse import parse_qs

if sys.stdout and hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
if sys.stderr and hasattr(sys.stderr, "reconfigure"):
    try:
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect, UploadFile, File
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel
from sarvamai import SarvamAI, AsyncSarvamAI, RealtimeAudioInput
import plivo
import asyncio

# Modular component imports
from components.prompts import (
    SYSTEM_PROMPT,
    LANGUAGE_PROMPT,
    TELUGU_LANGUAGE_INSTRUCTION,
    ENGLISH_LANGUAGE_INSTRUCTION,
)
from components.sarvam_tts import (
    SARVAM_MODEL,
    SARVAM_PACE,
    LANGUAGES,
    get_sarvam_client,
    synthesize_sarvam_speech,
    make_sarvam_audio,
    play_chunked_speech,
)
from components.openai_service import (
    OPENAI_MODEL,
    get_async_openai_client,
    get_openai_client,
    generate_chat_reply,
    generate_sync_chat_reply,
)
from components.plivo_service import (
    ACTIVE_CALL_METADATA,
    get_base_url,
    hangup_plivo_call,
    start_plivo_recording,
    send_audio_to_plivo,
    download_and_save_recording,
)
from components.questionnaire import (
    is_unemployed_response,
    detect_end_request,
    extract_single_question,
    is_closing_reply,
    detect_yes,
    detect_no,
    is_continuation_response,
    is_confusion_or_identity_query,
    is_interest_rate_or_details_query,
    is_repeat_or_clarification_query,
    is_company_response,
    normalize_hindi_stt,
    is_callback_time_query,
    is_human_agent_or_callback_request,
    is_recording_announcement,
    detect_explicit_language_switch,
    detect_initial_call_language,
    is_telugu_speech,
    get_recording_followup,
    get_opening_text,
    speak_opening_message,
)
from components.db import (
    get_db_pool,
    save_call_record,
    save_recording_data,
    fetch_all_calls,
    fetch_call_recording,
)

load_dotenv(Path(__file__).resolve().parent / ".env", override=True)

app = FastAPI(title="AI Voice Agent API")
print("RUNNING MAIN FILE:", Path(__file__).resolve())

AUDIO_DIR = Path("generated_audio")
AUDIO_DIR.mkdir(exist_ok=True)
ALL_CALL_RECORDINGS_DIR = Path("allcallrecordings")
ALL_CALL_RECORDINGS_DIR.mkdir(exist_ok=True)

# Demo-only conversation memory
CALL_STATE: Dict[str, Any] = {}

class CallRequest(BaseModel):
    to_number: str


@app.get("/")
def health():
    return {
        "status": "ok",
        "service": "AI Voice Agent",
    }


@app.get("/audio.wav")
def generate_greeting_audio():
    try:
        filename = make_sarvam_audio(
            "Hello! Thank you for calling. How can I help you today?",
            "greeting.wav",
            audio_dir=AUDIO_DIR,
        )
    except Exception as exc:
        raise HTTPException(
            status_code=502,
            detail=f"Could not generate greeting audio: {exc}",
        ) from exc

    return FileResponse(
        AUDIO_DIR / filename,
        media_type="audio/wav",
        headers={"Cache-Control": "no-store"},
    )


@app.get("/reply/{filename}")
def get_reply_audio(filename: str):
    if Path(filename).name != filename or not filename.endswith(".wav"):
        raise HTTPException(status_code=400, detail="Invalid filename")

    audio_path = AUDIO_DIR / filename
    if not audio_path.is_file():
        raise HTTPException(status_code=404, detail="Audio not found")

    return FileResponse(
        audio_path,
        media_type="audio/wav",
        headers={"Cache-Control": "no-store"},
    )


@app.post("/process-speech")
async def process_speech(request: Request):
    base_url = get_base_url()
    if not base_url:
        raise HTTPException(status_code=500, detail="PUBLIC_BASE_URL is missing")

    body = (await request.body()).decode("utf-8")
    form_data = parse_qs(body)

    customer_text = form_data.get("SpeechResult", [""])[0].strip()
    call_sid = form_data.get("CallSid", ["unknown"])[0]

    state = CALL_STATE.setdefault(
        call_sid,
        {
            "messages": [],
            "confirm_end": False,
        },
    )

    should_hang_up = False

    if state["confirm_end"]:
        if customer_text.lower().strip() in {
            "yes", "yes please", "yes end the call", "yes, end the call",
            "please end the call", "end the call", "goodbye", "bye",
        }:
            reply_text = "Thank you for calling. Have a nice day."
            should_hang_up = True
            state["confirm_end"] = False
        elif detect_no(customer_text):
            reply_text = "Of course. I'm happy to help. What else can I assist you with?"
            state["confirm_end"] = False
        else:
            reply_text = (
                "Sorry, I didn't understand. "
                "Would you like me to end the call now? Please say yes or no."
            )
    elif not customer_text:
        reply_text = "I'm sorry, I didn't catch that. Could you please repeat it?"
    elif detect_end_request(customer_text):
        state["confirm_end"] = True
        reply_text = "Would you like me to end the call now? Please say yes or no."
    else:
        try:
            state["messages"].append({
                "role": "user",
                "content": customer_text,
            })
            state["messages"] = state["messages"][-12:]

            messages_payload = [
                {"role": "system", "content": SYSTEM_PROMPT},
                *state["messages"],
            ]
            reply_text = generate_sync_chat_reply(messages_payload, max_tokens=150)
            if not reply_text:
                reply_text = "I'm sorry. Could you please repeat that?"

            state["messages"].append({
                "role": "assistant",
                "content": reply_text,
            })
            state["messages"] = state["messages"][-12:]
        except Exception:
            print("OpenAI response generation failed")
            reply_text = (
                "I'm sorry, I'm having trouble answering right now. "
                "Could you please try again?"
            )

    try:
        filename = f"{uuid.uuid4().hex}.wav"
        make_sarvam_audio(reply_text, filename, audio_dir=AUDIO_DIR)
    except Exception:
        print("Sarvam audio generation failed")
        fallback_xml = """
        <Response>
            <Say language="en-US" voice="woman">
                I'm sorry, the voice service is temporarily unavailable.
            </Say>
            <Hangup/>
        </Response>
        """
        return Response(content=fallback_xml.strip(), media_type="application/xml")

    reply_audio_url = f"{base_url}/reply/{filename}"

    if should_hang_up:
        xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Play>{reply_audio_url}</Play>
    <Hangup/>
</Response>"""
        return Response(content=xml, media_type="application/xml")

    action_url = f"{base_url}/process-speech"

    xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <GetInput inputType="speech"
              action="{action_url}"
              method="POST"
              executionTimeout="30">
        <Play>{reply_audio_url}</Play>
    </GetInput>
    <Redirect method="POST">{action_url}</Redirect>
</Response>"""
    return Response(content=xml, media_type="application/xml")


@app.post("/call")
def make_call(request: CallRequest):
    auth_id = os.getenv("PLIVO_AUTH_ID")
    auth_token = os.getenv("PLIVO_AUTH_TOKEN")
    from_number = os.getenv("PLIVO_PHONE_NUMBER")
    base_url = get_base_url()

    if not all([auth_id, auth_token, from_number, base_url]):
        raise HTTPException(
            status_code=500,
            detail="Plivo credentials, phone number, or PUBLIC_BASE_URL are missing",
        )

    try:
        client = plivo.RestClient(auth_id, auth_token)
        response = client.calls.create(
            from_=from_number,
            to_=request.to_number,
            answer_url=f"{base_url}/plivo-stream-answer",
            answer_method="POST",
            ring_url=f"{base_url}/plivo-ring",
            ring_method="POST",
            callback_url=f"{base_url}/plivo-call-status",
            callback_method="POST",
            hangup_url=f"{base_url}/plivo-call-status",
            hangup_method="POST",
            ring_timeout=45,
        )
        req_uuid = getattr(response, "request_uuid", None)
        if not req_uuid and isinstance(response, dict):
            req_uuid = response.get("request_uuid")
        return {
            "message": "Plivo call request submitted",
            "call_uuid": req_uuid,
            "details": str(response),
        }
    except Exception as exc:
        import traceback
        traceback.print_exc()
        raise HTTPException(
            status_code=502,
            detail=f"Plivo error: {type(exc).__name__}: {str(exc)}",
        ) from exc


@app.post("/plivo-record-callback")
async def plivo_record_callback(request: Request):
    """Webhook called by Plivo when the telecom recording is ready."""
    try:
        form = dict(await request.form())
    except Exception:
        form = {}

    record_url = (
        form.get("RecordUrl")
        or form.get("record_url")
        or form.get("recording_url")
        or request.query_params.get("RecordUrl")
        or request.query_params.get("record_url")
    )
    call_uuid = (
        form.get("CallUUID")
        or form.get("call_uuid")
        or request.query_params.get("CallUUID")
        or request.query_params.get("call_uuid")
    )

    resp_str = form.get("response") or request.query_params.get("response")
    if resp_str:
        try:
            resp_data = json.loads(resp_str)
            if isinstance(resp_data, dict):
                if not record_url:
                    record_url = resp_data.get("record_url") or resp_data.get("RecordUrl")
                if not call_uuid:
                    call_uuid = resp_data.get("call_uuid") or resp_data.get("CallUUID")
        except Exception:
            pass

    print(f"Plivo recording callback received for CallUUID: {call_uuid}, URL: {record_url}")

    if record_url and call_uuid:
        asyncio.create_task(download_and_save_recording(call_uuid, record_url, ALL_CALL_RECORDINGS_DIR))

    return Response(content="<Response/>", media_type="application/xml")


@app.api_route("/plivo-ring", methods=["GET", "POST"])
async def plivo_ring(request: Request):
    """Webhook invoked by Plivo when the remote customer phone begins ringing."""
    return Response(content="<Response/>", media_type="application/xml")


@app.api_route("/plivo-call-status", methods=["GET", "POST"])
async def plivo_call_status(request: Request):
    """
    Webhook called by Plivo for call status updates (busy, no-answer, failed, hangup).
    Ensures non-connected calls are tracked in PostgreSQL.
    """
    try:
        form = dict(await request.form())
    except Exception:
        form = {}

    call_uuid = form.get("CallUUID") or request.query_params.get("CallUUID") or ""
    call_status_raw = (form.get("CallStatus") or request.query_params.get("CallStatus") or "").lower()
    hangup_cause = (form.get("HangupCause") or request.query_params.get("HangupCause") or "").upper()
    to_number = form.get("To") or request.query_params.get("To") or ""
    duration_sec = int(form.get("Duration") or 0)

    print(f"Plivo call status callback: CallUUID={call_uuid}, Status={call_status_raw}, Cause={hangup_cause}, To={to_number}, Duration={duration_sec}s")

    if not call_uuid:
        return Response(content="<Response/>", media_type="application/xml")

    # If call was not answered or failed (busy, no-answer, rejected, failed, cancel)
    if call_status_raw in ("busy", "no-answer", "failed", "cancel") or hangup_cause in ("USER_BUSY", "NO_ANSWER", "CALL_REJECTED"):
        if call_status_raw == "busy" or "BUSY" in hangup_cause:
            db_call_status = "failed"
            loan_purpose = f"Customer Busy ({hangup_cause or 'USER_BUSY'})"
            lead_status = "follow_up"
            system_msg = f"Call Status: BUSY - Customer was busy / rejected call ({hangup_cause})"
        elif call_status_raw == "no-answer" or "NO_ANSWER" in hangup_cause:
            db_call_status = "no_answer"
            loan_purpose = f"No Answer ({hangup_cause or 'NO_ANSWER'})"
            lead_status = "follow_up"
            system_msg = f"Call Status: NO-ANSWER - Customer did not pick up ({hangup_cause})"
        else:
            db_call_status = "failed"
            loan_purpose = f"Call Failed ({hangup_cause or call_status_raw})"
            lead_status = "follow_up"
            system_msg = f"Call Status: FAILED ({hangup_cause or call_status_raw})"

        history = [{"role": "system", "content": system_msg}]

        try:
            await asyncio.to_thread(
                save_call_record,
                call_sid=call_uuid,
                phone_number=to_number or "+919999999999",
                conversation_history=history,
                call_status=db_call_status,
                lead_status=lead_status,
                loan_purpose=loan_purpose,
                customer_name=None,
            )
            print(f"Recorded non-connected call status '{db_call_status}' ({loan_purpose}) to DB for {call_uuid}")
        except Exception as exc:
            print("Error saving call status callback to DB:", repr(exc))

    return Response(content="<Response/>", media_type="application/xml")


def normalize_phone_digits(num: str) -> str:
    d = "".join([c for c in str(num or "") if c.isdigit()])
    if len(d) == 10:
        return "91" + d
    if d.startswith("0"):
        return "91" + d[1:]
    return d


class BatchSyncRequest(BaseModel):
    phone_numbers: List[str] = []
    call_uuids: Optional[List[str]] = []


@app.post("/plivo-sync-batch")
async def plivo_sync_batch(payload: BatchSyncRequest):
    """
    Dynamically checks telecom call status for a batch of phone numbers directly against
    the Plivo API and PostgreSQL, returning exact outcomes (answered, busy, no_answer, ringing)
    in real time without waiting for default timeouts.
    """
    auth_id = os.getenv("PLIVO_AUTH_ID")
    auth_token = os.getenv("PLIVO_AUTH_TOKEN")
    if not auth_id or not auth_token:
        raise HTTPException(status_code=500, detail="Plivo credentials missing")

    client = plivo.RestClient(auth_id, auth_token)

    # 1. Fetch live active call UUIDs from Plivo
    live_call_uuids = set()
    try:
        live_res = await asyncio.to_thread(client.calls.live_call_list_ids)
        if isinstance(live_res, dict):
            live_call_uuids = set(live_res.get("calls", []))
    except Exception as exc:
        print("Error fetching live call list from Plivo:", exc)

    # 2. Query recent calls from PostgreSQL
    db_calls_by_phone = {}
    db_calls_by_sid = {}
    p = get_db_pool()
    if p:
        conn = None
        try:
            conn = p.getconn()
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT id, phone_number, call_sid, call_status, call_duration_seconds, loan_purpose, conversation_json, created_at
                    FROM public.ai_agent_calls
                    ORDER BY id DESC
                    LIMIT 200;
                """)
                for row in cur.fetchall():
                    cid, phone, sid, status, dur, purpose, conv, created = row
                    norm_p = normalize_phone_digits(phone)
                    rec = {
                        "id": cid,
                        "phone": phone,
                        "call_sid": sid,
                        "status": status,
                        "duration": dur or 0,
                        "loan_purpose": purpose or "",
                        "conversation": conv or [],
                    }
                    if norm_p and norm_p not in db_calls_by_phone:
                        db_calls_by_phone[norm_p] = rec
                    if sid and sid not in db_calls_by_sid:
                        db_calls_by_sid[sid] = rec
        except Exception as exc:
            print("Error querying recent DB calls:", exc)
        finally:
            if conn and p:
                try:
                    p.putconn(conn)
                except Exception:
                    pass

    # 3. Query recent calls from Plivo (limit=20)
    plivo_calls_map = {}
    try:
        recent_plivo = await asyncio.to_thread(client.calls.list, limit=20)
        c_list = getattr(recent_plivo, "objects", recent_plivo) if not isinstance(recent_plivo, list) else recent_plivo
        for c in c_list:
            to_n = normalize_phone_digits(getattr(c, "to_number", ""))
            if to_n and to_n not in plivo_calls_map:
                plivo_calls_map[to_n] = c
    except Exception as exc:
        print("Error fetching recent Plivo calls:", exc)

    results = {}
    for raw_phone in payload.phone_numbers:
        norm_phone = normalize_phone_digits(raw_phone)
        c_plivo = plivo_calls_map.get(norm_phone)

        # If not in the recent 20, look up by number in Plivo
        if not c_plivo and norm_phone:
            try:
                single_res = await asyncio.to_thread(client.calls.list, to_number=norm_phone, limit=1)
                s_list = getattr(single_res, "objects", single_res) if not isinstance(single_res, list) else single_res
                if s_list:
                    c_plivo = s_list[0]
                    plivo_calls_map[norm_phone] = c_plivo
            except Exception:
                pass

        db_rec = db_calls_by_phone.get(norm_phone)
        uuid = getattr(c_plivo, "call_uuid", "") if c_plivo else (db_rec.get("call_sid", "") if db_rec else "")

        # Check if call is currently actively ringing on Plivo
        if uuid and uuid in live_call_uuids:
            results[raw_phone] = {
                "status": "ringing",
                "category": "ringing",
                "isLive": True,
                "callSid": uuid,
                "duration": 0,
            }
            continue

        # If we have a Plivo record:
        if c_plivo:
            state = getattr(c_plivo, "call_state", None) or ""
            cause = getattr(c_plivo, "hangup_cause_name", None) or ""
            dur = int(getattr(c_plivo, "call_duration", 0) or 0)
            source = getattr(c_plivo, "hangup_source", None) or ""

            if state == "ANSWER" or dur > 0:
                cat = "answered"
                cut_by = "user" if source == "Callee" else "ai"
            elif cause in ("Rejected", "Busy Line", "User Busy") or "busy" in cause.lower():
                cat = "busy"
                cut_by = None
                # Ensure saved to DB if not already present
                if not db_rec:
                    try:
                        await asyncio.to_thread(
                            save_call_record,
                            call_sid=uuid,
                            phone_number=raw_phone,
                            conversation_history=[{"role": "system", "content": f"Plivo Call Status: BUSY ({cause})"}],
                            call_status="failed",
                            lead_status="follow_up",
                            loan_purpose=f"Customer Busy ({cause})",
                            customer_name=None,
                        )
                    except Exception:
                        pass
            elif state in ("RINGING", "IN_PROGRESS", "QUEUED"):
                cat = "ringing"
                cut_by = None
            else:
                cat = "no_answer"
                cut_by = None
                if not db_rec:
                    try:
                        await asyncio.to_thread(
                            save_call_record,
                            call_sid=uuid,
                            phone_number=raw_phone,
                            conversation_history=[{"role": "system", "content": f"Plivo Call Status: NO-ANSWER ({cause})"}],
                            call_status="no_answer",
                            lead_status="follow_up",
                            loan_purpose=f"No Answer ({cause})",
                            customer_name=None,
                        )
                    except Exception:
                        pass

            results[raw_phone] = {
                "status": cat,
                "category": cat,
                "cutBy": cut_by,
                "duration": dur if dur > 0 else (db_rec.get("duration", 0) if db_rec else 0),
                "hangupCause": cause,
                "hangupSource": source,
                "callSid": uuid,
                "dbId": db_rec.get("id") if db_rec else None,
            }
        elif db_rec:
            # Use DB record
            db_status = (db_rec.get("status") or "").lower()
            purpose = (db_rec.get("loan_purpose") or "").lower()
            dur = db_rec.get("duration") or 0
            conv = db_rec.get("conversation") or []
            sys_texts = " ".join([m.get("content", "") for m in conv if isinstance(m, dict) and m.get("role") == "system"]).lower()

            if "busy" in db_status or "busy" in purpose or "busy" in sys_texts:
                cat = "busy"
                cut_by = None
            elif dur > 0 or db_status == "completed":
                cat = "answered"
                cut_by = "ai" if ("call completed normally" in sys_texts or "ai_completed" in sys_texts) else "user"
            else:
                cat = "no_answer"
                cut_by = None

            results[raw_phone] = {
                "status": cat,
                "category": cat,
                "cutBy": cut_by,
                "duration": dur,
                "dbId": db_rec.get("id"),
                "callSid": db_rec.get("call_sid"),
            }
        else:
            results[raw_phone] = {
                "status": "queued",
                "category": "queued",
            }

    return {"success": True, "statuses": results}


@app.api_route("/call-status", methods=["GET", "POST"])
async def get_single_call_status(request: Request):
    """Dynamically get status for a single phone call or call_uuid."""
    phone = request.query_params.get("phone") or request.query_params.get("phone_number") or ""
    call_uuid = request.query_params.get("call_uuid") or request.query_params.get("CallUUID") or ""
    if not phone and not call_uuid:
        try:
            body = await request.json()
            phone = body.get("phone") or body.get("phone_number") or ""
            call_uuid = body.get("call_uuid") or ""
        except Exception:
            pass

    sync_req = BatchSyncRequest(phone_numbers=[phone] if phone else [], call_uuids=[call_uuid] if call_uuid else [])
    res = await plivo_sync_batch(sync_req)
    st = res.get("statuses", {}).get(phone, {})
    return st


@app.api_route("/plivo-stream-answer", methods=["GET", "POST"])
async def plivo_stream_answer(request: Request):
    base_url = get_base_url()
    if not base_url.startswith("https://"):
        raise HTTPException(
            status_code=500,
            detail="PUBLIC_BASE_URL must use HTTPS for Plivo streaming",
        )

    call_uuid = ""
    to_number = ""
    from_number = ""
    try:
        form = dict(await request.form())
        call_uuid = form.get("CallUUID") or form.get("call_uuid") or ""
        to_number = form.get("To") or form.get("to_number") or ""
        from_number = form.get("From") or form.get("from_number") or ""
    except Exception:
        pass

    if not call_uuid:
        call_uuid = request.query_params.get("CallUUID") or request.query_params.get("call_uuid") or ""
    if not to_number:
        to_number = request.query_params.get("to_number") or request.query_params.get("To") or ""
    if not from_number:
        from_number = request.query_params.get("From") or request.query_params.get("from_number") or ""

    phone = to_number or from_number
    if call_uuid and phone:
        ACTIVE_CALL_METADATA[call_uuid] = {
            "phone_number": phone,
            "started_at": datetime.now(timezone.utc),
        }
    elif phone:
        ACTIVE_CALL_METADATA["latest"] = {
            "phone_number": phone,
            "started_at": datetime.now(timezone.utc),
        }

    ws_url = "wss://" + base_url.removeprefix("https://") + "/plivo-audio-stream"

    xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Stream bidirectional="true"
            keepCallAlive="true"
            streamTimeout="3600"
            contentType="audio/x-mulaw;rate=8000">{ws_url}</Stream>
    <Hangup/>
</Response>"""
    return Response(content=xml, media_type="application/xml")


@app.api_route("/plivo-answer", methods=["GET", "POST"])
async def plivo_answer(request: Request):
    import xml.sax.saxutils as saxutils
    try:
        base_url = get_base_url()
        greeting_text = (
            "గుడ్ మార్నింగ్ అండి! నేను పర్సనల్ లోన్ డిపార్ట్మెంట్ నుండి స్నేహని మాట్లాడుతున్నాను. "
            "ఐసీఐసీఐ బ్యాంక్ పర్సనల్ లోన్ గురించి కాల్ చేస్తున్నాను. మీకు పర్సనల్ లోన్ అవసరమా?"
        )
        audio_filename = "opening_greeting_te.wav"
        make_sarvam_audio(greeting_text, audio_filename, language="te", audio_dir=AUDIO_DIR)
        audio_url = saxutils.escape(f"{base_url}/reply/{audio_filename}")
        xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <GetInput inputType="speech"
              action="{base_url}/plivo-process-speech"
              method="POST"
              executionTimeout="30">
        <Play>{audio_url}</Play>
    </GetInput>
    <Redirect method="POST">{base_url}/plivo-timeout</Redirect>
</Response>"""
        return Response(content=xml, media_type="application/xml")
    except Exception as exc:
        print("plivo_answer exception:", repr(exc))
        raise HTTPException(status_code=500, detail="Error generating call answer") from exc


@app.api_route("/plivo-timeout", methods=["GET", "POST"])
async def plivo_timeout(request: Request):
    import xml.sax.saxutils as saxutils
    base_url = get_base_url()
    try:
        form_data = dict(await request.form()) if request.method == "POST" else {}
    except Exception:
        form_data = {}

    call_sid = (
        form_data.get("CallUUID")
        or form_data.get("call_uuid")
        or request.query_params.get("CallUUID")
        or request.query_params.get("call_uuid")
        or "unknown"
    )

    state = CALL_STATE.setdefault(call_sid, {})
    silence_count = state.get("silence_count", 0) + 1
    state["silence_count"] = silence_count

    if silence_count >= 2:
        farewell = "Thank you for your time. Have a nice day."
        try:
            filename = f"{uuid.uuid4().hex}.wav"
            make_sarvam_audio(farewell, filename, language="en", audio_dir=AUDIO_DIR)
            audio_url = saxutils.escape(f"{base_url}/reply/{filename}")
            xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Play>{audio_url}</Play>
    <Hangup/>
</Response>"""
        except Exception:
            xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Speak voice="WOMAN">{farewell}</Speak>
    <Hangup/>
</Response>"""
        return Response(content=xml, media_type="application/xml")

    current_q = state.get("current_question", "Are you looking for a personal loan?")
    reminder = f"I'm sorry, I didn't hear anything. {current_q}"

    try:
        filename = f"{uuid.uuid4().hex}.wav"
        make_sarvam_audio(reminder, filename, language="en", audio_dir=AUDIO_DIR)
        audio_url = saxutils.escape(f"{base_url}/reply/{filename}")
        xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <GetInput inputType="speech"
              action="{base_url}/plivo-process-speech"
              method="POST"
              executionTimeout="30">
        <Play>{audio_url}</Play>
    </GetInput>
    <Redirect method="POST">{base_url}/plivo-timeout</Redirect>
</Response>"""
    except Exception:
        safe_reminder = saxutils.escape(reminder)
        xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <GetInput inputType="speech"
              action="{base_url}/plivo-process-speech"
              method="POST"
              executionTimeout="30">
        <Speak voice="WOMAN">{safe_reminder}</Speak>
    </GetInput>
    <Redirect method="POST">{base_url}/plivo-timeout</Redirect>
</Response>"""
    return Response(content=xml, media_type="application/xml")


@app.api_route("/plivo-process-speech", methods=["GET", "POST"])
async def plivo_process_speech(request: Request):
    import xml.sax.saxutils as saxutils
    base_url = get_base_url()
    if not base_url:
        raise HTTPException(status_code=500, detail="PUBLIC_BASE_URL is missing")

    form_data = dict(await request.form())
    customer_text = (
        form_data.get("SpeechResult")
        or form_data.get("Speech")
        or form_data.get("speech_result")
        or request.query_params.get("SpeechResult")
        or ""
    ).strip()

    call_sid = (
        form_data.get("CallUUID")
        or form_data.get("call_uuid")
        or request.query_params.get("CallUUID")
        or "unknown"
    )

    state = CALL_STATE.setdefault(call_sid, {"messages": []})
    state["silence_count"] = 0

    if not customer_text:
        return await plivo_timeout(request)

    state["messages"].append({"role": "user", "content": customer_text})
    state["messages"] = state["messages"][-14:]

    messages_payload = [
        {"role": "system", "content": SYSTEM_PROMPT + "\n\n" + TELUGU_LANGUAGE_INSTRUCTION},
        *state["messages"],
    ]

    try:
        reply_text = generate_sync_chat_reply(messages_payload, max_tokens=100)
    except Exception:
        reply_text = "సరే అండి, మీ ఆన్సర్ ఇంకోసారి చెప్పగలరా?"

    reply_text = extract_single_question(reply_text)
    state["messages"].append({"role": "assistant", "content": reply_text})

    filename = f"{uuid.uuid4().hex}.wav"
    make_sarvam_audio(reply_text, filename, language="te", audio_dir=AUDIO_DIR)
    reply_audio_url = saxutils.escape(f"{base_url}/reply/{filename}")

    if is_closing_reply(reply_text):
        xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Play>{reply_audio_url}</Play>
    <Hangup/>
</Response>"""
        return Response(content=xml, media_type="application/xml")

    xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <GetInput inputType="speech"
              action="{base_url}/plivo-process-speech"
              method="POST"
              executionTimeout="30">
        <Play>{reply_audio_url}</Play>
    </GetInput>
    <Redirect method="POST">{base_url}/plivo-timeout</Redirect>
</Response>"""
    return Response(content=xml, media_type="application/xml")


@app.post("/test-language-detection")
async def test_language_detection(file: UploadFile = File(...)):
    load_dotenv(override=True)
    api_key = os.getenv("SARVAM_API_KEY")
    if not api_key:
        raise HTTPException(status_code=500, detail="SARVAM_API_KEY is missing")
    audio_bytes = await file.read()
    if not audio_bytes:
        raise HTTPException(status_code=400, detail="Audio file is empty")

    import io
    client = SarvamAI(api_subscription_key=api_key)
    try:
        result = client.speech_to_text.transcribe(
            file=("speech.wav", io.BytesIO(audio_bytes)),
            model="saaras:v4",
            language_code="auto",
        )
        return {"language": getattr(result, "language_code", None), "text": getattr(result, "transcript", "")}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.websocket("/plivo-audio-stream")
async def plivo_audio_stream(websocket: WebSocket):
    await websocket.accept()

    load_dotenv(override=True)
    api_key = os.getenv("SARVAM_API_KEY")
    if not api_key:
        print("Streaming error: SARVAM_API_KEY is missing")
        await websocket.close(code=1011)
        return

    client = AsyncSarvamAI(api_subscription_key=api_key)
    sarvam_socket = None
    listener_task = None
    audio_packet_count = 0
    conversation_history = []

    call_sid = None
    phone_number = None
    call_started_at = datetime.now(timezone.utc)
    stream_ended_normally = False
    saved_to_db = False
    call_done_event = asyncio.Event()
    call_audio_path = None
    ai_speaking_until = 0.0
    is_processing_turn = False
    call_language: Optional[str] = "en"  # Starts in English by default
    silence_prompt_stage = 0  # 0: waiting for first response, 1: reminder played, 2: terminating
    last_interaction_time = time.time()
    opening_already_spoken = False
    silence_task = None

    def is_ai_active():
        return is_processing_turn or (time.time() < ai_speaking_until)

    try:
        async with client.speech_to_text_realtime_streaming.connect(
            language_code="auto",
            model="saaras:v4",
            stream_type="fast",
            mode="transcribe",
            endpointing="vad",
            encoding="mulaw",
            sample_rate="8000",
            silence_duration_ms="400",
        ) as sarvam_socket:

            async def receive_sarvam_results():
                nonlocal stream_ended_normally, saved_to_db, ai_speaking_until, is_processing_turn, call_language
                try:
                    while True:
                        event = await sarvam_socket.recv()
                        event_type = getattr(event, "event", "")

                        if event_type == "transcript.partial":
                            if not is_ai_active():
                                print("Partial:", event.text)
                                if (event.text or "").strip():
                                    last_interaction_time = time.time()

                        elif event_type == "transcript.final":
                            # Only ignore transcript if AI is still processing turn or in the middle of speech (allow 0.2s tail buffer)
                            if is_processing_turn or (time.time() < (ai_speaking_until - 0.2)):
                                print(f"[IGNORING MID-SPEECH] Customer audio arrived during AI speech ('{event.text}')")
                                continue

                            customer_text = (event.text or "").strip()
                            # Normalize common Hindi STT outputs (like कंपनी -> company) since agent is English/Telugu only
                            customer_text = normalize_hindi_stt(customer_text)
                            detected_language = getattr(event, "language", None)
                            if not customer_text or customer_text.strip(" .,!?:;") == "":
                                continue

                            # Customer spoke: reset silence watchdog timer and stage
                            silence_prompt_stage = 0
                            last_interaction_time = time.time()
                            is_processing_turn = True

                            print(
                                "FINAL TRANSCRIPT:",
                                customer_text,
                                "| Language:",
                                detected_language,
                                "| Locked call language:",
                                call_language,
                            )

                            if not customer_text:
                                is_processing_turn = False
                                continue

                            # --------------------------------------------------
                            # 1) Ignore automatic phone/network recording prompts.
                            #    They are NOT a customer response and MUST NOT
                            #    lock the call language.
                            # --------------------------------------------------
                            if is_recording_announcement(customer_text):
                                tts_language = call_language or "te"
                                reply_text = get_recording_followup(tts_language)
                                print("[SYSTEM AUDIO] Recording announcement ignored.")
                                print("AI REPLY:", reply_text)

                                try:
                                    audio_bytes = await asyncio.to_thread(
                                        synthesize_sarvam_speech,
                                        reply_text,
                                        tts_language,
                                    )
                                    if audio_bytes:
                                        if call_audio_path:
                                            with open(call_audio_path, "ab") as f:
                                                f.write(audio_bytes)
                                        audio_base64 = base64.b64encode(audio_bytes).decode("ascii")
                                        await send_audio_to_plivo(websocket, audio_base64)
                                        ai_speaking_until = time.time() + (len(audio_bytes) / 8000.0)
                                finally:
                                    is_processing_turn = False
                                continue

                            # --------------------------------------------------
                            # 2) Language Management & Switching
                            #    Rules:
                            #    - Starting language is English (en)
                            #    - eng to tel (e to t): Auto-switches to Telugu whenever customer speaks Telugu
                            #    - tel to eng: CANNOT switch to English automatically. ONLY if customer explicitly asks to speak in English.
                            # --------------------------------------------------
                            requested_language = detect_explicit_language_switch(customer_text)

                            if call_language == "en" or call_language is None:
                                # Agent is currently speaking English
                                # Auto-switch to Telugu if customer speaks Telugu OR explicitly asks for Telugu:
                                if requested_language == "te" or is_telugu_speech(customer_text, detected_language):
                                    call_language = "te"
                                    print(f"[LANGUAGE] Customer spoke Telugu. Auto-switched from English to Telugu (e to t). Input: '{customer_text}'")
                                elif requested_language == "en":
                                    call_language = "en"
                                else:
                                    call_language = "en"
                                    # Otherwise stay in English!

                            elif call_language == "te":
                                # Agent is currently speaking Telugu
                                # Rule: Once in Telugu, CANNOT switch to English automatically!
                                # ONLY switch if customer explicitly requests English:
                                if requested_language == "en":
                                    call_language = "en"
                                    print(f"[LANGUAGE] Customer explicitly requested English. Switched to: en. Input: '{customer_text}'")
                                # Otherwise STAY in Telugu! (Loan terms like HDFC, salary, EMI do NOT switch)

                            tts_language = call_language

                            try:
                                if not conversation_history:
                                    conversation_history.append({
                                        "role": "assistant",
                                        "content": get_opening_text("en"),
                                    })

                                conversation_history.append({
                                    "role": "user",
                                    "content": customer_text,
                                })

                                language_instruction = (
                                    ENGLISH_LANGUAGE_INSTRUCTION
                                    if call_language == "en"
                                    else TELUGU_LANGUAGE_INSTRUCTION
                                )

                                runtime_context = (
                                    "\n\nRUNTIME_CONTEXT:\n"
                                    f"CALL_LANGUAGE = {'ENGLISH' if call_language == 'en' else 'TELUGU'}\n"
                                    f"STRICT_LANGUAGE_MODE = {'REPLY ONLY IN ENGLISH' if call_language == 'en' else 'REPLY ONLY IN TELUGU'}\n"
                                    "LANGUAGE_LOCKED = true\n"
                                )

                                messages_payload = [
                                    {
                                        "role": "system",
                                        "content": (
                                            SYSTEM_PROMPT
                                            + "\n\n"
                                            + language_instruction
                                            + runtime_context
                                        ),
                                    },
                                    *conversation_history[-14:],
                                ]

                                # Determine previous assistant context:
                                last_asst_msg = ""
                                for m in reversed(conversation_history[:-1]):
                                    if m.get("role") == "assistant":
                                        last_asst_msg = m.get("content", "").lower()
                                        break

                                is_eligibility_context = any(kw in last_asst_msg for kw in (
                                    "eligibility", "check your eligibility", "check", "ఎలిజిబిలిటీ",
                                    "need a personal loan", "need a loan", "పర్సనల్ లోన్ అవసరమా",
                                    "yes చెప్పండి", "yes or no", "do you need"
                                ))

                                is_continuation = is_continuation_response(customer_text) or detect_yes(customer_text)

                                # Check if job/business has already been discussed in history:
                                job_already_asked = any(
                                    ("job" in m.get("content", "").lower() or "జాబ్" in m.get("content", "").lower() or "business" in m.get("content", "").lower() or "బిజినెస్" in m.get("content", "").lower())
                                    for m in conversation_history
                                )

                                # Check if loan offer (1 lakh to 1 crore at 9.99% to 18%) was already pitched:
                                offer_already_pitched = any(
                                    ("1 lakh" in m.get("content", "").lower() or "1 లక్ష" in m.get("content", "").lower() or "9.99%" in m.get("content", "").lower() or "nine point" in m.get("content", "").lower())
                                    for m in conversation_history
                                    if m.get("role") == "assistant"
                                )

                                is_callback_closing = False
                                # Check if previous turn was callback time question:
                                if is_callback_time_query(last_asst_msg):
                                    print(f"[CALLBACK TIME ANSWER] Customer replied to callback time question: '{customer_text}'")
                                    is_callback_closing = True
                                    if detect_no(customer_text) or any(w in customer_text.lower() for w in ("no callback", "call cheyoddu", "oddu", "vaddu", "వద్దు")):
                                        if call_language == "en":
                                            reply_text = "Okay, thank you for your time. Have a great day!"
                                        else:
                                            reply_text = "సరే అండి, మీ సమయానికి ధన్యవాదాలు. హావ్ ఏ గ్రేట్ డే!"
                                    else:
                                        if call_language == "en":
                                            reply_text = "Thank you! Our executive will call you at that time. Have a nice day!"
                                        else:
                                            reply_text = "థాంక్యూ అండి! ఆ సమయానికి మా ఎగ్జిక్యూటివ్ మీకు కాల్ చేస్తారు. హావ్ ఏ నైస్ డే!"

                                # 1. Confusion / Identity / Purpose Query ("What are you talking about?", "Who is this?", "Why did you call?"):
                                elif is_confusion_or_identity_query(customer_text):
                                    print(f"[CONFUSION / IDENTITY] Customer asked: '{customer_text}'. Clarifying loan department offer.")
                                    if call_language == "en":
                                        reply_text = "I am Sneha calling from the personal loan department. We provide personal loans from 1 lakh to 1 crore at interest rates from 9.99% to 18%. Do you need a personal loan?"
                                    else:
                                        reply_text = "నేను పర్సనల్ లోన్ డిపార్ట్‌మెంట్ నుండి స్నేహ మాట్లాడుతున్నాను అండి. మేము 9.99% నుండి 18% వడ్డీ రేటుతో 1 లక్ష నుండి 1 కోటి వరకు పర్సనల్ లోన్స్ అందిస్తున్నాము. మీకు పర్సనల్ లోన్ అవసరమా అండి?"

                                # 2. Explicit Call End Request / DNC:
                                elif detect_end_request(customer_text):
                                    print(f"[END REQUEST] Customer requested end call ('{customer_text}'). Ending call.")
                                    if call_language == "en":
                                        reply_text = "Okay, thank you for your time. Have a nice day!"
                                    else:
                                        reply_text = "సరే అండి, మీ సమయానికి ధన్యవాదాలు. హావ్ ఏ నైస్ డే!"

                                # 3. Customer Asks About Interest Rate / Loan Details ("interate", "interest rate", "vaddi entha", "loan details"):
                                elif is_interest_rate_or_details_query(customer_text) and not job_already_asked:
                                    print(f"[INTEREST RATE / DETAILS] Customer asked interest rate / details ('{customer_text}'). Pitching terms.")
                                    if call_language == "en":
                                        reply_text = "We provide personal loans from 1 lakh to 1 crore at interest rates from 9.99% to 18%. Do you need a personal loan?"
                                    else:
                                        reply_text = "మేము 9.99% నుండి 18% వడ్డీ రేటుతో 1 లక్ష నుండి 1 కోటి వరకు పర్సనల్ లోన్స్ అందిస్తున్నాము. మీకు పర్సనల్ లోన్ అవసరమా అండి?"

                                # 4. Human Agent / Callback Request / Busy / Call Later:
                                elif is_human_agent_or_callback_request(customer_text) and not is_confusion_or_identity_query(customer_text) and not is_interest_rate_or_details_query(customer_text):
                                    print(f"[HUMAN AGENT / CALLBACK REQUEST] Customer requested callback or human agent ('{customer_text}'). Asking convenient day/time.")
                                    if call_language == "en":
                                        reply_text = "On which day and at what time would it be convenient for our executive to call you?"
                                    else:
                                        reply_text = "మా ఎగ్జిక్యూటివ్ మీకు కాల్ చేయడానికి ఏ రోజు, ఏ టైమ్ మీకు కన్వీనియంట్‌గా ఉంటుంది అండి?"

                                # 5. Customer says NO / NOT INTERESTED:
                                elif detect_no(customer_text):
                                    if not offer_already_pitched and (len(conversation_history) <= 3 or is_eligibility_context):
                                        print(f"[INITIAL NO] Customer said no to opening ('{customer_text}'). Pitching loan offer before closing.")
                                        if call_language == "en":
                                            reply_text = "We provide personal loans from 1 lakh to 1 crore at interest rates from 9.99% to 18%. Do you need a personal loan?"
                                        else:
                                            reply_text = "మేము 9.99% నుండి 18% వడ్డీ రేటుతో 1 లక్ష నుండి 1 కోటి వరకు పర్సనల్ లోన్స్ అందిస్తున్నాము. మీకు పర్సనల్ లోన్ అవసరమా అండి?"
                                    else:
                                        print(f"[LOAN DECLINED] Customer declined loan ('{customer_text}'). Ending call.")
                                        if call_language == "en":
                                            reply_text = "Okay, thank you for your time. Have a nice day!"
                                        else:
                                            reply_text = "సరే అండి, మీ సమయానికి ధన్యవాదాలు. హావ్ ఏ నైస్ డే!"

                                # 6. Customer requests repeat / says didn't understand ("malli cheppandi", "repeat", "artham kale"):
                                elif is_repeat_or_clarification_query(customer_text):
                                    print(f"[REPEAT / CLARIFICATION] Customer requested repeat ('{customer_text}').")
                                    if not job_already_asked:
                                        if offer_already_pitched:
                                            if call_language == "en":
                                                reply_text = "We provide personal loans from 1 lakh to 1 crore at interest rates from 9.99% to 18%. Do you need a personal loan?"
                                            else:
                                                reply_text = "మేము 9.99% నుండి 18% వడ్డీ రేటుతో 1 లక్ష నుండి 1 కోటి వరకు పర్సనల్ లోన్స్ అందిస్తున్నాము. మీకు పర్సనల్ లోన్ అవసరమా అండి?"
                                        else:
                                            if call_language == "en":
                                                reply_text = "This is Sneha from the personal loan department. I’m calling regarding ICICI Bank personal loans. Do you need a personal loan?"
                                            else:
                                                reply_text = "నేను పర్సనల్ లోన్ డిపార్ట్మెంట్ నుండి స్నేహని మాట్లాడుతున్నాను. ఐసీఐసీఐ బ్యాంక్ పర్సనల్ లోన్ గురించి కాల్ చేస్తున్నాను. మీకు పర్సనల్ లోన్ అవసరమా?"
                                    else:
                                        reply_text = await generate_chat_reply(messages_payload, max_tokens=60)

                                # 7. Unemployed / No Work:
                                elif is_unemployed_response(customer_text):
                                    print("Customer identified as unemployed / no business. Ending call politely.")
                                    if call_language == "en":
                                        reply_text = "Thank you for your time. Have a great day!"
                                    else:
                                        reply_text = "సరే అండి, మీ విలువైన సమయానికి ధన్యవాదాలు. హావ్ ఏ నైస్ డే!"

                                # 8. Customer answers "Company" to Job or Business question:
                                elif is_company_response(customer_text) and any(w in last_asst_msg for w in ("job", "business", "జాబ్", "బిజినెస్", "చేస్తున్నారా")):
                                    print(f"[COMPANY RESPONSE] Customer answered company ('{customer_text}'). Asking employee vs own company.")
                                    if call_language == "en":
                                        reply_text = "Are you working in a company as an employee, or is it your own company?"
                                    else:
                                        reply_text = "మీరు కంపెనీలో జాబ్ చేస్తున్నారా లేదా అది మీ సొంత కంపెనీనా అండి?"

                                # 9. Customer clarifying employee vs own company:
                                elif any(w in last_asst_msg for w in ("working in a company", "own company", "సొంత కంపెనీ", "కంపెనీలో జాబ్")):
                                    c_low = customer_text.lower()
                                    if any(w in c_low for w in (
                                        "work", "working", "employee", "job", "private", "pvt", "in a company",
                                        "menu", "me no", "me", "nenu", "neenu", "nene", "nenandi", "నేను", "నేనండి",
                                        "జాబ్", "వర్క్", "ఉద్యోగం", "చేస్తున్నా", "చేస్తున్నాను", "వర్కింగ్"
                                    )):
                                        print(f"[EMPLOYMENT CLARIFIED] Customer confirmed salaried employee ('{customer_text}'). Asking company name.")
                                        if call_language == "en":
                                            reply_text = "Which company are you working in?"
                                        else:
                                            reply_text = "మీరు ఏ కంపెనీలో వర్క్ చేస్తున్నారు అండి?"
                                    elif any(w in c_low for w in ("own", "my own", "mine", "business", "owner", "proprietor", "సొంత", "బిజినెస్")):
                                        print(f"[EMPLOYMENT CLARIFIED] Customer confirmed own company / business ('{customer_text}'). Asking business type.")
                                        if call_language == "en":
                                            reply_text = "What is the nature of your business?"
                                        else:
                                            reply_text = "మీది ఏ విధమైన బిజినెస్ అండి?"
                                    else:
                                        reply_text = await generate_chat_reply(messages_payload, max_tokens=60)

                                # 10. Customer confirms or says continuation words (e.g. 'click', 'check', 'continue', 'forward', 'formward', 'proceed', 'yes', 'sure', 'tell me', etc.):
                                elif is_continuation and (not job_already_asked or is_eligibility_context or len(conversation_history) <= 4):
                                    print(f"[CONTINUATION DETECTED] Customer confirmed continuation ('{customer_text}'). Asking job/business.")
                                    if call_language == "en":
                                        reply_text = "Are you doing a job or business?"
                                    else:
                                        reply_text = "సరే అండి! మీరు ప్రస్తుతం జాబ్ చేస్తున్నారా లేదా బిజినెస్ చేస్తున్నారా?"

                                else:
                                    reply_text = await generate_chat_reply(messages_payload, max_tokens=60)
                                    # Fallback safety: If OpenAI generated closing reply despite affirmative continuation, override it!
                                    if is_closing_reply(reply_text) and is_continuation:
                                        print(f"[SAFETY OVERRIDE] Customer continued ('{customer_text}'), but OpenAI gave closing reply. Overriding to job/business.")
                                        if not job_already_asked:
                                            if call_language == "en":
                                                reply_text = "Are you doing a job or business?"
                                            else:
                                                reply_text = "సరే అండి! మీరు ప్రస్తుతం జాబ్ చేస్తున్నారా లేదా బిజినెస్ చేస్తున్నారా?"

                                    # End-of-Questionnaire Safety: Before cutting the call with Thank You, ALWAYS ask executive callback day & time!
                                    callback_already_asked = any(
                                        is_callback_time_query(m.get("content", ""))
                                        for m in conversation_history
                                        if m.get("role") == "assistant"
                                    )
                                    if is_closing_reply(reply_text) and not callback_already_asked:
                                        if len(conversation_history) > 3 and not detect_no(customer_text) and not is_unemployed_response(customer_text):
                                            print(f"[CALLBACK TIME INTERCEPT] All questions over. Asking executive callback time before closing.")
                                            if call_language == "en":
                                                reply_text = "On which day and at what time would it be convenient for our executive to call you?"
                                            else:
                                                reply_text = "మా ఎగ్జిక్యూటివ్ మీకు కాల్ చేయడానికి ఏ రోజు, ఏ టైమ్ మీకు కన్వీనియంట్‌గా ఉంటుంది అండి?" 

                                if not reply_text:
                                    print("OpenAI returned an empty reply.")
                                    continue

                                reply_text = extract_single_question(reply_text)

                                conversation_history.append({
                                    "role": "assistant",
                                    "content": reply_text,
                                })

                                print("AI REPLY:", reply_text)
                                print(f"Streaming speech chunks ({tts_language}) to Plivo...")

                                total_duration, total_bytes, playback_end = await play_chunked_speech(
                                    websocket=websocket,
                                    text_source=reply_text,
                                    language=tts_language,
                                    call_audio_path=call_audio_path,
                                    silence_ms=60,
                                    t_vad_end=time.time(),
                                    call_sid=call_sid,
                                )

                                ai_speaking_until = playback_end
                                last_interaction_time = max(time.time(), ai_speaking_until)
                                print(f"Audio sent to Plivo: {total_bytes} bytes (~{total_duration:.2f}s speech)")

                                # Automatic call hangup detection:
                                should_close = (
                                    is_callback_closing
                                    or (is_closing_reply(reply_text) and not (
                                        (len(conversation_history) <= 4 and is_continuation)
                                        or is_company_response(customer_text)
                                        or is_confusion_or_identity_query(customer_text)
                                        or is_interest_rate_or_details_query(customer_text)
                                        or is_repeat_or_clarification_query(customer_text)
                                        or is_callback_time_query(reply_text)
                                    ))
                                    or detect_end_request(customer_text)
                                )
                                if should_close:
                                    print(f"Conversation finished. Closing call stream gracefully. [reply_text='{reply_text}']")
                                    stream_ended_normally = True

                                    # Save immediately to database
                                    if not saved_to_db and conversation_history:
                                        saved_to_db = True
                                        try:
                                            call_ended_at = datetime.now(timezone.utc)
                                            await asyncio.to_thread(
                                                save_call_record,
                                                call_sid=call_sid,
                                                phone_number=phone_number,
                                                conversation_history=conversation_history,
                                                call_status="completed",
                                                call_started_at=call_started_at,
                                                call_ended_at=call_ended_at,
                                                loan_purpose="AI Agent Cut (Completed normally)",
                                            )
                                        except Exception as db_exc:
                                            print("Error saving call record to DB:", repr(db_exc))

                                    playback_wait = max(2.5, total_duration + 0.6)
                                    print(f"[HANGUP] Waiting {playback_wait:.2f}s for playback to complete before telecom line cut...")
                                    await asyncio.sleep(playback_wait)
                                    call_done_event.set()
                                    try:
                                        await websocket.close()
                                    except Exception:
                                        pass
                                    if call_sid:
                                        print(f"[HANGUP] Line cut triggered via Plivo REST API for: {call_sid}")
                                        try:
                                            await asyncio.to_thread(hangup_plivo_call, call_sid)
                                        except Exception as hang_exc:
                                            print("Error in hangup_plivo_call:", repr(hang_exc))
                                    break

                            except Exception as exc:
                                print("AI speech response failed:", repr(exc))
                                print("Error details:", getattr(exc, "body", None))
                                print("HTTP status:", getattr(exc, "status_code", None))

                                extracted_q = None
                                err_body = getattr(exc, "body", None)
                                failed_gen = ""
                                if isinstance(err_body, dict):
                                    failed_gen = str(err_body.get("error", {}).get("failed_generation", ""))
                                if not failed_gen:
                                    failed_gen = str(exc)

                                if failed_gen and "{" in failed_gen:
                                    try:
                                        parsed_data = json.loads(failed_gen)
                                        args = parsed_data.get("arguments", {})
                                        if isinstance(args, str):
                                            try:
                                                args = json.loads(args)
                                            except Exception:
                                                pass
                                        if isinstance(args, dict):
                                            for k in ("text", "content", "message", "response", "reply", "answer", "query", "question"):
                                                val = args.get(k)
                                                if val and isinstance(val, str) and len(val.strip()) > 1:
                                                    extracted_q = val.strip()
                                                    break
                                    except Exception:
                                        pass

                                if not extracted_q or len(extracted_q) > 80:
                                    extracted_q = ("Please say your answer one more time." if call_language == "en" else "ప్లీజ్, మీ ఆన్సర్ ఇంకోసారి చెప్పగలరా?")

                                reply_text = extracted_q.strip()
                                print("Recovering from error with retry prompt:", reply_text)

                                conversation_history.append({
                                    "role": "assistant",
                                    "content": reply_text,
                                })

                                try:
                                    audio_bytes = await asyncio.to_thread(
                                        synthesize_sarvam_speech,
                                        reply_text,
                                        tts_language,
                                    )
                                    if audio_bytes and len(audio_bytes) <= 80000:
                                        if call_audio_path:
                                            with open(call_audio_path, "ab") as f:
                                                f.write(audio_bytes)
                                        audio_base64 = base64.b64encode(audio_bytes).decode("ascii")
                                        await send_audio_to_plivo(websocket, audio_base64)
                                        ai_speaking_until = time.time() + (len(audio_bytes) / 8000.0)
                                        print("Retry audio sent to Plivo:", len(audio_bytes), "bytes")
                                    elif audio_bytes and len(audio_bytes) > 80000:
                                        print("Warning: Retry audio too large (" + str(len(audio_bytes)) + " bytes), skipped to prevent line block.")
                                except Exception as speech_exc:
                                    print("Failed to send retry audio:", speech_exc)
                            finally:
                                is_processing_turn = False

                        elif event_type == "error":
                            print("Sarvam streaming error:", event)

                except asyncio.CancelledError:
                    pass
                except Exception as stream_err:
                    print("Sarvam stream listener ended:", repr(stream_err))
                finally:
                    call_done_event.set()

            async def silence_watchdog():
                nonlocal silence_prompt_stage, last_interaction_time, saved_to_db, stream_ended_normally, ai_speaking_until, is_processing_turn
                try:
                    # Wait until opening message has completed playing
                    while not call_done_event.is_set() and not opening_already_spoken:
                        await asyncio.sleep(0.5)

                    while not call_done_event.is_set():
                        await asyncio.sleep(1.0)

                        if call_done_event.is_set():
                            break

                        # If AI is currently thinking or speaking, silence timer does not tick
                        if is_ai_active():
                            last_interaction_time = max(time.time(), ai_speaking_until)
                            continue

                        now = time.time()
                        silence_duration = now - last_interaction_time

                        # Stage 0: 20 seconds of customer silence -> play reminder
                        if silence_prompt_stage == 0 and silence_duration >= 20.0:
                            print(f"[SILENCE WATCHDOG] 20s customer silence detected ({silence_duration:.1f}s). Playing reminder prompt.")
                            silence_prompt_stage = 1

                            if len(conversation_history) <= 2:
                                if call_language == "en":
                                    reminder_text = "Hello, are you there? This is Sneha calling regarding ICICI Bank personal loans. Do you need a personal loan?"
                                else:
                                    reminder_text = "హలో, లైన్‌లో ఉన్నారా అండి? నేను ఐసీఐసీఐ బ్యాంక్ పర్సనల్ లోన్ గురించి మాట్లాడుతున్నాను. మీకు పర్సనల్ లోన్ అవసరమా?"
                            else:
                                if call_language == "en":
                                    reminder_text = "Hello, are you there? Could you please confirm your answer?"
                                else:
                                    reminder_text = "హలో, లైన్‌లో ఉన్నారా అండి? మీ సమాధానం చెప్పగలరా?"

                            conversation_history.append({
                                "role": "assistant",
                                "content": reminder_text,
                            })
                            print(f"[SILENCE WATCHDOG] Reminder: '{reminder_text}'")

                            is_processing_turn = True
                            try:
                                tts_lang = call_language or "te"
                                total_dur, total_b, playback_end = await play_chunked_speech(
                                    websocket=websocket,
                                    text_source=reminder_text,
                                    language=tts_lang,
                                    call_audio_path=call_audio_path,
                                    silence_ms=60,
                                    t_vad_end=time.time(),
                                    call_sid=call_sid,
                                )
                                ai_speaking_until = playback_end
                                last_interaction_time = max(time.time(), ai_speaking_until)
                            except Exception as rem_err:
                                print("[SILENCE WATCHDOG] Failed to play reminder:", rem_err)
                                last_interaction_time = time.time()
                            finally:
                                is_processing_turn = False

                        # Stage 1: 30 seconds of further silence after reminder -> farewell and hang up
                        elif silence_prompt_stage == 1 and silence_duration >= 30.0:
                            print(f"[SILENCE WATCHDOG] 30s silence after reminder ({silence_duration:.1f}s). Ending call.")
                            silence_prompt_stage = 2
                            stream_ended_normally = True

                            if call_language == "en":
                                farewell_text = "Thank you for your time. Have a nice day!"
                            else:
                                farewell_text = "సరే అండి, మీ విలువైన సమయానికి ధన్యవాదాలు. హావ్ ఏ నైస్ డే!"

                            conversation_history.append({
                                "role": "assistant",
                                "content": farewell_text,
                            })
                            print(f"[SILENCE WATCHDOG] Farewell: '{farewell_text}'")

                            is_processing_turn = True
                            try:
                                tts_lang = call_language or "te"
                                total_dur, total_b, _ = await play_chunked_speech(
                                    websocket=websocket,
                                    text_source=farewell_text,
                                    language=tts_lang,
                                    call_audio_path=call_audio_path,
                                    silence_ms=60,
                                    t_vad_end=time.time(),
                                    call_sid=call_sid,
                                )
                                playback_wait = max(2.5, total_dur + 0.6)
                                print(f"[SILENCE WATCHDOG] Waiting {playback_wait:.2f}s before cutting line...")
                                await asyncio.sleep(playback_wait)
                            except Exception as fare_err:
                                print("[SILENCE WATCHDOG] Failed to play farewell:", fare_err)
                            finally:
                                is_processing_turn = False

                            # Save to database
                            if not saved_to_db and conversation_history:
                                saved_to_db = True
                                try:
                                    call_ended_at = datetime.now(timezone.utc)
                                    conversation_history.append({
                                        "role": "system",
                                        "content": "Call ended: AI agent timed out after customer silence (20s + 30s) and cut the call",
                                    })
                                    await asyncio.to_thread(
                                        save_call_record,
                                        call_sid=call_sid,
                                        phone_number=phone_number,
                                        conversation_history=conversation_history,
                                        call_status="completed",
                                        call_started_at=call_started_at,
                                        call_ended_at=call_ended_at,
                                        loan_purpose="AI Agent Cut (Silence Timeout)",
                                    )
                                except Exception as db_exc:
                                    print("Error saving call record to DB on silence timeout:", repr(db_exc))

                            call_done_event.set()
                            try:
                                await websocket.close()
                            except Exception:
                                pass
                            if call_sid:
                                print(f"[SILENCE WATCHDOG] Cutting telecom line via Plivo API for: {call_sid}")
                                try:
                                    await asyncio.to_thread(hangup_plivo_call, call_sid)
                                except Exception as h_exc:
                                    print("Error in hangup_plivo_call on silence timeout:", repr(h_exc))
                            break

                except asyncio.CancelledError:
                    pass
                except Exception as exc:
                    print("[SILENCE WATCHDOG] Error:", repr(exc))

            opening_already_spoken = False
            listener_task = asyncio.create_task(receive_sarvam_results())
            silence_task = asyncio.create_task(silence_watchdog())

            print("Sarvam realtime transcription connected.")

            while not call_done_event.is_set():
                try:
                    message = await asyncio.wait_for(websocket.receive_json(), timeout=1.0)
                except asyncio.TimeoutError:
                    if call_done_event.is_set():
                        break
                    continue
                except WebSocketDisconnect:
                    print("Plivo WebSocket disconnected.")
                    break

                event_type = message.get("event")

                if event_type == "start":
                    start_data = message.get("start", {})
                    print("Plivo stream started:", start_data)

                    stream_call_id = start_data.get("callId")
                    if stream_call_id:
                        call_sid = stream_call_id
                        asyncio.create_task(asyncio.to_thread(start_plivo_recording, stream_call_id))
                        meta = (
                            ACTIVE_CALL_METADATA.get(stream_call_id)
                            or ACTIVE_CALL_METADATA.get("latest")
                            or (list(ACTIVE_CALL_METADATA.values())[-1] if ACTIVE_CALL_METADATA else None)
                        )
                        if meta:
                            phone_number = meta.get("phone_number")
                            if "started_at" in meta:
                                call_started_at = meta["started_at"]

                    if not call_sid:
                        call_sid = f"call_{uuid.uuid4().hex[:12]}"

                    call_audio_path = ALL_CALL_RECORDINGS_DIR / f"call_{call_sid}.ulaw"

                    try:
                        opening_text, opening_len, opening_duration, playback_end = await speak_opening_message(
                            websocket,
                            call_audio_path=call_audio_path,
                            call_sid=call_sid,
                            language="en",
                        )
                        ai_speaking_until = playback_end
                        last_interaction_time = max(time.time(), ai_speaking_until)
                        silence_prompt_stage = 0
                        if not conversation_history:
                            conversation_history.append({
                                "role": "assistant",
                                "content": opening_text,
                            })
                        opening_already_spoken = True
                    except Exception as exc:
                        print("Opening speech failed:", repr(exc))
                        opening_already_spoken = True
                        last_interaction_time = time.time()

                elif event_type == "media":
                    media = message.get("media", {})
                    if media.get("track") == "inbound" and media.get("payload"):
                        audio_packet_count += 1
                        if audio_packet_count == 1:
                            print("First inbound Plivo audio packet received.")

                        # Continuous feed to Sarvam AI STT so VAD stays alive and captures user speech immediately
                        await sarvam_socket.send_realtime_audio_input(
                            RealtimeAudioInput(audio=media["payload"])
                        )

                elif event_type == "stop":
                    print("Plivo stream stopped.")
                    break

    except WebSocketDisconnect:
        print("Plivo WebSocket disconnected.")
    except Exception as exc:
        print("Streaming error:", repr(exc))
    finally:
        call_done_event.set()
        if silence_task:
            silence_task.cancel()
            try:
                await silence_task
            except (asyncio.CancelledError, Exception):
                pass
        if listener_task:
            listener_task.cancel()
            try:
                await listener_task
            except (asyncio.CancelledError, Exception):
                pass

        if not saved_to_db and conversation_history:
            saved_to_db = True
            try:
                call_ended_at = datetime.now(timezone.utc)
                saved_status = "completed"
                if stream_ended_normally:
                    outcome_desc = "AI Agent Cut (Completed normally)"
                    conversation_history.append({
                        "role": "system",
                        "content": "Call ended: AI agent completed the conversation and cut the call"
                    })
                else:
                    outcome_desc = "User Cut (Customer hung up early)"
                    conversation_history.append({
                        "role": "system",
                        "content": "Call ended: User left the call (Customer disconnected midway)"
                    })
                await asyncio.to_thread(
                    save_call_record,
                    call_sid=call_sid,
                    phone_number=phone_number,
                    conversation_history=conversation_history,
                    call_status=saved_status,
                    call_started_at=call_started_at,
                    call_ended_at=call_ended_at,
                    loan_purpose=outcome_desc,
                )
            except Exception as db_exc:
                print("Error saving call conversation to database in finally:", repr(db_exc))


@app.get("/calls")
def get_recorded_calls():
    """Retrieve recorded calls from PostgreSQL public.ai_agent_calls table."""
    return fetch_all_calls()


@app.get("/recording/{call_id}")
def stream_call_recording(call_id: int):
    """Stream and play call recording directly from PostgreSQL recording_data BYTEA."""
    audio_bytes, call_sid = fetch_call_recording(call_id, ALL_CALL_RECORDINGS_DIR)
    if not audio_bytes:
        raise HTTPException(status_code=404, detail="Recording audio not yet available for this call ID")
    return Response(
        content=audio_bytes,
        media_type="audio/mpeg",
        headers={
            "Content-Disposition": f'inline; filename="call_{call_id}_{call_sid}.mp3"',
            "Accept-Ranges": "bytes",
        },
    )
