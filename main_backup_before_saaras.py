import os
import base64
import uuid
from pathlib import Path
from datetime import datetime
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel
from groq import Groq
from sarvamai import SarvamAI
import plivo

load_dotenv(Path(__file__).resolve().parent / ".env")

app = FastAPI(title="AI Voice Agent API")

GROQ_MODEL = "openai/gpt-oss-20b"

AUDIO_DIR = Path("generated_audio")
AUDIO_DIR.mkdir(exist_ok=True)

# Demo-only conversation memory. It resets when the server restarts.
CALL_STATE = {}


SYSTEM_PROMPT = """
You are a friendly, professional AI voice customer-support assistant.

LANGUAGE AND TONE
- Speak in clear, natural English for this demo.
- Keep every spoken reply short: one or two sentences maximum.
- Be warm, patient, calm, and respectful.
- Never sound annoyed or blame the caller for recognition errors.

LISTENING AND SILENCE
- If the caller's speech is empty or unclear, politely say:
  "I'm sorry, I didn't catch that. Could you please repeat it?"
- Do not repeatedly say "I didn't hear anything" if the caller has already spoken.
- If the caller asks whether you are listening, reassure them:
  "Yes, I'm here and listening. I'm sorry for the delay. How can I help?"
- Never pretend you heard words that were not recognized.

UNDERSTANDING THE REQUEST
- Answer the caller's actual question directly.
- If a word could have multiple meanings, ask a short clarification.
- For example, if you are unsure whether the caller said "loan" or
  "launch", ask: "Did you mean a loan, or a product launch?"
- Never assume the caller is asking about a product launch when they
  might be asking about a loan.
- For loan questions, ask what information they need. Do not invent
  interest rates, eligibility rules, or financial terms.

CONVERSATION FLOW
- Ask only one question at a time.
- Remember the previous turns in the current call.
- Do not repeat the initial greeting on every turn.
- If the caller says they have no further questions or want to leave,
  ask: "Would you like me to end the call now? Please say yes or no."
- End only after a clear confirmation.
- If the confirmation is unclear, politely ask again.
- If the caller says no, continue helping them.

IMPORTANT
- You are a customer-support assistant, not a generic chatbot.
- Never claim to have completed an action you did not perform.
- Keep replies concise and easy to understand over a telephone connection.
"""





LANGUAGES = {
    "English": "en-IN",
    "Hindi": "hi-IN",
    "Telugu": "te-IN",
}

LANGUAGE_PROMPT = """
You are Shailu, a professional personal-loan voice assistant.

Supported languages: English, Hindi, and Telugu.

Follow these rules:
- Identify the language the customer is speaking.
- Reply in that language without asking permission to switch.
- Remember the selected language throughout the call.
- Keep responses natural, polite, and short.
- Ask one question at a time.
- Never invent loan eligibility, approval, or financial terms.
"""


class CallRequest(BaseModel):
    to_number: str


def get_base_url():
    return os.getenv("PUBLIC_BASE_URL", "").rstrip("/")



# Bulbul V4 Flash voice configuration
SARVAM_MODEL = "bulbul:v4-flash"
SARVAM_PACE = 1.25  # Faster than the default 1.0; test 1.1–1.4

LANGUAGES = {
    "en": {
        "code": "en-IN",
        "speaker": "simran_en_customer",
    },
    "hi": {
        "code": "hi-IN",
        "speaker": "ritu_hi_customer",
    },
    "te": {
        "code": "te-IN",
        # Verify the Telugu female persona ID in Sarvam's voice catalog.
        "speaker": os.getenv("SARVAM_TELUGU_SPEAKER", "priya_te_customer"),
    },
}

def make_sarvam_audio(text, filename, language="en"):
    api_key = os.getenv("SARVAM_API_KEY")
    if not api_key:
        raise RuntimeError("SARVAM_API_KEY is missing")

    language = language if language in LANGUAGES else "en"
    voice = LANGUAGES[language]

    client = SarvamAI(api_subscription_key=api_key)

    result = client.text_to_speech.convert(
        text=text,
        model=SARVAM_MODEL,
        language_code=voice["code"],
        speaker=voice["speaker"],
        pace=SARVAM_PACE,
    )

    if not result.audios:
        raise RuntimeError("Sarvam returned no audio")

    audio_bytes = base64.b64decode(result.audios[0])
    audio_path = AUDIO_DIR / filename
    audio_path.write_bytes(audio_bytes)

    return filename




def detect_end_request(text):
    text = text.lower().strip()

    phrases = (
        "no need",
        "nothing else",
        "that's all",
        "that is all",
        "goodbye",
        "end the call",
        "hang up",
        "i am done",
        "i'm done",
        "no more questions",
    )

    return any(phrase in text for phrase in phrases)


