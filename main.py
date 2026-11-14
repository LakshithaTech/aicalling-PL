import os
import sys
import time
import base64
import uuid
import json
import threading
from pathlib import Path
from typing import Optional, Dict, Any, List
from datetime import datetime, timezone
from urllib.parse import parse_qs, quote

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
from fastapi import Depends, FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect, UploadFile, File
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel
import database
from components.personal_loan_flow import CallFlow, Turn, opening, question, respond, confirmation_after_save
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
    cancel_call_tts,
)
from components.openai_service import (
    OPENAI_MODEL,
    get_async_openai_client,
    get_openai_client,
    generate_chat_reply,
    generate_sync_chat_reply,
    stream_chat_reply_chunks,
)
from components.plivo_service import (
    ACTIVE_CALL_METADATA,
    get_base_url,
    hangup_plivo_call,
    start_plivo_recording,
    send_audio_to_plivo,
    clear_plivo_audio_queue,
    download_and_save_recording,
)
from components.questionnaire import (
    is_unemployed_response,
    detect_end_request,
    extract_single_question,
    is_closing_reply,
    detect_yes,
    detect_no,
    is_recording_announcement,
    detect_explicit_language_switch,
    detect_initial_call_language,
    is_telugu_speech,
    get_recording_followup,
    get_opening_text,
    get_personal_loan_opening_text,
    speak_opening_message,
)
from components.db import (
    get_db_pool,
    save_call_record,
    save_recording_data,
    fetch_all_calls,
    fetch_call_recording,
    delete_personal_loan_call,
    delete_personal_loan_calls_batch,
)
from components.auth import (
    COOKIE_NAME,
    TOKEN_EXPIRE_HOURS,
    authenticate_admin,
    extract_client_ip,
    fetch_login_logs,
    init_auth,
    register_admin,
    require_admin_user,
)

load_dotenv(Path(__file__).resolve().parent / ".env", override=True)

app = FastAPI(title="Personal Loan AI Voice Agent API")
print("RUNNING MAIN FILE:", Path(__file__).resolve())
init_auth()

AUDIO_DIR = Path("generated_audio")
AUDIO_DIR.mkdir(exist_ok=True)
ALL_CALL_RECORDINGS_DIR = Path("allcallrecordings")
ALL_CALL_RECORDINGS_DIR.mkdir(exist_ok=True)

# Demo-only conversation memory
CALL_STATE: Dict[str, Any] = {}

class CallRequest(BaseModel):
    to_number: str
    customer_name: Optional[str] = None


class AdminLoginRequest(BaseModel):
    identifier: Optional[str] = ""
    username: Optional[str] = ""
    email: Optional[str] = ""
    password: str = ""


class AdminRegisterRequest(BaseModel):
    username: str = ""
    email: str = ""
    password: str = ""


def _set_auth_cookie(request: Request, response: Response, token: str) -> None:
    is_https = (
        request.url.scheme == "https"
        or request.headers.get("x-forwarded-proto", "").lower() == "https"
    )
    response.set_cookie(
        key=COOKIE_NAME,
        value=token,
        max_age=TOKEN_EXPIRE_HOURS * 3600,
        httponly=True,
        samesite="lax",
        secure=is_https,
        path="/",
    )


@app.get("/")
def health():
    return {
        "status": "ok",
        "service": "Personal Loan AI Voice Agent",
    }


@app.post("/auth/login")
def admin_login(payload: AdminLoginRequest, request: Request, response: Response):
    ident = (payload.identifier or payload.username or payload.email or "").strip()
    result = authenticate_admin(
        identifier=ident,
        password=payload.password or "",
        ip_address=extract_client_ip(request),
        user_agent=request.headers.get("user-agent"),
    )
    _set_auth_cookie(request, response, result["token"])
    return result


@app.post("/auth/register")
def admin_register(payload: AdminRegisterRequest, request: Request, response: Response):
    result = register_admin(
        username=payload.username,
        email=payload.email,
        password=payload.password,
        ip_address=extract_client_ip(request),
        user_agent=request.headers.get("user-agent"),
    )
    _set_auth_cookie(request, response, result["token"])
    return result


@app.post("/auth/logout")
def admin_logout(response: Response):
    response.delete_cookie(key=COOKIE_NAME, path="/")
    return {"success": True, "message": "Signed out from LMV_HL Calling"}


@app.get("/auth/me")
def admin_me(current_admin: Dict[str, Any] = Depends(require_admin_user)):
    return {"authenticated": True, "user": current_admin}


@app.get("/auth/logs")
def admin_login_logs(
    limit: int = 200,
    current_admin: Dict[str, Any] = Depends(require_admin_user),
):
    result = fetch_login_logs(limit=limit)
    if result.get("error"):
        raise HTTPException(status_code=500, detail=result["error"])
    return result


