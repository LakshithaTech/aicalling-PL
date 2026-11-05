# components/sarvam_tts.py



import os

import re

import time

import base64

import asyncio

from pathlib import Path

from typing import Optional, Dict, List, Tuple, Union, AsyncGenerator, Set

from dotenv import load_dotenv

from sarvamai import SarvamAI, AsyncSarvamAI

from fastapi import WebSocket



load_dotenv(Path(__file__).resolve().parent.parent / ".env")





# --------------------------------------------------

# SARVAM CONFIGURATION

# --------------------------------------------------



SARVAM_MODEL = "bulbul:v4-flash"



SARVAM_PACE = 1.2
SARVAM_PACE_EN = 1.1
SARVAM_PACE_TE = 1.3
SARVAM_PITCH = 0.0

SARVAM_LOUDNESS = 1.0





# --------------------------------------------------

# LANGUAGE CONFIGURATION

# --------------------------------------------------



LANGUAGES = {

    "en": {
        "code": "en-IN",
        "speaker": "simran_en_customer",
        "pace": SARVAM_PACE_EN,
    },

    "hi": {

        "code": "hi-IN",

        "speaker": "ritu_hi_customer",

    },

    "te": {

        "code": "te-IN",

        "speaker": os.getenv(

            "SARVAM_TELUGU_SPEAKER",

            "kavitha_te_conversation"

        ),

        "pace": SARVAM_PACE_TE,
    },

}



# Predefined message & chunk cache for ultra-low latency playback (0ms TTFB)

AUDIO_CHUNK_CACHE: Dict[str, bytes] = {}



# Active generation tasks per call for clean cancellation on hangup

CALL_TTS_TASKS: Dict[str, Set[asyncio.Task]] = {}





# --------------------------------------------------

# SARVAM CLIENTS

# --------------------------------------------------



_sarvam_client: Optional[SarvamAI] = None

_async_sarvam_client: Optional[AsyncSarvamAI] = None





def get_sarvam_client() -> SarvamAI:

    global _sarvam_client

    if _sarvam_client is None:

        key = os.getenv("SARVAM_API_KEY")

        if not key:

            load_dotenv(Path(__file__).resolve().parent.parent / ".env", override=True)

            key = os.getenv("SARVAM_API_KEY")

        if not key:

            raise RuntimeError("SARVAM_API_KEY is missing")

        _sarvam_client = SarvamAI(api_subscription_key=key)

    return _sarvam_client





def get_async_sarvam_client() -> AsyncSarvamAI:

    global _async_sarvam_client

    if _async_sarvam_client is None:

        key = os.getenv("SARVAM_API_KEY")

        if not key:

            load_dotenv(Path(__file__).resolve().parent.parent / ".env", override=True)

            key = os.getenv("SARVAM_API_KEY")

        if not key:

            raise RuntimeError("SARVAM_API_KEY is missing")

        _async_sarvam_client = AsyncSarvamAI(api_subscription_key=key)

    return _async_sarvam_client





# --------------------------------------------------

# TEXT NORMALIZATION

# --------------------------------------------------



def normalize_speech_text(text: str, language: str = "te") -> str:

    """Normalize text (numbers, percentages, whitespaces) for smooth speech synthesis."""

    if language == "te":

        text = re.sub(

            r'&#x39;\\.&#x39;9\s\*%\s\*(శాతం)?',

            'నైన్ పాయింట్ నైన్ నైన్ పర్సెంట్ ',

            text

        )

        text = re.sub(

            r'18\s*%\s*(శాతం)?',

            'ఎయిటీన్ పర్సెంట్ ',

            text

        )

    elif language == "en":
        text = re.sub(r'9\.99\s*%', 'nine point nine nine percent', text)
        text = re.sub(r'18\s*%', 'eighteen percent', text)

    return re.sub(r'\s+', ' ', text).strip()





# --------------------------------------------------

# WORD & CLAUSE BOUNDARY CHUNKER

# --------------------------------------------------



