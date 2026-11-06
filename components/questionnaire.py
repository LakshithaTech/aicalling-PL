# components/questionnaire.py

import re
import time
from pathlib import Path
from typing import Optional, Tuple
from datetime import datetime
from zoneinfo import ZoneInfo

from fastapi import WebSocket

from components.sarvam_tts import play_chunked_speech


def normalize_hindi_stt(text: str) -> str:
    """
    Map common Hindi/Devanagari STT outputs from Sarvam into standard English/Telugu words.
    Our agent supports ONLY English and Telugu.
    """
    if not text:
        return text
    t = text.strip().replace("।", ".").replace("॥", ".")
    if not re.search(r"[\u0900-\u097F]", t):
        return t

    mapping = {
        "कंपनी": "company",
        "कम्पनी": "company",
        "जॉब": "job",
        "नौकरी": "job",
        "बिजनेस": "business",
        "बिज़नेस": "business",
        "व्यापार": "business",
        "सैलरी": "salary",
        "वेतन": "salary",
        "लोन": "loan",
        "ऋण": "loan",
        "हाँ": "yes",
        "हां": "yes",
        "जी": "yes",
        "जी हाँ": "yes",
        "जी हां": "yes",
        "नहीं": "no",
        "नही": "no",
        "बैंक": "bank",
        "चेक": "check",
        "क्लिक": "check",
        "कंटिन्यू": "continue",
        "लाख": "lakh",
        "हज़ार": "thousand",
        "हजार": "thousand",
        "करोड़": "crore",
        "प्राइवेट": "private",
        "गवर्नमेंट": "government",
    }
    for hi, en in mapping.items():
        t = re.sub(re.escape(hi), en, t)
    return t


def is_confusion_or_identity_query(text: str) -> bool:
    """
    Detect when caller is asking what the call is about, who is calling, why you called,
    or expressing confusion instead of answering the loan offer.
    Examples:
    - 'What are you talking about?'
    - 'Who are you?' / 'Who is this?'
    - 'Why did you call?' / 'What call is this?'
    - 'What is this about?' / 'What do you mean?'
    - 'ఏమిటి ఇది?' / 'దేని గురించి మాట్లాడుతున్నారు?' / 'ఎవరు మీరు?' / 'ఎందుకు కాల్ చేశారు?'
    """
    t = (text or "").lower().strip()
    clean = re.sub(r"[^\w\s\u0C00-\u0C7F]", " ", t)
    clean_t = " ".join(clean.split())

    phrases = (
        "what are you talking about",
        "what are you saying",
        "what do you mean",
        "what is this about",
        "what is this",
        "what is it",
        "what call is this",
        "why are you calling",
        "why did you call",
        "who are you",
        "who is this",
        "who is speaking",
        "i dont understand",
        "i don't understand",
        "did not understand",
        "what loan",
        "which department",
        "kya bol rahe ho",
        "kaun bol raha hai",
        "kya hai ye",
        "ఏమిటి ఇది",
        "ఏంటి ఇది",
        "దేని గురించి",
        "దేని కోసం",
        "ఎవరు మీరు",
        "మీరు ఎవరు",
        "ఎందుకు కాల్ చేశారు",
        "ఎందుకు చేసారు",
        "అర్థం కాలేదు",
        "ఏం మాట్లాడుతున్నారు",
        "ఏంటి చెప్పండి",
    )
    if any(p in clean_t for p in phrases):
        return True

    words = set(clean_t.split())
    if words & {"enti", "emiti", "yenti", "evaru", "evvaru"}:
        return True
    return False