def detect_yes(text):
    text = text.lower().strip()
    return text in {
        "yes", "yes please", "yeah", "yep", "correct",
        "please do", "end the call", "hang up", "goodbye",
    }


def detect_no(text):
    text = text.lower().strip()
    return text in {
        "no", "no thanks", "not yet", "keep going",
        "continue", "i have a question", "don't end",
    }


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
        raise HTTPException(
            status_code=500,
            detail="PUBLIC_BASE_URL is missing",
        )

    groq_key = os.getenv("GROQ_API_KEY")
    if not groq_key:
        raise HTTPException(
            status_code=500,
            detail="GROQ_API_KEY is missing",
        )

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
            "yes",
            "yes please",
            "yes end the call",
            "yes, end the call",
            "please end the call",
            "end the call",
            "goodbye",
            "bye",
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
        reply_text = (
            "I'm sorry, I didn't catch that. "
            "Could you please repeat it?"
        )

    elif detect_end_request(customer_text):
        state["confirm_end"] = True
        reply_text = (
            "Would you like me to end the call now? Please say yes or no."
        )

    else:
        try:
            groq_client = Groq(api_key=groq_key)

            state["messages"].append({
                "role": "user",
                "content": customer_text,
            })

            # Keep recent turns to maintain conversational context.
            state["messages"] = state["messages"][-12:]

            result = groq_client.chat.completions.create(
                model=GROQ_MODEL,
                messages=[
                    {"role": "system", "content": SYSTEM_PROMPT},
                    *state["messages"],
                ],
                reasoning_effort="low",
                max_completion_tokens=1000,
            )

            reply_text = (
                result.choices[0].message.content
                or "I'm sorry. Could you please repeat that?"
            ).strip()

            state["messages"].append({
                "role": "assistant",
                "content": reply_text,
            })

            state["messages"] = state["messages"][-12:]

        except Exception:
            print("Groq response generation failed")
            reply_text = (
                "I'm sorry, I'm having trouble answering right now. "
                "Could you please try again?"
            )

    try:
        filename = f"{uuid.uuid4().hex}.wav"
        make_sarvam_audio(reply_text, filename)
    except Exception:
        print("Sarvam audio generation failed")
        fallback_xml = f"""
        <Response>
            <Say language="en-US" voice="woman">
                I'm sorry, the voice service is temporarily unavailable.
            </Say>
            <Redirect method="POST">{base_url}/listen</Redirect>
        </Response>
        """
        return Response(
            content=fallback_xml,
            media_type="application/xml",
        )

    if should_hang_up:
        next_step = "<Hangup/>"
    else:
        next_step = f'<Redirect method="POST">{base_url}/listen</Redirect>'

    twiml = f"""
    <Response>
        <Play>{base_url}/reply/{filename}</Play>
        {next_step}
    </Response>
    """

    return Response(content=twiml, media_type="application/xml")




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
            answer_url=f"{base_url}/plivo-answer",
            answer_method="POST",
        )

        return {
            "message": "Plivo call request submitted",
            "details": str(response),
        }

    except Exception as exc:
        import traceback

        print("Plivo exception type:", type(exc).__name__)
        print("Plivo exception message:", str(exc))

        response = getattr(exc, "response", None)
        if response is not None:
            print("Plivo HTTP status:", response.status_code)
            print("Plivo response body:", response.text)

        traceback.print_exc()

        raise HTTPException(
            status_code=502,
            detail=f"Plivo error: {type(exc).__name__}: {str(exc)}",
        ) from exc




# Kept from your existing project. Twilio trial calls in this demo
# use Gather rather than this Media Streams endpoint.




from datetime import datetime
from zoneinfo import ZoneInfo