@app.get("/audio.wav")
def generate_greeting_audio():
    try:
        filename = make_sarvam_audio(
            get_personal_loan_opening_text(),
            "greeting.wav",
            language="en",
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


async def process_flow_turn(flow: CallFlow, text: str, call_sid: str,
                            phone_number: Optional[str], history: List[Dict[str, Any]],
                            started_at: datetime, detected_language: Optional[str] = None) -> Turn:
    """One shared conversation engine for WebSocket and HTTP fallback routes."""
    history.append({"role": "user", "content": text})
    turn = await respond(flow, text, detected_language)
    if turn.persist:
        if turn.persist == "opt_out":
            saved = bool(phone_number) and await asyncio.to_thread(database.save_do_not_call, phone_number)
        else:
            # Persist callback preference before claiming it is recorded.
            record = history + [{"role": "system", "content":
                                  "CALL_STRUCTURED_DATA=" + json.dumps(flow.snapshot(), ensure_ascii=False)}]
            saved = bool(phone_number and call_sid) and bool(await asyncio.to_thread(
                save_call_record, call_sid=call_sid, phone_number=phone_number,
                conversation_history=record, call_status="completed",
                lead_status="follow_up", loan_type="personal_loan",
                customer_name=flow.customer_name or None,
                call_started_at=started_at, call_ended_at=datetime.now(timezone.utc),
                loan_purpose=flow.answers.get("purpose", "Personal loan callback request")))
        turn.text = confirmation_after_save(flow, turn.persist, bool(saved))
    history.append({"role": "assistant", "content": turn.text})
    return turn


async def _http_speech_route(request: Request, action_path: str):
    """Plivo GetInput alternative to the low-latency WebSocket route."""
    from xml.sax.saxutils import escape
    base_url = get_base_url()
    if not base_url:
        raise HTTPException(status_code=500, detail="PUBLIC_BASE_URL is missing")
    try:
        form_data = dict(await request.form())
    except Exception:
        form_data = parse_qs((await request.body()).decode("utf-8"))
        form_data = {k: v[0] for k, v in form_data.items()}
    text = (form_data.get("SpeechResult") or form_data.get("Speech")
            or request.query_params.get("SpeechResult") or "").strip()
    call_sid = (form_data.get("CallUUID") or form_data.get("CallSid")
                or request.query_params.get("CallUUID") or "unknown")
    metadata = ACTIVE_CALL_METADATA.get(call_sid, {})
    phone = metadata.get("phone_number")
    if not phone:
        from_no, to_no = form_data.get("From", ""), form_data.get("To", "")
        own_no = normalize_phone_digits(os.getenv("PLIVO_PHONE_NUMBER", ""))
        phone = to_no if normalize_phone_digits(from_no) == own_no else from_no
    state = CALL_STATE.setdefault(call_sid, {"messages": [], "flow": CallFlow(),
                                             "started_at": datetime.now(timezone.utc)})
    flow = state.setdefault("flow", CallFlow())
    if flow.status == "ended":
        return Response(content="<Response><Hangup/></Response>", media_type="application/xml")
    if metadata.get("customer_name"):
        flow.customer_name = metadata["customer_name"]
    state.setdefault("messages", [])
    if text:
        state["silence_count"] = 0
        turn = await process_flow_turn(flow, text, call_sid, phone, state["messages"], state["started_at"])
        reply_text = turn.text
    else:
        turn = Turn("")
        reply_text = question(flow)
    state["current_question"] = question(flow) if flow.status != "ended" else ""
    filename = f"{uuid.uuid4().hex}.wav"
    try:
        await asyncio.to_thread(make_sarvam_audio, reply_text, filename,
                                language=flow.language, audio_dir=AUDIO_DIR)
    except Exception as exc:
        print("Sarvam HTTP speech generation failed:", type(exc).__name__)
        return Response(content="<Response><Say>Sorry, there is a voice service problem.</Say><Hangup/></Response>",
                        media_type="application/xml")
    audio_url = escape(f"{base_url}/reply/{filename}")
    action_url = escape(f"{base_url}{action_path}")
    timeout_url = escape(f"{base_url}/plivo-timeout?CallUUID={quote(call_sid)}")
    if turn.close:
        xml = f'<Response><Play>{audio_url}</Play><Hangup/></Response>'
    else:
        xml = (f'<Response><GetInput inputType="speech" action="{action_url}" '
               f'method="POST" executionTimeout="30"><Play>{audio_url}</Play>'
               f'</GetInput><Redirect method="POST">{timeout_url}</Redirect></Response>')
    return Response(content=xml, media_type="application/xml")


@app.post("/process-speech")
async def process_speech(request: Request):
    return await _http_speech_route(request, "/process-speech")


OUTBOUND_DIAL_LOCK = threading.Lock()
ACTIVE_OUTBOUND_DIALS: Dict[str, Dict[str, Any]] = {}
MIN_REDIAL_COOLDOWN_SEC = 15.0
ACTIVE_CALL_GUARD_TTL_SEC = 180.0


def normalize_phone_digits(num: str) -> str:
    d = "".join([c for c in str(num or "") if c.isdigit()])
    if len(d) == 10:
        return "91" + d
    if d.startswith("0"):
        return "91" + d[1:]
    return d


def _release_outbound_guard(phone_number: str = "", call_uuid: str = "") -> None:
    norm_p = normalize_phone_digits(phone_number) if phone_number else ""
    with OUTBOUND_DIAL_LOCK:
        if norm_p and norm_p in ACTIVE_OUTBOUND_DIALS:
            entry = ACTIVE_OUTBOUND_DIALS[norm_p]
            if not call_uuid or entry.get("call_uuid") == call_uuid:
                ACTIVE_OUTBOUND_DIALS.pop(norm_p, None)
        if call_uuid:
            stale_keys = [
                k for k, v in ACTIVE_OUTBOUND_DIALS.items()
                if v.get("call_uuid") == call_uuid
            ]
            for k in stale_keys:
                ACTIVE_OUTBOUND_DIALS.pop(k, None)


def _is_uuid_active_on_plivo(client: Any, call_uuid: str) -> bool:
    if not call_uuid:
        return False
    active_ids = set()
    try:
        live_res = client.calls.live_call_list_ids()
        if isinstance(live_res, dict):
            active_ids.update(live_res.get("calls", []) or [])
    except Exception:
        pass
    try:
        queued_res = client.calls.queued_call_list_ids()
        if isinstance(queued_res, dict):
            active_ids.update(queued_res.get("calls", []) or [])
    except Exception:
        pass
    return call_uuid in active_ids


def _is_call_completed_in_db(call_sid: str) -> bool:
    if not call_sid:
        return False
    p = get_db_pool()
    if not p:
        return False
    conn = None
    try:
        conn = p.getconn()
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT call_status, call_duration_seconds
                FROM public.ai_agent_calls
                WHERE call_sid = %s
                LIMIT 1;
                """,
                (call_sid,),
            )
            row = cur.fetchone()
            if not row:
                return False
            st = str(row[0] or "").lower()
            dur = int(row[1] or 0)
            return st == "completed" or dur > 0
    except Exception:
        return False
    finally:
        if conn and p:
            try:
                p.putconn(conn)
            except Exception:
                pass


@app.post("/call")
def make_call(
    request: CallRequest,
    current_admin: Dict[str, Any] = Depends(require_admin_user),
):
    auth_id = os.getenv("PLIVO_AUTH_ID")
    auth_token = os.getenv("PLIVO_AUTH_TOKEN")
    from_number = os.getenv("PLIVO_PHONE_NUMBER")
    base_url = get_base_url()

    if not all([auth_id, auth_token, from_number, base_url]):
        raise HTTPException(
            status_code=500,
            detail="Plivo credentials, phone number, or PUBLIC_BASE_URL are missing",
        )

    dnc_status = database.is_do_not_call(request.to_number)
    if dnc_status is True:
        raise HTTPException(status_code=403, detail="Number is on the do-not-call list")
    if dnc_status is None:
        raise HTTPException(status_code=503, detail="Unable to verify do-not-call status; dialling blocked")

    norm_phone = normalize_phone_digits(request.to_number)
    client = plivo.RestClient(auth_id, auth_token)
    now_mono = time.monotonic()

    # Prevent duplicate / simultaneous calls to the same phone number
    existing_to_verify = None
    with OUTBOUND_DIAL_LOCK:
        expired = [
            k for k, v in ACTIVE_OUTBOUND_DIALS.items()
            if (now_mono - float(v.get("started_at", 0))) > ACTIVE_CALL_GUARD_TTL_SEC
        ]
        for k in expired:
            ACTIVE_OUTBOUND_DIALS.pop(k, None)

        existing = ACTIVE_OUTBOUND_DIALS.get(norm_phone) if norm_phone else None
        if existing:
            elapsed = now_mono - float(existing.get("started_at", 0))
            if existing.get("in_progress") and elapsed < MIN_REDIAL_COOLDOWN_SEC:
                raise HTTPException(
                    status_code=409,
                    detail="Call initiation already in progress for this phone number",
                )
            existing_uuid = existing.get("call_uuid")
            if elapsed < MIN_REDIAL_COOLDOWN_SEC and existing_uuid:
                return {
                    "message": "Plivo call already initiated recently (deduplicated)",
                    "call_uuid": existing_uuid,
                    "deduplicated": True,
                }
            if existing_uuid:
                existing_to_verify = existing_uuid

    if existing_to_verify:
        if _is_uuid_active_on_plivo(client, existing_to_verify):
            return {
                "message": "Call is still active on Plivo for this number (deduplicated)",
                "call_uuid": existing_to_verify,
                "deduplicated": True,
            }
        _release_outbound_guard(phone_number=norm_phone, call_uuid=existing_to_verify)

    if norm_phone:
        with OUTBOUND_DIAL_LOCK:
            existing = ACTIVE_OUTBOUND_DIALS.get(norm_phone)
            if existing:
                elapsed = time.monotonic() - float(existing.get("started_at", 0))
                if elapsed < MIN_REDIAL_COOLDOWN_SEC:
                    if existing.get("call_uuid"):
                        return {
                            "message": "Plivo call already initiated recently (deduplicated)",
                            "call_uuid": existing["call_uuid"],
                            "deduplicated": True,
                        }
                    raise HTTPException(
                        status_code=409,
                        detail="Call initiation already in progress for this phone number",
                    )
            ACTIVE_OUTBOUND_DIALS[norm_phone] = {
                "started_at": time.monotonic(),
                "call_uuid": None,
                "in_progress": True,
            }

    try:
        response = client.calls.create(
            from_=from_number,
            to_=request.to_number,
            answer_url=f"{base_url}/plivo-stream-answer?customer_name={quote(request.customer_name or '')}",
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
        if isinstance(req_uuid, (list, tuple)):
            req_uuid = req_uuid[0] if req_uuid else None

        if norm_phone:
            with OUTBOUND_DIAL_LOCK:
                ACTIVE_OUTBOUND_DIALS[norm_phone] = {
                    "started_at": time.monotonic(),
                    "call_uuid": req_uuid,
                    "in_progress": False,
                }
        return {
            "message": "Plivo call request submitted",
            "call_uuid": req_uuid,
            "details": str(response),
        }
    except Exception as exc:
        if norm_phone:
            _release_outbound_guard(phone_number=norm_phone)
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
    Ensures non-connected calls are tracked in PostgreSQL without overwriting completed calls.
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

    if call_status_raw in ("completed", "busy", "no-answer", "failed", "cancel", "hangup") or hangup_cause:
        _release_outbound_guard(phone_number=to_number, call_uuid=call_uuid)

    # If call was not answered or failed (busy, no-answer, rejected, failed, cancel)
    if call_status_raw in ("busy", "no-answer", "failed", "cancel") or hangup_cause in ("USER_BUSY", "NO_ANSWER", "CALL_REJECTED"):
        already_completed = await asyncio.to_thread(_is_call_completed_in_db, call_uuid)
        if already_completed:
            print(f"Skipping non-connected overwrite for already completed CallUUID={call_uuid}")
            return Response(content="<Response/>", media_type="application/xml")

        if call_status_raw == "busy" or "BUSY" in hangup_cause or "REJECTED" in hangup_cause:
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
                loan_type="personal_loan",
                loan_purpose=loan_purpose,
                customer_name=None,
            )
            print(f"Recorded non-connected call status '{db_call_status}' ({loan_purpose}) to DB for {call_uuid}")
        except Exception as exc:
            print("Error saving call status callback to DB:", repr(exc))

    return Response(content="<Response/>", media_type="application/xml")


class BatchSyncRequest(BaseModel):
    phone_numbers: List[str] = []
    call_uuids: Optional[List[str]] = []
    call_uuid_by_phone: Optional[Dict[str, str]] = None


def _classify_db_rec_outcome(db_rec: Dict[str, Any]) -> tuple[str, Optional[str]]:
    db_status = (db_rec.get("status") or "").lower()
    purpose = (db_rec.get("loan_purpose") or "").lower()
    dur = int(db_rec.get("duration") or 0)
    conv = db_rec.get("conversation") or []
    sys_texts = " ".join([
        m.get("content", "")
        for m in conv
        if isinstance(m, dict) and m.get("role") == "system"
    ]).lower()

    if (
        dur > 0
        or db_status == "completed"
        or "user left" in purpose
        or "user cut" in purpose
        or "user left" in sys_texts
        or "disconnected midway" in sys_texts
    ):
        is_user_cut = (
            "user left" in purpose
            or "user cut" in purpose
            or "user left" in sys_texts
            or "disconnected midway" in sys_texts
        )
        is_ai_cut = (
            "completed normally" in sys_texts
            or "ai agent completed" in sys_texts
            or "ai agent cut" in purpose
            or '"status": "ended"' in sys_texts
        )
        cut_by = "user" if is_user_cut else ("ai" if is_ai_cut else "user")
        return "answered", cut_by

    if "busy" in db_status or "busy" in purpose or "busy" in sys_texts or "rejected" in purpose:
        return "busy", None

    return "no_answer", None


def _is_recent_plivo_end(end_time: Any, max_age_sec: float = 600.0) -> bool:
    """Only auto-persist missing Plivo Busy/No-Answer CDRs if the call ended very recently (prevents resurrecting deleted historical rows)."""
    if not end_time:
        return False
    try:
        dt = datetime.fromisoformat(str(end_time).strip().replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        age = (datetime.now(timezone.utc) - dt).total_seconds()
        return -60.0 <= age <= max_age_sec
    except Exception:
        return False


@app.post("/plivo-sync-batch")
async def plivo_sync_batch(
    payload: BatchSyncRequest,
    current_admin: Dict[str, Any] = Depends(require_admin_user),
):
    """
    Dynamically checks telecom call status directly against Plivo API and Personal Loan
    PostgreSQL rows (loan_type='personal_loan' only), preferring exact call_uuid lookups
    so live calls or answered calls are never confused with older historical busy CDRs.
    """
    auth_id = os.getenv("PLIVO_AUTH_ID")
    auth_token = os.getenv("PLIVO_AUTH_TOKEN")
    if not auth_id or not auth_token:
        raise HTTPException(status_code=500, detail="Plivo credentials missing")

    client = plivo.RestClient(auth_id, auth_token)

    # 1. Fetch live + queued active call UUIDs from Plivo
    live_call_uuids = set()
    try:
        live_res = await asyncio.to_thread(client.calls.live_call_list_ids)
        if isinstance(live_res, dict):
            live_call_uuids.update(live_res.get("calls", []) or [])
    except Exception as exc:
        print("Error fetching live call list from Plivo:", exc)

    try:
        queued_res = await asyncio.to_thread(client.calls.queued_call_list_ids)
        if isinstance(queued_res, dict):
            live_call_uuids.update(queued_res.get("calls", []) or [])
    except Exception:
        pass

    requested_uuids = {
        str(u).strip()
        for u in ([*(payload.call_uuids or [])] + list((payload.call_uuid_by_phone or {}).values()))
        if str(u or "").strip()
    }

    # 2. Query recent Personal Loan calls strictly from PostgreSQL (never mix home_loan rows)
    db_calls_by_phone: Dict[str, Dict[str, Any]] = {}
    db_calls_by_sid: Dict[str, Dict[str, Any]] = {}
    p = get_db_pool()
    if p:
        conn = None
        try:
            conn = p.getconn()
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT id, phone_number, call_sid, call_status, call_duration_seconds, loan_purpose, conversation_json, created_at
                    FROM public.ai_agent_calls
                    WHERE LOWER(COALESCE(loan_type, '')) = 'personal_loan'
                    ORDER BY id DESC
                    LIMIT 300;
                """)
                rows = cur.fetchall()

                missing_uuids = [u for u in requested_uuids if all(r[2] != u for r in rows)]
                if missing_uuids:
                    cur.execute("""
                        SELECT id, phone_number, call_sid, call_status, call_duration_seconds, loan_purpose, conversation_json, created_at
                        FROM public.ai_agent_calls
                        WHERE LOWER(COALESCE(loan_type, '')) = 'personal_loan'
                          AND call_sid = ANY(%s)
                        ORDER BY id DESC;
                    """, (missing_uuids,))
                    rows = list(rows) + list(cur.fetchall())

                for row in rows:
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
            print("Error querying recent Personal Loan DB calls:", exc)
        finally:
            if conn and p:
                try:
                    p.putconn(conn)
                except Exception:
                    pass

    # 3. Query recent calls from Plivo (limit=20) indexed by BOTH call_uuid and to_number
    plivo_calls_map: Dict[str, Any] = {}
    plivo_calls_by_uuid: Dict[str, Any] = {}
    try:
        recent_plivo = await asyncio.to_thread(client.calls.list, limit=20)
        c_list = getattr(recent_plivo, "objects", recent_plivo) if not isinstance(recent_plivo, list) else recent_plivo
        for c in c_list:
            c_uid = getattr(c, "call_uuid", None)
            if c_uid and c_uid not in plivo_calls_by_uuid:
                plivo_calls_by_uuid[c_uid] = c
            to_n = normalize_phone_digits(getattr(c, "to_number", ""))
            if to_n and to_n not in plivo_calls_map:
                plivo_calls_map[to_n] = c
    except Exception as exc:
        print("Error fetching recent Plivo calls:", exc)

    uuid_map_req = payload.call_uuid_by_phone or {}
    results = {}

    for idx, raw_phone in enumerate(payload.phone_numbers):
        norm_phone = normalize_phone_digits(raw_phone)
        target_uuid = (
            uuid_map_req.get(raw_phone)
            or uuid_map_req.get(norm_phone)
            or (
                payload.call_uuids[idx]
                if payload.call_uuids
                and len(payload.call_uuids) == len(payload.phone_numbers)
                and idx < len(payload.call_uuids)
                else ""
            )
            or ""
        ).strip()

        if target_uuid:
            # Exact UUID path: never match an older unrelated CDR or unrelated DB row
            if target_uuid in live_call_uuids:
                results[raw_phone] = {
                    "status": "ringing",
                    "category": "ringing",
                    "isLive": True,
                    "callSid": target_uuid,
                    "duration": 0,
                }
                continue

            db_rec = db_calls_by_sid.get(target_uuid)
            c_plivo = plivo_calls_by_uuid.get(target_uuid)
            if not c_plivo:
                try:
                    c_plivo = await asyncio.to_thread(client.calls.get, call_uuid=target_uuid)
                except Exception:
                    c_plivo = None

            uuid = target_uuid
            if not c_plivo and not db_rec:
                # Call was just submitted or CDR not finalized yet — keep ringing instead of pulling older Busy CDR
                results[raw_phone] = {
                    "status": "ringing",
                    "category": "ringing",
                    "isLive": True,
                    "callSid": target_uuid,
                    "duration": 0,
                }
                continue
        else:
            c_plivo = plivo_calls_map.get(norm_phone)
            if not c_plivo and norm_phone:
                try:
                    single_res = await asyncio.to_thread(client.calls.list, to_number=norm_phone, limit=1)
                    s_list = getattr(single_res, "objects", single_res) if not isinstance(single_res, list) else single_res
                    if s_list:
                        c_plivo = s_list[0]
                        plivo_calls_map[norm_phone] = c_plivo
                except Exception:
                    pass

            plivo_uuid = getattr(c_plivo, "call_uuid", "") if c_plivo else ""
            if plivo_uuid:
                db_rec = db_calls_by_sid.get(plivo_uuid)
                uuid = plivo_uuid
            else:
                db_rec = db_calls_by_phone.get(norm_phone)
                uuid = db_rec.get("call_sid", "") if db_rec else ""

            if uuid and uuid in live_call_uuids:
                results[raw_phone] = {
                    "status": "ringing",
                    "category": "ringing",
                    "isLive": True,
                    "callSid": uuid,
                    "duration": 0,
                }
                continue

        if c_plivo:
            state = getattr(c_plivo, "call_state", None) or ""
            cause = getattr(c_plivo, "hangup_cause_name", None) or ""
            dur = int(getattr(c_plivo, "call_duration", 0) or 0)
            source = getattr(c_plivo, "hangup_source", None) or ""
            end_time = getattr(c_plivo, "end_time", None)

            db_dur = int(db_rec.get("duration") or 0) if db_rec else 0
            db_status = (db_rec.get("status") or "").lower() if db_rec else ""

            if state == "ANSWER" or dur > 0 or db_status == "completed" or db_dur > 0:
                cat = "answered"
                if db_rec:
                    _, db_cut_by = _classify_db_rec_outcome(db_rec)
                    cut_by = "user" if (source == "Callee" or db_cut_by == "user") else "ai"
                else:
                    cut_by = "user" if source == "Callee" else "ai"
            elif state in ("RINGING", "IN_PROGRESS", "QUEUED") or (not end_time and not cause):
                cat = "ringing"
                cut_by = None
            elif cause in ("Rejected", "Busy Line", "User Busy") or "busy" in cause.lower() or "reject" in cause.lower():
                cat = "busy"
                cut_by = None
                if not db_rec and uuid and _is_recent_plivo_end(end_time):
                    try:
                        await asyncio.to_thread(
                            save_call_record,
                            call_sid=uuid,
                            phone_number=raw_phone,
                            conversation_history=[{"role": "system", "content": f"Plivo Call Status: BUSY ({cause})"}],
                            call_status="failed",
                            lead_status="follow_up",
                            loan_type="personal_loan",
                            loan_purpose=f"Customer Busy ({cause})",
                            customer_name=None,
                        )
                    except Exception:
                        pass
            else:
                cat = "no_answer"
                cut_by = None
                if not db_rec and uuid and _is_recent_plivo_end(end_time):
                    try:
                        await asyncio.to_thread(
                            save_call_record,
                            call_sid=uuid,
                            phone_number=raw_phone,
                            conversation_history=[{"role": "system", "content": f"Plivo Call Status: NO-ANSWER ({cause})"}],
                            call_status="no_answer",
                            lead_status="follow_up",
                            loan_type="personal_loan",
                            loan_purpose=f"No Answer ({cause})",
                            customer_name=None,
                        )
                    except Exception:
                        pass

            results[raw_phone] = {
                "status": cat,
                "category": cat,
                "cutBy": cut_by,
                "duration": dur if dur > 0 else db_dur,
                "hangupCause": cause,
                "hangupSource": source,
                "callSid": uuid,
                "dbId": db_rec.get("id") if db_rec else None,
            }
        elif db_rec:
            cat, cut_by = _classify_db_rec_outcome(db_rec)
            results[raw_phone] = {
                "status": cat,
                "category": cat,
                "cutBy": cut_by,
                "duration": db_rec.get("duration") or 0,
                "dbId": db_rec.get("id"),
                "callSid": db_rec.get("call_sid") or uuid,
            }
        else:
            results[raw_phone] = {
                "status": "queued",
                "category": "queued",
            }

    return {"success": True, "statuses": results}


