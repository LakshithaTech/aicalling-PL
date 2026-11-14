# components/questionnaire.py

import re
import time
from pathlib import Path
from typing import Optional, Tuple
from datetime import datetime
from zoneinfo import ZoneInfo

from fastapi import WebSocket




def is_unemployed_response(text: str) -> bool:
    """
    Detect if customer states they have no job, no business, are idle or unemployed.
    For the salaried-only personal-loan offer, non-salaried answers must be
    handled as ineligible by personal_loan_flow.employment_value().
    """
    t = (text or "").lower().strip()
    phrases = (
        "ఖాళీ", "ఖాళీగా", "ఏం చేయట్లేదు", "ఏం చేయట్లే", "ఏమీ చేయట్లేదు",
        "ఏమీ చెయ్యట్లేదు", "ఏం చెయ్యట్లే", "ఏం చేయడం లేదు",
        "చేయట్లేదు", "చెయ్యట్లేదు", "చేయట్లే", "చెయ్యట్లే",
        "జాబ్ లేదు", "జాబ్ చేయట్లే", "జాబ్ చెయ్యట్లే", "జాబ్ చేయలేదు",
        "బిజినెస్ లేదు", "బిజినెస్ చేయట్లే", "బిజినెస్ చెయ్యట్లే",
        "పని లేదు", "పని చేయట్లేదు", "ఉద్యోగం లేదు",
        "స్టూడెంట్", "హౌస్‌వైఫ్", "చదువుకుంటున్నా",
        "jobless", "unemployed", "no job", "not working", "no work",
        "no business", "without job", "student", "housewife",
        "job ledu", "job ledhu", "job leydhu", "job cheyaledu",
        "job cheyatledu", "job cheyyatledu", "kaliga", "khaliga",
        "em cheyatle", "em cheyatledu", "business ledu",
        "bussiness ledu", "pani ledu", "udyogam ledu",
    )
    return any(p in t for p in phrases)


def detect_end_request(text: str) -> bool:
    """Detect explicit call-ending requests. Contextual 'no' is handled by the prompt."""
    t = (text or "").lower().strip()
    phrases = (
        "nothing else",
        "that's all",
        "that is all",
        "goodbye",
        "bye bye",
        "bye",
        "end the call",
        "hang up",
        "please hang up",
        "cut the call",
        "stop the call",
        "i am done",
        "i'm done",
        "no more questions",
        "call end cheyandi",
        "call cut cheyandi",
        "call end చేయండి",
        "కాల్ ఎండ్ చేయండి",
        "కాల్ పెట్టేయండి",
        "కాల్ కట్ చేయండి",
        "ఫోన్ పెట్టేయండి",
        "ఇక మాట్లాడను",
        "మాట్లాడటం వద్దు",
        "ఇక చాలు",
        "చాలు",
        "అంతే",
    )
    return any(phrase in t for phrase in phrases)


def extract_single_question(text: str) -> str:
    """Enforce that only ONE single customer question is asked per turn."""
    cleaned = (text or "").strip()
    if cleaned.count("?") > 1:
        parts = cleaned.split("?", 1)
        return parts[0].strip() + "?"
    return cleaned


def is_closing_reply(text: str) -> bool:
    """
    Detect replies that should be followed by call termination.
    A reply ending in '?' is never considered closing.
    """
    lower = (text or "").lower().strip()

    if not lower:
        return False

    if lower.endswith("?"):
        return False

    # Avoid treating eligibility/callback questions as closings.
    non_closing_markers = (
        "free",
        "ఫ్రీగా",
        "yes చెప్పండి",
        "would you like",
        "do you need",
        "are you interested",
        "comfortable continuing",
        "eligibility",
        "ఎలిజిబిలిటీ",
        "ఏ టైమ్",
        "ఏ రోజు",
        "callback arrange",
        "continue చేద్దామా",
        "మాట్లాడటం ఓకేనా",
    )
    if any(marker.lower() in lower for marker in non_closing_markers):
        return False

    closing_phrases = (
        "have a great day",
        "have a nice day",
        "have a wonderful day",
        "have a good day",
        "thank you for your time",
        "thanks for your time",
        "ధన్యవాదాలు",
        "థాంక్యూ",
        "నైస్ డే",
        "హావ్ ఎ నైస్ డే",
        "హావ్ ఎ గ్రేట్ డే",
        "గ్రేట్ డే",
        "విలువైన సమయం",
        "సారీ ఫర్ ది డిస్టర్బెన్స్",
        "మీ request noted",
        "ఈ call ఇక్కడ end చేస్తున్నాను",
        "ఈ కాల్ ఇక్కడ ఎండ్ చేస్తున్నాను",
        "ఆ టైమ్‌కి మా team",
        "ఆ టైమ్‌కి మా personal loan టీమ్",
        "ఆ టైమ్‌కి మా టీమ్",
        "త్వరలోనే కాంటాక్ట్ చేస్తుంది",
        "త్వరలోనే సంప్రదిస్తుంది",
        "కాంటాక్ట్ చేస్తారు",
    )
    return any(phrase in lower for phrase in closing_phrases)