@app.api_route("/plivo-answer", methods=["GET", "POST"])
async def plivo_answer(request: Request):
    import traceback
    import xml.sax.saxutils as saxutils

    try:
        base_url = get_base_url()
        print("DEBUG base_url:", repr(base_url))

        call_sid = (
            request.query_params.get("CallUUID")
            or request.query_params.get("call_uuid")
            or "unknown"
        )
        if call_sid == "unknown":
            try:
                form = dict(await request.form())
                call_sid = form.get("CallUUID") or form.get("call_uuid") or "unknown"
            except Exception:
                pass

        current_hour = datetime.now(ZoneInfo("Asia/Kolkata")).hour

        if 5 <= current_hour < 12:
            greeting = "Good morning"
        elif 12 <= current_hour < 17:
            greeting = "Good afternoon"
        elif 17 <= current_hour < 21:
            greeting = "Good evening"
        else:
            greeting = "Hello"

        opening = (
            f"{greeting}! My name is Shailu. "
            "We offer personal loans with interest rates starting from "
            "9.99 percent. Are you looking for a personal loan?"
        )

        state = CALL_STATE.setdefault(call_sid, {})
        state["current_question"] = opening
        state["stage"] = "interest"
        state["silence_count"] = 0

        filename = f"{uuid.uuid4().hex}.wav"
        make_sarvam_audio(opening, filename, language="en")
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

        return Response(content=xml, media_type="application/xml")

    except Exception:
        traceback.print_exc()
        safe_opening = saxutils.escape(
            "Hello! My name is Shailu. Are you looking for a personal loan?"
        )
        xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <GetInput inputType="speech"
              action="{base_url}/plivo-process-speech"
              method="POST"
              executionTimeout="30">
        <Speak voice="WOMAN">{safe_opening}</Speak>
    </GetInput>
    <Redirect method="POST">{base_url}/plivo-timeout</Redirect>