def is_interest_rate_or_details_query(text: str) -> bool:
    """
    Detect if customer is asking about interest rate, loan amount, or offer details.
    Handles phonetic terms like 'interate', 'vaddi entha', 'interest rate kitna', etc.
    """
    raw = (text or "").lower()
    raw = raw.replace("\u200c", "").replace("\u200d", "")
    clean = re.sub(r'[^\w\s\u0C00-\u0C7F\u0900-\u097F%]', ' ', raw).strip()
    t = ' '.join(clean.split())
    if not t:
        return False

    exact_tokens = {
        "interest", "interate", "intrest", "rate", "interest rate", "rate of interest", "roi",
        "vaddi", "vaddee", "vadi", "loan details", "details", "loan amount", "amount",
        "how much", "kitna", "entha", "వడ్డీ", "ఇంట్రెస్ట్", "డీటెయిల్స్",
    }
    if t in exact_tokens:
        return True

    phrases = (
        "interest rate",
        "rate of interest",
        "interate",
        "intrest rate",
        "what interest",
        "what is the interest",
        "how much interest",
        "kitna interest",
        "vaddi entha",
        "entha vaddi",
        "vaddi percentage",
        "interest entha",
        "entha interest",
        "interest percentage",
        "percentage entha",
        "loan amount",
        "how much loan",
        "loan entha",
        "entha loan",
        "loan details",
        "details cheppandi",
        "offer details",
        "terms and conditions",
        "వడ్డీ ఎంత",
        "ఎంత వడ్డీ",
        "ఇంట్రెస్ట్ ఎంత",
        "ఎంత ఇంట్రెస్ట్",
        "ఇంట్రెస్ట్ రేట్",
        "లోన్ ఎంత",
        "ఎంత లోన్",
        "డీటెయిల్స్ చెప్పండి",
        "వివరాలు చెప్పండి",
    )
    if any(p in t for p in phrases):
        return True

    words = set(t.split())
    if ("interest" in words or "interate" in words or "intrest" in words or "vaddi" in words or "వడ్డీ" in words or "ఇంట్రెస్ట్" in words) and (
        words & {"rate", "entha", "kitna", "much", "percentage", "what", "how", "ఎంత"}
    ):
        return True

    return False


def is_repeat_or_clarification_query(text: str) -> bool:
    """
    Detect if customer is asking the agent to repeat or says they didn't understand / hear.
    e.g. 'what did you say?', 'could you repeat?', 'pardon?', 'malli cheppandi', 'malle chpade', 'artham kale'.
    """
    raw = (text or "").lower()
    raw = raw.replace("\u200c", "").replace("\u200d", "")
    clean = re.sub(r'[^\w\s\u0C00-\u0C7F\u0900-\u097F]', ' ', raw).strip()
    t = ' '.join(clean.split())
    if not t:
        return False

    exact_matches = {
        "repeat", "repeat please", "please repeat", "pardon", "what", "come again",
        "say again", "once more", "malli cheppandi", "malle cheppandi", "malle chpade",
        "malli cheppara", "malli", "మళ్ళీ చెప్పండి", "మళ్లీ చెప్పండి", "మళ్ళీ", "మళ్లీ",
        "artham kale", "artham kaledu", "అర్థం కాలేదు",
    }
    if t in exact_matches:
        return True

    phrases = (
        "could you repeat",
        "can you repeat",
        "please repeat",
        "say again",
        "say that again",
        "what did you say",
        "didn't hear",
        "did not hear",
        "didn't understand",
        "did not understand",
        "not audible",
        "voice not clear",
        "malli cheppandi",
        "malle cheppandi",
        "malle chpade",
        "malli cheppara",
        "మళ్ళీ చెప్పండి",
        "మళ్లీ చెప్పండి",
        "మళ్ళీ ఒకసారి",
        "అర్థం కాలేదు",
        "వాయిస్ వినిపించట్లేదు",
        "వినిపించలేదు",
    )
    return any(p in t for p in phrases)