@app.api_route("/call-status", methods=["GET", "POST"])
async def get_single_call_status(
    request: Request,
    current_admin: Dict[str, Any] = Depends(require_admin_user),
):
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

    key_phone = phone or call_uuid
    sync_req = BatchSyncRequest(
        phone_numbers=[key_phone] if key_phone else [],
        call_uuids=[call_uuid] if call_uuid else [],
    )
    res = await plivo_sync_batch(sync_req, current_admin=current_admin)
    st = res.get("statuses", {}).get(key_phone, {})
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

    # Outbound call: From = our Plivo number, To = customer.
    # Customer return/inbound call: To = our Plivo number, From = customer.
    plivo_number = normalize_phone_digits(os.getenv("PLIVO_PHONE_NUMBER", ""))
    normalized_to = normalize_phone_digits(to_number)
    normalized_from = normalize_phone_digits(from_number)

    is_customer_return_call = bool(
        plivo_number
        and normalized_to == plivo_number
        and normalized_from
        and normalized_from != plivo_number
    )

    phone = from_number if is_customer_return_call else (to_number or from_number)
    call_meta = {
        "phone_number": phone,
        "customer_name": request.query_params.get("customer_name", ""),
        "started_at": datetime.now(timezone.utc),
        "is_customer_return_call": is_customer_return_call,
    }

    if call_uuid:
        ACTIVE_CALL_METADATA[call_uuid] = call_meta
    if phone:
        ACTIVE_CALL_METADATA[phone] = call_meta
    ACTIVE_CALL_METADATA["latest"] = call_meta

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
        greeting_text = get_personal_loan_opening_text()
        audio_filename = "opening_greeting_en.wav"
        make_sarvam_audio(greeting_text, audio_filename, language="en", audio_dir=AUDIO_DIR)
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
        farewell = ("Thank you for your time. Goodbye." if (state.get("flow") or CallFlow()).language == "en" else "మీ సమయానికి ధన్యవాదాలు అండి. నమస్కారం!")
        try:
            filename = f"{uuid.uuid4().hex}.wav"
            make_sarvam_audio(farewell, filename, language=(state.get("flow") or CallFlow()).language, audio_dir=AUDIO_DIR)
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

    flow = state.get("flow") or CallFlow()
    current_q = state.get("current_question") or question(flow)
    reminder = (("Sorry, I couldn't hear you. " if flow.language == "en" else "క్షమించండి అండి, వినిపించలేదు. ") + current_q)

    try:
        filename = f"{uuid.uuid4().hex}.wav"
        make_sarvam_audio(reminder, filename, language="te", audio_dir=AUDIO_DIR)
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
    return await _http_speech_route(request, "/plivo-process-speech")


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
    is_customer_return_call = False
    call_started_at = datetime.now(timezone.utc)
    stream_ended_normally = False
    saved_to_db = False
    call_done_event = asyncio.Event()
    call_audio_path = None
    ai_speaking_until = 0.0
    is_processing_turn = False
    flow_state = CallFlow(language="en")  # purpose -> permission -> salaried check -> branch questions
    call_language: Optional[str] = "en"
    last_final_transcript = ""
    last_final_transcript_at = 0.0

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
                nonlocal last_final_transcript, last_final_transcript_at
                try:
                    while True:
                        event = await sarvam_socket.recv()
                        event_type = getattr(event, "event", "")

                        if event_type == "transcript.partial":
                            if not is_ai_active():
                                print("Partial:", event.text)

                        elif event_type == "transcript.final":
                            if is_ai_active():
                                # Customer audio arrived while AI was thinking or speaking: ignore
                                continue

                            customer_text = (event.text or "").strip()
                            detected_language = getattr(event, "language", None)
                            if not customer_text or customer_text.strip(" .,!?:;") == "":
                                continue

                            # Sarvam can emit the same final event more than once.
                            # Avoid processing a duplicate utterance twice, especially
                            # when a TTS failure leaves no playback window for
                            # is_ai_active() to suppress it.
                            now = time.monotonic()
                            transcript_key = " ".join(customer_text.casefold().split())
                            if (transcript_key == last_final_transcript
                                    and now - last_final_transcript_at < 3.0):
                                print("[STT] Ignoring duplicate final transcript:", customer_text)
                                continue
                            last_final_transcript = transcript_key
                            last_final_transcript_at = now

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
                                print("[SYSTEM AUDIO] Ignoring recording announcement; keeping current question and language.")
                                is_processing_turn = False
                                continue

                            # 2) Deterministic personal-loan questions: skip the 1500-line prompt.
                            # OpenAI only handles genuinely ambiguous answers/questions.
                            try:
                                if not conversation_history:
                                    conversation_history.append({"role": "assistant", "content": opening(flow_state)})
                                turn = await process_flow_turn(
                                    flow_state, customer_text, call_sid or "unknown",
                                    phone_number, conversation_history, call_started_at,
                                    detected_language,
                                )
                                call_language = flow_state.language
                                tts_language = call_language
                                reply_text = turn.text
                                if not reply_text:
                                    continue
                                print("[FLOW] stage:", flow_state.pending, "language:", call_language)
                                print("AI REPLY:", reply_text)
                                total_duration, total_bytes, _ = await play_chunked_speech(
                                    websocket=websocket,
                                    text_source=reply_text,
                                    language=tts_language,
                                    call_audio_path=call_audio_path,
                                    silence_ms=60,
                                    t_vad_end=time.time(),
                                    call_sid=call_sid,
                                )
                                ai_speaking_until = time.time() + total_duration
                                if turn.close:
                                    print("[FLOW] Ending call:", flow_state.outcome)
                                    stream_ended_normally = True
                                    if not saved_to_db and conversation_history:
                                        try:
                                            record = conversation_history + [{
                                                "role": "system", "content": "CALL_STRUCTURED_DATA=" +
                                                json.dumps(flow_state.snapshot(), ensure_ascii=False)
                                            }]
                                            record_id = await asyncio.to_thread(
                                                save_call_record,
                                                call_sid=call_sid,
                                                phone_number=phone_number,
                                                conversation_history=record,
                                                call_status="completed",
                                                lead_status=turn.lead_status or (
                                                    "follow_up" if flow_state.callback_requested or
                                                    flow_state.outcome == "callback_requested" else
                                                    "not_interested" if flow_state.outcome in
                                                    ("do_not_call", "declined", "wrong_person") else "interested"
                                                ),
                                                call_started_at=call_started_at,
                                                call_ended_at=datetime.now(timezone.utc),
                                                loan_purpose=flow_state.answers.get("purpose", flow_state.outcome),
                                                loan_type="personal_loan", customer_name=flow_state.customer_name or None,
                                            )
                                            saved_to_db = record_id is not None
                                        except Exception as db_exc:
                                            print("Error saving call record:", repr(db_exc))
                                    # Wait until audio has played before cutting telecom line.
                                    await asyncio.sleep(max(0.4, total_duration + 0.5))
                                    call_done_event.set()
                                    try:
                                        await websocket.close()
                                    except Exception:
                                        pass
                                    if call_sid:
                                        await asyncio.to_thread(hangup_plivo_call, call_sid)
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

            opening_already_spoken = False
            listener_task = asyncio.create_task(receive_sarvam_results())

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

                        )
                        if meta:
                            phone_number = meta.get("phone_number")
                            is_customer_return_call = bool(meta.get("is_customer_return_call", False))
                            flow_state.customer_name = meta.get("customer_name", "") or ""
                            if "started_at" in meta:
                                call_started_at = meta["started_at"]

                    if not call_sid:
                        call_sid = f"call_{uuid.uuid4().hex[:12]}"

                    call_audio_path = ALL_CALL_RECORDINGS_DIR / f"call_{call_sid}.ulaw"

                    try:
                        opening_text, opening_len, opening_duration = await speak_opening_message(
                            websocket,
                            call_audio_path=call_audio_path,
                            call_sid=call_sid,
                            language="en",
                            customer_name=flow_state.customer_name,
                        )
                        ai_speaking_until = time.time() + opening_duration
                        if not conversation_history:
                            conversation_history.append({
                                "role": "assistant",
                                "content": opening_text,
                            })
                        opening_already_spoken = True
                    except Exception as exc:
                        print("Opening speech failed:", repr(exc))

                elif event_type == "media":
                    media = message.get("media", {})
                    if media.get("track") == "inbound" and media.get("payload"):
                        audio_packet_count += 1
                        if audio_packet_count == 1:
                            print("First inbound Plivo audio packet received.")

                        if not is_ai_active():
                            await sarvam_socket.send_realtime_audio_input(
                                RealtimeAudioInput(audio=media["payload"])
                            )

                elif event_type == "stop":
                    print("Plivo stream stopped.")
                    cancel_call_tts(call_sid)
                    await clear_plivo_audio_queue(websocket)
                    break

    except WebSocketDisconnect:
        print("Plivo WebSocket disconnected.")
    except Exception as exc:
        print("Streaming error:", repr(exc))
    finally:
        call_done_event.set()
        cancel_call_tts(call_sid)
        _release_outbound_guard(phone_number=phone_number or "", call_uuid=call_sid or "")
        try:
            await clear_plivo_audio_queue(websocket)
        except Exception:
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
                record = conversation_history + [{"role": "system", "content":
                      "CALL_STRUCTURED_DATA=" + json.dumps(flow_state.snapshot(), ensure_ascii=False)}]
                await asyncio.to_thread(
                    save_call_record,
                    call_sid=call_sid,
                    phone_number=phone_number,
                    conversation_history=record,
                    call_status=saved_status,
                    call_started_at=call_started_at,
                    call_ended_at=call_ended_at,
                    loan_purpose=outcome_desc,
                    loan_type="personal_loan",
                    customer_name=flow_state.customer_name or None,
                )
            except Exception as db_exc:
                print("Error saving call conversation to database in finally:", repr(db_exc))