</Response>"""
        return Response(content=xml, media_type="application/xml")


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
            make_sarvam_audio(farewell, filename, language="en")
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
        make_sarvam_audio(reminder, filename, language="en")
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
    import re
    import xml.sax.saxutils as saxutils

    base_url = get_base_url()
    if not base_url:
        raise HTTPException(
            status_code=500,
            detail="PUBLIC_BASE_URL is missing",
        )

    form_data = dict(await request.form())

    customer_text = (
        form_data.get("Speech")
        or form_data.get("speech")
        or form_data.get("text")
        or ""
    ).strip()

    print("DEBUG Plivo speech transcript:", repr(customer_text))
    print("DEBUG Plivo callback fields:", list(form_data.keys()))

    call_sid = (
        form_data.get("CallUUID")
        or form_data.get("call_uuid")
        or form_data.get("CallUUID".lower())
        or "unknown"
    )

    state = CALL_STATE.setdefault(call_sid, {})
    state.setdefault("answers", {})
    state.setdefault("loans", [])
    state.setdefault("cards", [])
    state.setdefault("stage", "interest")
    state.setdefault("confirm_end", False)
    state["silence_count"] = 0

    def is_no(text):
        return bool(re.search(
            r"\b(no|nope|not interested|don't|do not|never|no thanks|not now)\b",
            text.lower()
        ))

    def is_yes(text):
        lower = text.lower()
        if is_no(lower):
            return False
        return bool(re.search(
            r"\b(yes|yeah|yep|correct|sure|okay|ok|interested)\b",
            lower
        ))

    def ask(stage, question):
        state["stage"] = stage
        return question

    def finish():
        state["stage"] = "finished"
        return "Thank you for your time. Our team can guide you on the next steps. Have a nice day."

    text = customer_text
    stage = state["stage"]
    answers = state["answers"]
    reply_text = ""
    should_hang_up = False

    if not text:
        reply_text = (
            "Sorry, I didn't catch that. "
            "Could you please repeat your answer?"
        )

    elif state["confirm_end"]:
        if is_yes(text):
            reply_text = "Thank you for your time. Have a nice day."
            should_hang_up = True
            state["confirm_end"] = False
        elif is_no(text):
            state["confirm_end"] = False
            reply_text = (
                "Of course. Let's continue. "
                + state.get("current_question", "How can I help you?")
            )
        else:
            reply_text = (
                "Would you like me to end the call now? Please say yes or no."
            )

    elif detect_end_request(text):
        state["confirm_end"] = True
        reply_text = (
            "Would you like me to end the call now? Please say yes or no."
        )

    elif stage == "interest":
        if is_yes(text):
            reply_text = ask(
                "employment",
                "Are you currently doing a job or running a business?"
            )
        elif is_no(text):
            reply_text = "Thank you for your time. Have a nice day."
            should_hang_up = True
        else:
            reply_text = (
                "Are you looking for a personal loan? Please say yes or no."
            )

    elif stage == "employment":
        lower = text.lower()

        if any(word in lower for word in (
            "job", "employed", "working", "salary", "employee"
        )):
            answers["employment_type"] = "job"
            reply_text = ask(
                "organization",
                "Which organization are you working for?"
            )
        elif any(word in lower for word in (
            "business", "self employed", "self-employed", "own business",
            "shop owner", "entrepreneur"
        )):
            answers["employment_type"] = "business"
            reply_text = (
                "Thank you. We'll handle the business details separately. "
                "Our team can guide you further. Have a nice day."
            )
            should_hang_up = True
        else:
            reply_text = (
                "Are you doing a job or running a business?"
            )

    elif stage == "organization":
        answers["organization"] = text
        reply_text = ask(
            "salary",
            "What is your monthly take-home salary?"
        )

    elif stage == "salary":
        answers["monthly_take_home_salary"] = text
        reply_text = ask(
            "salary_bank",
            "Which bank account receives your salary?"
        )

    elif stage == "salary_bank":
        answers["salary_bank"] = text
        reply_text = ask(
            "existing_loans",
            "Do you have any existing loans? Please say yes or no."
        )

    elif stage == "existing_loans":
        if is_yes(text):
            reply_text = ask(
                "loan_type",
                "What type of loan do you have, for example, "
                "a home loan, personal loan, or business loan?"
            )
        elif is_no(text):
            reply_text = ask(
                "credit_cards",
                "Do you have any credit cards?"
            )
        else:
            reply_text = (
                "Do you have any existing loans? Please say yes or no."
            )

    elif stage == "loan_type":
        state["pending_loan"] = {"type": text}
        reply_text = ask(
            "loan_amount",
            "What is the outstanding amount of this loan?"
        )

    elif stage == "loan_amount":
        state["pending_loan"]["amount"] = text
        reply_text = ask(
            "loan_emi",
            "What is the monthly EMI for this loan?"
        )

    elif stage == "loan_emi":
        state["pending_loan"]["emi"] = text
        reply_text = ask(
            "loan_tenure",
            "What is the remaining tenure of this loan?"
        )

    elif stage == "loan_tenure":
        state["pending_loan"]["tenure"] = text
        state["loans"].append(state.pop("pending_loan"))

        reply_text = ask(
            "another_loan",
            "Do you have any other existing loans?"
        )

    elif stage == "another_loan":
        if is_yes(text):
            reply_text = ask(
                "loan_type",
                "What type of loan is it?"
            )
        elif is_no(text):
            reply_text = ask(
                "credit_cards",
                "Do you have any credit cards?"
            )
        else:
            reply_text = (
                "Do you have another existing loan? Please say yes or no."
            )

    elif stage == "credit_cards":
        if is_yes(text):
            reply_text = ask(
                "card_bank",
                "Which bank issued your credit card?"
            )
        elif is_no(text):
            reply_text = finish()
            should_hang_up = True
        else:
            reply_text = (
                "Do you have any credit cards? Please say yes or no."
            )

    elif stage == "card_bank":
        state["pending_card"] = {"bank": text}
        reply_text = ask(
            "card_limit",
            "What is the credit limit on this card?"
        )

    elif stage == "card_limit":
        state["pending_card"]["limit"] = text
        reply_text = ask(
            "card_outstanding",
            "What is the outstanding amount on this card?"
        )

    elif stage == "card_outstanding":
        state["pending_card"]["outstanding"] = text
        state["cards"].append(state.pop("pending_card"))

        reply_text = ask(
            "another_card",
            "Do you have another credit card?"
        )

    elif stage == "another_card":
        if is_yes(text):
            reply_text = ask(
                "card_bank",
                "Which bank issued your next credit card?"
            )
        elif is_no(text):
            reply_text = finish()
            should_hang_up = True
        else:
            reply_text = (
                "Do you have another credit card? Please say yes or no."
            )

    elif stage == "finished":
        reply_text = "Thank you for your time. Have a nice day."
        should_hang_up = True

    else:
        reply_text = (
            "I'm sorry. Let's continue. "
            "Are you looking for a personal loan?"
        )
        state["stage"] = "interest"

    state["current_question"] = reply_text

    # Generate speech using your existing Sarvam helper.
    try:
        filename = f"{uuid.uuid4().hex}.wav"
        make_sarvam_audio(reply_text,filename,language=state.get("language", "en"),)

    except Exception:
        print("Sarvam audio generation failed")
        fallback = saxutils.escape(
            "Sorry, the voice service is temporarily unavailable."
        )
        xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Speak voice="WOMAN">{fallback}</Speak>
    <Hangup/>
</Response>"""
        return Response(content=xml, media_type="application/xml")

    audio_url = saxutils.escape(f"{base_url}/reply/{filename}")

    if should_hang_up:
        xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Play>{audio_url}</Play>
    <Hangup/>
</Response>"""
    else:
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



from fastapi import UploadFile, File

@app.post("/test-language-detection")
async def test_language_detection(file: UploadFile = File(...)):
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
            language_code="unknown",
            mode="transcribe",
        )
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    detected_code = getattr(result, "language_code", None)

    return {
        "transcript": result.transcript,
        "detected_language": detected_code,
        "language_probability": getattr(result, "language_probability", None),
    }