def split_into_speech_chunks(

    text: str,

    language: str = "te",

    target_words: int = 6,

    min_words: int = 4,

    max_words: int = 7

) -> List[str]:

    """

    Split long Telugu AI replies into small, natural groups of approximately 5–7 words.

    - Normalizes speech text first (numbers, percentages, currencies).

    - Preserves natural clause and sentence boundaries.

    - Yields Chunk 1 immediately for ultra-fast TTFB (~300-400ms).

    - Never splits numbers, interest rates, currency amounts, or connected phrases.

    """

    text = normalize_speech_text(text, language).strip()

    if not text:

        return []

    # Dedicated English chunker: Preserves complete natural sentence & clause boundaries.
    # Avoids cutting inside prepositional phrases ("from 1 lakh") or noun phrases ("personal loan department").
    if language == "en":
        sentence_pattern = r'(?<!\d)[.?!;\n]+(?!\d)'
        sentences = []
        last_idx = 0
        for match in re.finditer(sentence_pattern, text):
            end = match.end()
            s_part = text[last_idx:end].strip()
            if s_part:
                sentences.append(s_part)
            last_idx = end
        if last_idx < len(text):
            rem = text[last_idx:].strip()
            if rem:
                sentences.append(rem)
        if not sentences:
            sentences = [text]

        chunks = []
        for s in sentences:
            s_words = s.split()
            if len(s_words) <= 25:
                chunks.append(s)
            else:
                # If exceptionally long, split only at natural comma pauses
                comma_parts = [p.strip() for p in re.split(r'(?<=,)\s+', s) if p.strip()]
                if len(comma_parts) > 1:
                    chunks.extend(comma_parts)
                else:
                    chunks.append(s)
        return [chunk for chunk in chunks if chunk.strip()]

    or_words = {'లేదా', 'or'}

    words = text.split()

    if len(words) <= max_words:

        # Check if there is an internal 'లేదా' break even in short sentences

        for i, w in enumerate(words):

            if w.lower() in or_words and min_words <= (i + 1) and (len(words) - (i + 1)) >= 2:

                return [' '.join(words[:i + 1]), ' '.join(words[i + 1:])]

        return [text]



    # Split into major sentences first

    sentence_pattern = r'(?<!\d)[.?!।;\n]+(?!\d)'

    sentences = []

    last_idx = 0

    for match in re.finditer(sentence_pattern, text):

        end = match.end()

        s_part = text[last_idx:end].strip()

        if s_part:

            sentences.append(s_part)

        last_idx = end

    if last_idx < len(text):

        rem = text[last_idx:].strip()

        if rem:

            sentences.append(rem)

    if not sentences:

        sentences = [text]



    chunks = []

    break_after = {'నుండి', 'వరకు', 'లేదా', 'అయితే', 'కానీ', 'మరియు', 'చేయడానికి', 'కోసం'}

    break_before = {'మీరు', 'మేము', 'వారు', 'లేదా'}



    for s in sentences:

        s_words = s.split()

        if len(s_words) <= max_words:

            # Check for 'లేదా' break

            split_done = False

            for i, w in enumerate(s_words):

                if w.lower() in or_words and min_words <= (i + 1) and (len(s_words) - (i + 1)) >= 2:

                    chunks.append(' '.join(s_words[:i + 1]))

                    chunks.append(' '.join(s_words[i + 1:]))

                    split_done = True

                    break

            if not split_done:

                chunks.append(s)

        else:

            w_idx = 0

            while w_idx < len(s_words):

                rem_w = len(s_words) - w_idx

                if rem_w <= max_words:

                    rem_slice = s_words[w_idx:]

                    split_rem = False

                    for i, w in enumerate(rem_slice):

                        if w.lower() in or_words and min_words <= (i + 1) and (len(rem_slice) - (i + 1)) >= 2:

                            chunks.append(' '.join(rem_slice[:i + 1]))

                            chunks.append(' '.join(rem_slice[i + 1:]))

                            split_rem = True

                            break

                    if not split_rem:

                        chunks.append(' '.join(rem_slice))

                    break



                split_at = target_words

                found = False



                # 1. Comma within window

                for offset in range(max_words, min_words - 1, -1):

                    pos = w_idx + offset

                    if pos < len(s_words) and s_words[pos - 1].endswith(','):

                        split_at = offset

                        found = True

                        break



                # 2. Break after particle like 'నుండి', 'వరకు'

                if not found:

                    for offset in range(max_words, min_words - 1, -1):

                        pos = w_idx + offset

                        if pos < len(s_words) and s_words[pos - 1].lower() in break_after:

                            split_at = offset

                            found = True

                            break



                # 3. Break before pronoun like 'మీరు', 'మేము'

                if not found:

                    for offset in range(max_words, min_words - 1, -1):

                        pos = w_idx + offset

                        if pos < len(s_words) and s_words[pos].lower() in break_before:

                            split_at = offset

                            found = True

                            break



                chunks.append(' '.join(s_words[w_idx:w_idx + split_at]))

                w_idx += split_at



    return chunks





# --------------------------------------------------

# TEXT TO SPEECH (SYNCHRONOUS & ASYNCHRONOUS)

# --------------------------------------------------