@app.get("/calls")
def get_recorded_calls(
    loan_type: Optional[str] = "personal_loan",
    current_admin: Dict[str, Any] = Depends(require_admin_user),
):
    """Retrieve recorded calls from PostgreSQL public.ai_agent_calls table."""
    return fetch_all_calls(loan_type=loan_type)


class DeleteCallsBatchRequest(BaseModel):
    call_ids: List[int] = []
    delete_test_only: bool = False


@app.post("/calls/delete-batch")
def remove_personal_loan_calls_batch(
    payload: DeleteCallsBatchRequest,
    current_admin: Dict[str, Any] = Depends(require_admin_user),
):
    """Batch-delete Personal Loan DB rows strictly restricted to loan_type='personal_loan'."""
    result = delete_personal_loan_calls_batch(
        call_ids=payload.call_ids,
        delete_test_only=payload.delete_test_only,
        all_call_recordings_dir=ALL_CALL_RECORDINGS_DIR,
    )
    if not result.get("success"):
        status_code = 400 if result.get("bad_request") else 500
        raise HTTPException(status_code=status_code, detail=result.get("error", "Batch delete failed"))
    return result


@app.delete("/calls/{call_id}")
def remove_personal_loan_call(
    call_id: int,
    current_admin: Dict[str, Any] = Depends(require_admin_user),
):
    """Delete a single Personal Loan DB row strictly restricted to loan_type='personal_loan'."""
    result = delete_personal_loan_call(call_id, ALL_CALL_RECORDINGS_DIR)
    if not result.get("success"):
        if result.get("not_found"):
            raise HTTPException(status_code=404, detail=result.get("error", "Call not found"))
        if result.get("forbidden"):
            raise HTTPException(status_code=403, detail=result.get("error", "Forbidden"))
        raise HTTPException(status_code=500, detail=result.get("error", "Delete failed"))
    return result


@app.get("/recording/{call_id}")
def stream_call_recording(
    call_id: int,
    current_admin: Dict[str, Any] = Depends(require_admin_user),
):
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