def is_company_response(text: str) -> bool:
    """
    Detect when customer answers 'company', 'in a company', 'working in company',
    'company lo', 'కంపెనీ' to the employment question (Job or Business).
    """
    t = (text or "").lower().strip()
    clean = re.sub(r"[^\w\s\u0C00-\u0C7F]", " ", t)
    clean_t = " ".join(clean.split())

    tokens = {
        "company",
        "in company",
        "in a company",
        "working in company",
        "working in a company",
        "company lo",
        "company lo job",
        "company work",
        "private company",
        "pvt company",
        "software company",
        "కంపెనీ",
        "కంపెనీలో",
        "కంపెనీలో వర్క్",
        "కంపెనీ వర్క్",
        "కంపెనీ లో",
        "కంపెనీ లో జాబ్",
    }
    if clean_t in tokens:
        return True

    words = set(clean_t.split())
    if "company" in words or "కంపెనీ" in words or "కంపెనీలో" in words:
        return True

    return False


def is_continuation_response(text: str) -> bool:
    """
    Detect affirmative continuation responses, including STT phonetic variations:
    e.g. 'check', 'click' (frequent STT misrecognition of check), 'continue', 'forward', 'proceed'.
    """
    clean = re.sub(r'[^\w\s\u0C00-\u0C7F\u0900-\u097F]', ' ', (text or '').lower()).strip()
    t = ' '.join(clean.split())
    if not t:
        return False
    tokens = {
        "click", "clique", "klick", "check", "check it", "check please",
        "continue", "please continue", "forward", "formward", "forwards",
        "proceed", "go ahead", "process", "yes", "yeah", "yep", "sure",
        "okay", "ok", "tell me", "avunu", "kavali", "kavale", "cheppandi",
        "check cheyandi", "chudandi", "choodandi", "అవును", "కావాలి", "సరే",
        "ఓకే", "చెప్పండి", "చూడండి", "కంటిన్యూ",
    }
    if t in tokens:
        return True
    words = set(t.split())
    if words & {"click", "check", "continue", "forward", "formward", "proceed", "go ahead", "process"}:
        return True
    phrases = (
        "check", "click", "continue", "forward", "proceed", "go ahead",
        "tell me", "check cheyandi", "chudandi",
    )
    return any(p in t for p in phrases)


def is_callback_time_query(text: str) -> bool:
    """
    Detect if the text is asking for executive callback day and time.
    e.g.
    - 'On which day and at what time would it be convenient for our executive to call you?'
    - 'మా ఎగ్జిక్యూటివ్ మీకు కాల్ చేయడానికి ఏ రోజు, ఏ టైమ్ మీకు కన్వీనియంట్‌గా ఉంటుంది అండి?'
    - 'మా టీమ్ మిమ్మల్ని కాంటాక్ట్ చేయడానికి మీరు ఏ టైమ్‌లో ఫ్రీగా ఉంటారు?'
    """
    t = (text or "").lower().strip()
    if not t:
        return False

    # A closing farewell / completion statement is NEVER an asking query
    closing_markers = (
        "have a nice day",
        "have a great day",
        "will call you at that time",
        "ధన్యవాదాలు",
        "థాంక్యూ",
        "చేస్తారు",
        "నైస్ డే",
        "గ్రేట్ డే",
        "ఆ సమయానికి",
        "ఆ టైమ్‌కి",
    )
    if any(m in t for m in closing_markers):
        return False

    markers = (
        "convenient",
        "convenient for our executive",
        "on which day",
        "what time would it be convenient",
        "what time",
        "కన్వీనియంట్",
        "కన్వీనియంట్‌గా",
        "ఏ రోజు",
        "ఏ టైమ్",
        "కాల్ చేయడానికి",
        "కాల్ చెయ్యడానికి",
        "ఫ్రీగా ఉంటారు",
    )
    return any(m in t for m in markers)