def detect_yes(text: str) -> bool:
    clean = re.sub(r'[^\w\s\u0C00-\u0C7F\u0900-\u097F]', ' ', (text or '').lower()).strip()
    t = ' '.join(clean.split())
    yes_tokens = {
        "yes", "yes please", "yeah", "yep", "correct", "okay", "ok",
        "sure", "continue", "please continue", "how much",
        "tell me", "ah tell me", "ha tell me", "aa tell me", "yes tell me",
        "avunu", "avvunu", "avnu", "aunu", "kavali", "kavale", "ha", "haa", "aa",
        "kavalandi", "kavale andi", "ha andi", "avunandi", "cheppandi", "chappade", "cheppade",
        "matladandi", "maatladandi",
        "అవును", "ఓకే", "సరే", "కావాలి", "హా", "చెప్పండి", "కావాలండి",
        "ఆ", "అవునండి", "మాట్లాడండి", "వీలవుతుంది", "వీలవుతుందండి",
    }
    if t in yes_tokens:
        return True
    return any(tok in t for tok in {"kavali", "kavale", "avunu", "avvunu", "chappade", "cheppade", "tell me", "కావాలి", "అవును", "how much", "చెప్పండి", "మాట్లాడండి", "వీలవుతుంది"})


def detect_no(text: str) -> bool:
    clean = (text or "").lower().replace("’", "'")
    clean = re.sub(r"[^\w\s\u0C00-\u0C7F\u0900-\u097F']", " ", clean).strip()
    t = " ".join(clean.split())
    if not t:
        return False
    exact_no = {
        "no", "no thanks", "no thank you", "no please", "nope", "nah",
        "not interested", "don't need", "dont need", "do not need",
        "don't want", "dont want", "do not want",
        "not required", "no need", "no loan", "no loans", "not now",
        "not interested in personal loan", "no personal loan",
        "లేదు", "వద్దు", "అవసరం లేదు", "వద్దండి", "నాకు వద్దు", "వద్దు అండి", "లేదండి", "ఏం లేదు", "వద్దులే",
        "ledu", "ledhu", "vaddu", "vaddandi", "avasaram ledu", "oddhu", "odhu",
        "nahi", "nahi chahiye", "nahin", "nahi chaiye", "mat karo",
    }
    if t in exact_no:
        return True
    words = t.split()
    if words and words[0].strip("'") in {"no", "nope", "nah", "వద్దు", "లేదు", "vaddu", "ledu", "ledhu", "nahi"}:
        return True
    negative_phrases = (
        "not interested", "don't need", "dont need", "do not need",
        "don't want", "dont want", "do not want", "not required",
        "no need", "no loan", "no loans", "stop calling",
        "అవసరం లేదు", "వద్దు", "లేదు", "వద్దండి", "నాకు వద్దు",
        "avasaram ledu", "vaddu", "nahi chahiye",
    )
    return any(p in t for p in negative_phrases)


# ------------------------------------------------------------------
# LANGUAGE / SYSTEM-AUDIO HELPERS
# ------------------------------------------------------------------