def synthesize_sarvam_speech(

    text: str,

    language: str = "en"

) -> bytes:

    language = language if language in LANGUAGES else "en"

    voice = LANGUAGES[language]
    pace = voice.get("pace", SARVAM_PACE)

    text = normalize_speech_text(text, language)



    cache_key = f"{language}:{voice['speaker']}:{pace}:{text}"

    if cache_key in AUDIO_CHUNK_CACHE:

        return AUDIO_CHUNK_CACHE[cache_key]



    client = get_sarvam_client()

    result = client.text_to_speech.convert(

        text=text,

        model=SARVAM_MODEL,

        language_code=voice["code"],

        speaker=voice["speaker"],

        pace=pace,

        pitch=SARVAM_PITCH,

        loudness=SARVAM_LOUDNESS,

        speech_sample_rate=8000,

        output_audio_codec="mulaw",

    )



    if not result.audios:

        raise RuntimeError("Sarvam returned no audio")



    audio_bytes = base64.b64decode(result.audios[0])

    AUDIO_CHUNK_CACHE[cache_key] = audio_bytes

    return audio_bytes





async def synthesize_sarvam_speech_async(

    text: str,

    language: str = "en",

    retries: int = 2

) -> bytes:

    """Asynchronous TTS conversion with in-memory caching and automatic retry."""

    language = language if language in LANGUAGES else "en"

    voice = LANGUAGES[language]
    pace = voice.get("pace", SARVAM_PACE)

    text = normalize_speech_text(text, language)



    cache_key = f"{language}:{voice['speaker']}:{pace}:{text}"

    if cache_key in AUDIO_CHUNK_CACHE:

        return AUDIO_CHUNK_CACHE[cache_key]



    client = get_async_sarvam_client()

    last_err = None



    for attempt in range(retries + 1):

        try:

            result = await client.text_to_speech.convert(

                text=text,

                model=SARVAM_MODEL,

                language_code=voice["code"],

                speaker=voice["speaker"],

                pace=pace,

                pitch=SARVAM_PITCH,

                loudness=SARVAM_LOUDNESS,

                speech_sample_rate=8000,

                output_audio_codec="mulaw",

            )

            if not result.audios:

                raise RuntimeError("Sarvam returned no audio")



            audio_bytes = base64.b64decode(result.audios[0])

            AUDIO_CHUNK_CACHE[cache_key] = audio_bytes

            return audio_bytes

        except Exception as exc:

            last_err = exc

            if attempt < retries:

                await asyncio.sleep(0.1)



    raise last_err or RuntimeError("Failed to synthesize Sarvam speech.")





def cancel_call_tts(call_sid: str):

    """Cancel all active background TTS generation tasks for a given call."""

    if call_sid in CALL_TTS_TASKS:

        tasks = list(CALL_TTS_TASKS[call_sid])

        for t in tasks:

            if not t.done():

                t.cancel()

        CALL_TTS_TASKS.pop(call_sid, None)

        print(f"[CLEANUP] Cancelled {len(tasks)} pending TTS tasks for call {call_sid}")





# --------------------------------------------------

# CONTINUOUS PIPELINED AUDIO STREAMING ENGINE

# --------------------------------------------------