def is_human_agent_or_callback_request(text: str) -> bool:
    """
    Detect if customer is requesting human agent, executive, manager, callback,
    or saying they are busy / call later / don't want to share phone details.
    """
    raw = (text or "").lower()
    raw = raw.replace("\u200c", "").replace("\u200d", "")
    clean = re.sub(r'[^\w\s\u0C00-\u0C7F]', ' ', raw).strip()
    t = ' '.join(clean.split())
    if not t:
        return False

    phrases = (
        "human agent",
        "talk to agent",
        "speak to agent",
        "talk to executive",
        "speak to executive",
        "connect to agent",
        "connect to executive",
        "call me later",
        "call later",
        "call tomorrow",
        "call in the evening",
        "busy now",
        "in a meeting",
        "not free now",
        "agent tho matladali",
        "team tho matladali",
        "executive tho matladali",
        "manager tho matladali",
        "human agent kavali",
        "repu call cheyandi",
        "later call cheyandi",
        "evening call cheyandi",
        "డైరెక్ట్ టీం",
        "డైరెక్ట్ టీంతో",
        "ఏజెంట్",
        "ఎగ్జిక్యూటివ్",
        "టీమ్‌తో మాట్లాడాలి",
        "మేనేజర్‌తో మాట్లాడాలి",
        "హ్యూమన్ ఏజెంట్",
        "రేపు కాల్ చేయండి",
        "ఈవినింగ్ కాల్ చేయండి",
        "తర్వాత కాల్ చేయండి",
        "ఇప్పుడు బిజీగా ఉన్నాను",
        "మీటింగ్‌లో ఉన్నాను",
        "డీటెయిల్స్ ఇవ్వను",
        "ఇన్ఫర్మేషన్ షేర్ చేయను",
    )
    if any(p in t for p in phrases):
        return True

    words = set(t.split())
    if words & {"busy", "meeting", "agent", "executive", "representative", "బిజీ", "మీటింగ్"}:
        return True

    return False


def is_unemployed_response(text: str) -> bool:
    """Detect if customer clearly states they have no job/business/regular work."""
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
        "bye",
        "end the call",
        "hang up",
        "i am done",
        "i'm done",
        "no more questions",
        "call end cheyandi",
        "call cut cheyandi",
        "కాల్ ఎండ్ చేయండి",
        "కాల్ కట్ చేయండి",
        "చాలు",
        "అంతే",
    )
    return any(phrase in t for phrase in phrases)


def extract_single_question(text: str) -> str:
    """Enforce that only ONE single question is asked per turn."""
    cleaned = (text or "").strip()
    if cleaned.count("?") > 1:
        parts = cleaned.split("?", 1)
        return parts[0].strip() + "?"
    return cleaned


def is_closing_reply(text: str) -> bool:
    lower = (text or "").lower().strip()

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
        "convenient",
        "కన్వీనియంట్",
        "కన్వీనియంట్‌గా",
        "on which day",
        "what time",
        "ఏ రోజు",
        "కాల్ చేయడానికి",
        "కాల్ చెయ్యడానికి",
    )
    if any(marker in lower for marker in non_closing_markers):
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
        "గ్రేట్ డే",
        "విలువైన సమయం",
        "ఆ టైమ్‌కి మా టీమ్",
        "ఆ సమయానికి మా ఎగ్జిక్యూటివ్",
        "executive will call you",
        "త్వరలోనే కాంటాక్ట్ చేస్తుంది",
        "త్వరలోనే సంప్రదిస్తుంది",
    )
    return any(phrase in lower for phrase in closing_phrases)