def is_recording_announcement(text: str) -> bool:
    """
    Detect telecom/device auto-recording announcements.
    These must NOT be treated as the customer's first real response.
    """
    t = (text or "").lower().strip()
    phrases = (
        "your call is recording",
        "your call is being recorded",
        "this call is recording",
        "this call is being recorded",
        "this call may be recorded",
        "call is being recorded",
        "call may be recorded",
        "ఈ కాల్ రికార్డ్ అవుతోంది",
        "ఈ కాల్ రికార్డ్ చేయబడుతోంది",
        "మీ కాల్ రికార్డ్ అవుతోంది",
    )
    return any(p in t for p in phrases)


def detect_explicit_language_switch(text: str) -> Optional[str]:
    """
    Return 'te' or 'en' ONLY when the customer clearly asks to switch language.
    Normal bank/property/loan English words do not trigger a switch.
    """
    t = (text or "").lower().strip()

    english_requests = (
        "can you talk in english",
        "can you speak in english",
        "can we speak in english",
        "please speak in english",
        "speak in english",
        "talk in english",
        "english please",
        "english only",
        "continue in english",
        "speak english",
        "change language to english",
        "switch to english",
        "change to english",
        "in english",
        "ఇంగ్లీష్ లో మాట్లాడండి",
        "ఇంగ్లీషులో మాట్లాడండి",
        "ఇంగ్లీష్ లో చెప్పండి",
        "english lo matladandi",
        "english lo cheppandi",
        "english lo maatladandi",
    )

    telugu_requests = (
        "can you talk in telugu",
        "can you speak in telugu",
        "can we speak in telugu",
        "please speak in telugu",
        "speak in telugu",
        "talk in telugu",
        "speak telugu",
        "telugu please",
        "telugu lo matlad",
        "telugu lo maatlad",
        "meeru telugu lo matlad",
        "miru telugu lo matlad",
        "nenu telugu lo matladacha",
        "nenu telugu lo maatladacha",
        # Customers often request Telugu after the call has already started
        # in English, using either "in Telugu" or a Telugu-script question.
        "in telugu",
        "continue in telugu",
        "continue with you in telugu",
        "మీరు తెలుగులో మాట్లాడతారా",
        "మీరు తెలుగు లో మాట్లాడతారా",
        "నేను తెలుగులో మాట్లాడచ్చా",
        "తెలుగులో మాట్లాడదామా",
        "తెలుగు లో మాట్లాడదామా",
        "తెలుగులో మాట్లాడాలి",
        "తెలుగులో మాట్లాడండి",
        "తెలుగు లో మాట్లాడండి",
        "తెలుగు మాట్లాడండి",
        "తెలుగులో చెప్పండి",
        "తెలుగు లో చెప్పండి",
    )

    if any(p in t for p in english_requests):
        return "en"
    if any(p in t for p in telugu_requests):
        return "te"
    return None


def is_telugu_speech(text: str, detected_language: Optional[str] = None) -> bool:
    """
    Check if customer is speaking Telugu.
    Used for automatic English -> Telugu switching (e to t without asking).
    """
    t = (text or "").strip()
    if not t:
        return False

    low = t.lower().rstrip(".!?,")

    # Neutral greetings do not count as Telugu
    if low in {"hello", "hello hello", "hi", "hey", "hello rada", "hello radha", "హలో", "హాలో"}:
        return False

    # 1. Any Telugu Unicode characters (beyond neutral greeting)
    if re_search_telugu(t):
        return True

    # 2. STT detected language is Telugu
    det = (detected_language or "").lower().strip()
    if det.startswith("te"):
        return True

    # 3. Whole-word Roman-Telugu spoken words
    words = set(re.findall(r'[a-zA-Z]+', low))
    single_word_markers = {
        # Affirmative / Permission / Inquiry responses
        "avunu", "avvunu", "aunu", "avnu", "avunandi", "avnunandi", "avvunandi",
        "kavali", "kavale", "kavalandi", "kavalamma", "kavale andi", "kavali andi", "kaavali", "kaavale",
        "cheppandi", "cheppamma", "cheppu", "chappade", "cheppade", "cheppadandi",
        "matladandi", "maatladandi",
        "sare", "sarey", "sare andi", "alage", "alage andi", "alaage",

        # Pronouns & questioning
        "meeru", "miru", "nenu", "maku", "maaku", "manaki",
        "enti", "entha", "yenti", "emiti", "vaddi", "unnara", "unnanu", "chudandi", "choodandi",
        "ala", "aithe", "ayithe", "telugu", "namaskaram",
        "leka", "unna", "adagandi", "evandi", "evaru", "evvaru", "ekkada",
        "ekkadnunchi", "ekkadi", "chala", "chaala",

        # Negative
        "ledu", "ledhu", "vaddu", "vaddamma", "vaddandi", "vadhu", "ledandi", "oddhu", "odhu",

        # Address
        "andi", "babu", "amma", "garu",
    }
    if words & single_word_markers:
        return True

    for w in words:
        if len(w) > 4 and w.endswith("andi") and w not in {"brandi"}:
            return True

    # Colloquial phrases (including transliterated "tell me", "ah tell me")
    multi_word_markers = (
        "emi kavali", "bank nunchi", "ye bank", "e bank", "em bank",
        "ha andi", "ha cheppandi", "loan kavali", "personal loan kavali", "avunu andi", "kavali andi",
        "tell me", "ah tell me", "ha tell me", "aa tell me", "yes tell me",
        "tell me andi", "tell me madam", "tell me sir", "tell me please",
        "cheppandi madam", "cheppandi sir", "job chestunna", "business chestunna",
        "cheppandi andi", "ha tell me andi", "avvunu kavale", "chappade",
        "flat konali", "house konali", "plot konali", "illu kattukovali",
    )
    if any(m in low for m in multi_word_markers):
        return True

    return False


