# components/__init__.py

from components.prompts import SYSTEM_PROMPT, LANGUAGE_PROMPT, TELUGU_LANGUAGE_INSTRUCTION
from components.sarvam_tts import (
    SARVAM_MODEL,
    SARVAM_PACE,
    LANGUAGES,
    get_sarvam_client,
    synthesize_sarvam_speech,
    make_sarvam_audio,
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
    speak_opening_message,
)
from components.db import (
    get_db_pool,
    save_call_record,
    save_recording_data,
    fetch_all_calls,
    fetch_call_recording,
)