def detect_yes(text: str) -> bool:
    if is_continuation_response(text):
        return True
    clean = re.sub(r'[^\w\s\u0C00-\u0C7F\u0900-\u097F]', ' ', (text or '').lower()).strip()
    t = ' '.join(clean.split())
    if not t:
        return False
    yes_tokens = {
        "yes", "yes please", "yeah", "yep", "correct", "okay", "ok",
        "sure", "ok sure", "yeah sure", "yes sure", "continue", "please continue", "how much",
        "tell me", "ah tell me", "ha tell me", "aa tell me", "yes tell me", "ok tell me",
        "avunu", "avvunu", "avnu", "aunu", "kavali", "kavale", "ha", "haa", "aa",
        "kavalandi", "kavale andi", "ha andi", "avunandi", "cheppandi", "chappade", "cheppade",
        "sare cheppandi", "sare", "ok cheppandi", "avunu cheppandi",
        "click", "clique", "check", "check it", "forward", "formward", "proceed", "go ahead",
        "అవును", "ఓకే", "సరే", "కావాలి", "హా", "చెప్పండి", "కావాలండి", "సరే చెప్పండి",
        "ఆ", "అవునండి",
    }
    if t in yes_tokens:
        return True
    # If phrase contains affirmative words and NO negation words
    words = set(t.split())
    negation = {"no", "not", "vaddu", "oddu", "oddhu", "ledu", "ledhu", "dont", "nahi", "లేదు", "వద్దు"}
    if not (words & negation):
        affirmative_words = {"yes", "sure", "ok", "okay", "yeah", "yep", "kavali", "avunu", "sare", "cheppandi", "కావాలి", "అవును", "సరే", "చెప్పండి"}
        if words & affirmative_words:
            return True
    return any(tok in t for tok in {"kavali", "kavale", "avunu", "avvunu", "chappade", "cheppade", "tell me", "click", "check", "continue", "forward", "proceed", "కావాలి", "అవును", "how much", "చెప్పండి"})


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
        "లేదు", "వద్దు", "అవసరం లేదు", "వద్దండి", "నాకు వద్దు", "వద్దు అండి", "లేదండి", "ఏం లేదు", "వద్దులే",
        "ledu", "ledhu", "vaddu", "vaddandi", "avasaram ledu", "oddhu", "odhu", "oddu", "oddandi",
        "nahi", "nahi chahiye", "nahin", "nahi chaiye", "mat karo",
    }
    if t in exact_no:
        return True
    words = t.split()
    if words and words[0].strip("'") in {"no", "nope", "nah", "వద్దు", "లేదు", "vaddu", "oddu", "oddhu", "odhu", "ledu", "ledhu", "nahi"}:
        return True
    negative_phrases = (
        "not interested", "don't need", "dont need", "do not need",
        "don't want", "dont want", "do not want", "not required",
        "no need", "no loan", "no loans", "stop calling",
        "అవసరం లేదు", "వద్దు", "లేదు", "వద్దండి", "నాకు వద్దు",
        "avasaram ledu", "vaddu", "oddu", "nahi chahiye",
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
    Normal bank/company/EMI English words do not trigger a switch.
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
        "మీరు తెలుగులో మాట్లాడతారా",
        "మీరు తెలుగు లో మాట్లాడతారా",
        "నేను తెలుగులో మాట్లాడచ్చా",
        "తెలుగులో మాట్లాడండి",
        "తెలుగు లో మాట్లాడండి",
        "తెలుగు మాట్లాడండి",
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
    if low in {"hello", "hello hello", "hi", "hey", "hello sneha", "hello priya", "హలో", "హాలో"}:
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
        "ha andi", "ha cheppandi", "loan kavali", "avunu andi", "kavali andi",
        "tell me", "ah tell me", "ha tell me", "aa tell me", "yes tell me",
        "tell me andi", "tell me madam", "tell me sir", "tell me please",
        "cheppandi madam", "cheppandi sir", "job chestunna", "business chestunna",
        "cheppandi andi", "ha tell me andi", "avvunu kavale", "chappade",
    )
    if any(m in low for m in multi_word_markers):
        return True

    return False