def detect_initial_call_language(text: str, detected_language: Optional[str] = None) -> str:
    """First *substantive* response decides language. English 'yes' stays English."""
    t = (text or "").strip()
    low = t.lower().strip(" .!?,")
    if re_search_telugu(t):
        return "te"
    roman_telugu = {"andi", "avunu", "avunandi", "kavali", "kavale", "ledu",
                    "ledhu", "vaddu", "cheppandi", "meeru", "miru", "nenu",
                    "enti", "entha", "sare", "telugu", "matladandi", "maatladandi"}
    words = set(re.findall(r"[a-zA-Z]+", low))
    if words & roman_telugu or "job chestunna" in low or "business chestunna" in low:
        return "te"
    lang = (detected_language or "").lower().strip()
    if lang.startswith("te") and low not in {"yes", "no", "yeah", "sure", "okay", "ok"}:
        return "te"
    return "en"


def re_search_telugu(text: str) -> bool:
    return any("\u0C00" <= ch <= "\u0C7F" for ch in (text or ""))


def get_recording_followup(language: str = "en") -> str:
    """If a telecom recording announcement interrupts the call, restate opening."""
    return get_opening_text(language=language)


def get_opening_text(language: str = "en", customer_name: str = "") -> str:
    from components.personal_loan_flow import CallFlow, opening
    return opening(CallFlow(language=language, customer_name=customer_name))


def get_personal_loan_opening_text(language: str = "en") -> str:
    """Backward-compatible alias for get_opening_text."""
    return get_opening_text(language=language)


async def speak_opening_message(
    websocket: WebSocket,
    call_audio_path: Optional[Path] = None,
    call_sid: Optional[str] = None,
    language: str = "en",
    customer_name: str = "",
) -> Tuple[str, int, float]:
    """
    Opening message using chunked streaming TTS.
    Defaults to English ('en') like PL or Telugu ('te'), locked or switched based on response.
    """
    opening_text = get_opening_text(language, customer_name=customer_name)
    from components.sarvam_tts import play_chunked_speech

    t_start = time.time()
    print("\n[OPENING] Triggering opening message with streaming TTS:")
    print(f"  Language: {language}")
    print(f"  Text: {opening_text}")

    total_duration, total_bytes, _ = await play_chunked_speech(
        websocket=websocket,
        text_source=opening_text,
        language=language,
        call_audio_path=call_audio_path,
        silence_ms=80,
        t_vad_end=t_start,
        call_sid=call_sid,
    )

    if total_bytes == 0:
        raise RuntimeError("Opening audio generation failed.")

    print(
        f"[OPENING] Completed streaming opening speech: "
        f"{total_bytes} bytes (~{total_duration:.2f}s playback)"
    )
    return opening_text, total_bytes, total_duration