async def play_chunked_speech(

    websocket: WebSocket,

    text_source: Union[str, AsyncGenerator[str, None]],

    language: str = "te",

    call_audio_path: Optional[Path] = None,

    silence_ms: int = 80,

    t_vad_end: Optional[float] = None,

    call_sid: Optional[str] = None,

) -> Tuple[float, int, str]:

    """

    Fast, continuous audio streaming engine:

    1. Feeds chunks from string or live LLM async generator into a bounded concurrent worker pool (semaphore=3).

    2. Sends Chunk 1 immediately as soon as its audio arrives (achieving ~1s TTFB).

    3. Concurrently synthesizes subsequent chunks in background while Chunk 1 plays over Plivo.

    4. Queues playback in strict original order with an 80ms silence buffer to avoid gaps/pops.

    5. Disables barge-in by computing true physical carrier playback duration.

    6. Logs detailed timing metrics at each milestone.

    """

    from components.plivo_service import send_audio_to_plivo



    t_start = t_vad_end or time.time()

    call_id = call_sid or "default"

    active_tasks = CALL_TTS_TASKS.setdefault(call_id, set())



    # Ordered queue of background tasks: each produces (chunk_index, chunk_text, audio_bytes)

    task_queue: asyncio.Queue[Optional[asyncio.Task]] = asyncio.Queue()

    all_chunks: List[str] = []

    t_first_llm_chunk: Optional[float] = None



    sem = asyncio.Semaphore(3)



    async def synth_task(idx: int, chunk_text: str) -> Tuple[int, str, bytes]:

        async with sem:

            audio = await synthesize_sarvam_speech_async(chunk_text, language)

            return idx, chunk_text, audio



    # Feeder: consumes text source (string or token generator) and dispatches TTS tasks immediately

    async def feeder():

        nonlocal t_first_llm_chunk

        try:

            if isinstance(text_source, str):

                chunks = split_into_speech_chunks(text_source, language=language)

                for idx, c in enumerate(chunks):

                    c = c.strip()

                    if not c:

                        continue

                    all_chunks.append(c)

                    t = asyncio.create_task(synth_task(idx, c))

                    active_tasks.add(t)

                    await task_queue.put(t)

            else:

                idx = 0

                async for c in text_source:

                    c = c.strip()

                    if not c:

                        continue

                    if t_first_llm_chunk is None:

                        t_first_llm_chunk = time.time()

                        print(f"[TIMING] [t_first_llm_chunk] First LLM chunk ready (+{t_first_llm_chunk - t_start:.3f}s): '{c}'")

                    all_chunks.append(c)

                    t = asyncio.create_task(synth_task(idx, c))

                    active_tasks.add(t)

                    await task_queue.put(t)

                    idx += 1

        except Exception as exc:

            print(f"[ERROR] Feeder exception in play_chunked_speech: {repr(exc)}")

        finally:

            await task_queue.put(None)



    feeder_task = asyncio.create_task(feeder())

    active_tasks.add(feeder_task)



    total_bytes = 0

    silence_bytes = b"\xff" * int(8000 * (silence_ms / 1000.0))

    t_first_sent: Optional[float] = None

    processed_count = 0



    try:

        while True:

            t = await task_queue.get()

            if t is None:

                break



            try:

                c_idx, c_text, chunk_audio = await t

            except asyncio.CancelledError:

                print(f"[STREAM] Generation for chunk was cancelled.")

                break

            except Exception as exc:

                print(f"[ERROR] Chunk TTS synthesis failed: {repr(exc)}")

                continue



            if not chunk_audio:

                continue



            # First chunk timing log

            if processed_count == 0:

                t_first_tts = time.time()

                print(f"[TIMING] [t_first_tts_audio] First TTS audio generated (+{t_first_tts - t_start:.3f}s, {len(chunk_audio)} bytes)")



            # Natural silence buffer between chunks (except before chunk 1)

            payload_bytes = chunk_audio if processed_count == 0 else (silence_bytes + chunk_audio)



            # Write to call recording file

            if call_audio_path:

                try:

                    with open(call_audio_path, "ab") as f:

                        f.write(payload_bytes)

                except Exception:

                    pass



            # Transmit base64 payload to Plivo

            audio_b64 = base64.b64encode(payload_bytes).decode("ascii")

            await send_audio_to_plivo(websocket, audio_b64)



            now = time.time()

            if processed_count == 0:

                t_first_sent = now

                ttfb = t_first_sent - t_start

                print(f"[TIMING] [t_first_audio_sent] First audio chunk sent to Plivo WebSocket (TTFB: {ttfb:.3f}s) - Agent starts speaking!")



            total_bytes += len(payload_bytes)

            chunk_duration = len(payload_bytes) / 8000.0

            processed_count += 1

            print(f"  -> [Chunk {processed_count}] Sent to Plivo (+{now - t_start:.3f}s, {len(payload_bytes)} bytes, ~{chunk_duration:.2f}s): '{c_text}'")



    finally:

        active_tasks.discard(feeder_task)

        if not feeder_task.done():

            feeder_task.cancel()



    full_text = " ".join(all_chunks)

    total_duration = total_bytes / 8000.0
    playback_end = (t_first_sent or time.time()) + total_duration

    print(f"[TIMING] All audio transmitted to Plivo buffer: {total_bytes} bytes (~{total_duration:.2f}s speech, ends at +{playback_end - t_start:.2f}s)")

    return total_duration, total_bytes, playback_end





# --------------------------------------------------

# SAVE GENERATED AUDIO (UTILITY)

# --------------------------------------------------



def make_sarvam_audio(

    text: str,

    filename: str,

    language: str = "en",

    audio_dir: Optional[Path] = None

):

    if audio_dir is None:

        audio_dir = (

            Path(__file__).resolve().parent.parent

            / "generated_audio"

        )

    audio_dir.mkdir(exist_ok=True)

    audio_bytes = synthesize_sarvam_speech(text, language)

    audio_path = audio_dir / filename

    audio_path.write_bytes(audio_bytes)

    return filename