def detect_initial_call_language(text: str, detected_language: Optional[str] = None) -> str:
    """
    Decide language for the FIRST customer response after the opening greeting.
    Default is Telugu ('te').
    Neutral greetings like "Hello", "Hi", "Hello andi" stay Telugu ('te').
    Switches to English ('en') only if the customer speaks an English sentence or phrase.
    """
    t = (text or "").strip()
    low = t.lower().rstrip(".!?,")
    words = [w for w in low.split() if w]

    # Neutral greetings: Hello, Hi, Hey, Hello andi -> Default to Telugu (te)
    neutral_words = {"hello", "hi", "hey", "ha", "haa", "hallo", "sir", "madam"}
    if not words or all(w in neutral_words for w in words):
        return "te"

    # Telugu Unicode -> Telugu
    if re_search_telugu(t):
        return "te"

    # Roman Telugu -> Telugu
    roman_telugu_markers = (
        "andi", "avunu", "kavali", "ledu", "ledhu", "vaddu", "vaddamma",
        "cheppandi", "matlad", "maatlad", "meeru", "miru", "nenu",
        "enti", "entha", "vaddi", "unnara", "unnanu", "chudandi",
        "sare", "ala", "aithe", "job chestunna", "business chestunna",
        "telugu", "namaskaram",
    )
    if any(marker in low for marker in roman_telugu_markers):
        return "te"

    # Check Sarvam detected language:
    detected = (detected_language or "").lower().strip()
    if detected.startswith("te"):
        return "te"

    # If customer spoke substantive English words (2+ words that are English)
    # e.g., "who is this", "what is this", "i don't need loan", "speak in english", "call later"
    if detected.startswith("en") and len(words) >= 2:
        return "en"

    # If single word is explicitly English request
    if "english" in low:
        return "en"

    return "te"


def re_search_telugu(text: str) -> bool:
    return any("\u0C00" <= ch <= "\u0C7F" for ch in (text or ""))


def get_recording_followup(language: str = "te") -> str:
    if language == "en":
        return "We provide loans from 1 lakh to 1 crore. Do you need a loan?"
    return "మేము 1 లక్ష నుండి 1 కోటి వరకు లోన్ ఇస్తున్నాము అండి. మీకు లోన్ అవసరమా?"


def get_opening_text(language: str = "en") -> str:
    current_hour = datetime.now(ZoneInfo("Asia/Kolkata")).hour

    if language == "en":
        if 5 <= current_hour < 12:
            greeting = "Good morning"
        elif 12 <= current_hour < 17:
            greeting = "Good afternoon"
        elif 17 <= current_hour < 21:
            greeting = "Good evening"
        else:
            greeting = "Hello"
        return f"{greeting}! This is Sneha from the personal loan department. I’m calling regarding ICICI Bank personal loans. Do you need a personal loan?"

    if 5 <= current_hour < 12:
        greeting = "గుడ్ మార్నింగ్ అండి"
    elif 12 <= current_hour < 17:
        greeting = "గుడ్ ఆఫ్టర్నూన్ అండి"
    elif 17 <= current_hour < 21:
        greeting = "గుడ్ ఈవినింగ్ అండి"
    else:
        greeting = "నమస్కారం అండి"

    return f"{greeting}! నేను పర్సనల్ లోన్ డిపార్ట్మెంట్ నుండి స్నేహని మాట్లాడుతున్నాను. ఐసీఐసీఐ బ్యాంక్ పర్సనల్ లోన్ గురించి కాల్ చేస్తున్నాను. మీకు పర్సనల్ లోన్ అవసరమా?"


async def speak_opening_message(
    websocket: WebSocket,
    call_audio_path: Optional[Path] = None,
    call_sid: Optional[str] = None,
    language: str = "en",
) -> Tuple[str, int, float]:
    """
    Opening is Telugu by default because CALL_LANGUAGE is not known yet.
    The first REAL customer response locks the language in main.py.
    """
    opening_text = get_opening_text(language)

    t_start = time.time()
    print("\n[OPENING] Triggering opening message with streaming TTS:")
    print(f"  Language: {language}")
    print(f"  Text: {opening_text}")

    total_duration, total_bytes, playback_end = await play_chunked_speech(
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
        f"{total_bytes} bytes (~{total_duration:.2f}s playback, ends at +{playback_end - t_start:.2f}s)"
    )
    return opening_text, total_bytes, total_duration, playback_end
