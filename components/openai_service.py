# components/openai_service.py

import os
import re
from pathlib import Path
from typing import Optional, List, Dict, Any, AsyncGenerator
from dotenv import load_dotenv
from openai import OpenAI, AsyncOpenAI

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")

_async_openai_client: Optional[AsyncOpenAI] = None
_sync_openai_client: Optional[OpenAI] = None


def get_async_openai_client() -> AsyncOpenAI:
    global _async_openai_client
    if _async_openai_client is None:
        key = os.getenv("OPENAI_API_KEY")
        if not key:
            load_dotenv(Path(__file__).resolve().parent.parent / ".env", override=True)
            key = os.getenv("OPENAI_API_KEY")
        if not key:
            raise RuntimeError("OPENAI_API_KEY is missing")
        _async_openai_client = AsyncOpenAI(api_key=key)
    return _async_openai_client


def get_openai_client() -> OpenAI:
    global _sync_openai_client
    if _sync_openai_client is None:
        key = os.getenv("OPENAI_API_KEY")
        if not key:
            load_dotenv(Path(__file__).resolve().parent.parent / ".env", override=True)
            key = os.getenv("OPENAI_API_KEY")
        if not key:
            raise RuntimeError("OPENAI_API_KEY is missing")
        _sync_openai_client = OpenAI(api_key=key)
    return _sync_openai_client


async def generate_chat_reply(messages: List[Dict[str, str]], max_tokens: int = 120) -> str:
    """Generate dynamic AI reply using AsyncOpenAI with model-appropriate parameters."""
    client = get_async_openai_client()
    model = os.getenv("OPENAI_MODEL", OPENAI_MODEL)

    llm_params: Dict[str, Any] = {
        "model": model,
        "messages": messages,
        "max_completion_tokens": max_tokens,
    }
    if any(k in model.lower() for k in ("luna", "o1", "o3", "o4")):
        llm_params["reasoning_effort"] = "low"
    else:
        llm_params["temperature"] = 0.3

    result = await client.chat.completions.create(**llm_params)
    return (result.choices[0].message.content or "").strip()


def generate_sync_chat_reply(messages: List[Dict[str, str]], max_tokens: int = 150) -> str:
    """Generate synchronous AI reply for HTTP endpoints."""
    client = get_openai_client()
    model = os.getenv("OPENAI_MODEL", OPENAI_MODEL)

    llm_params: Dict[str, Any] = {
        "model": model,
        "messages": messages,
        "max_completion_tokens": max_tokens,
    }
    if any(k in model.lower() for k in ("luna", "o1", "o3", "o4")):
        llm_params["reasoning_effort"] = "low"
    else:
        llm_params["temperature"] = 0.3

    result = client.chat.completions.create(**llm_params)
    return (result.choices[0].message.content or "").strip()


async def stream_chat_reply_chunks(
    messages: List[Dict[str, str]],
    max_tokens: int = 120,
    target_words: int = 6,
    min_words: int = 4,
    max_words: int = 7,
) -> AsyncGenerator[str, None]:
    """
    Stream tokens from OpenAI in real-time, yielding completed speech chunks (approx 8-12 words)
    at natural Telugu sentence/clause boundaries without waiting for the full response.
    - Immediately yields Chunk 1 as soon as 6-10 words arrive at a natural boundary.
    - Never splits numbers, currency amounts, or interest rates.
    - Limits turns to a single question by stopping after the first question mark '?'.
    """
    client = get_async_openai_client()
    model = os.getenv("OPENAI_MODEL", OPENAI_MODEL)

    llm_params: Dict[str, Any] = {
        "model": model,
        "messages": messages,
        "max_completion_tokens": max_tokens,
        "stream": True,
    }
    if any(k in model.lower() for k in ("luna", "o1", "o3", "o4")):
        llm_params["reasoning_effort"] = "low"
    else:
        llm_params["temperature"] = 0.3

    try:
        stream = await client.chat.completions.create(**llm_params)
    except Exception as exc:
        print(f"[ERROR] OpenAI streaming init failed: {repr(exc)}")
        yield "సరే అండి, మీ ఆన్సర్ ఇంకోసారి చెప్పగలరా?"
        return

    buffer = ""
    yielded_count = 0
    clause_pattern = re.compile(r'(?<!\d)[.?!।;\n,]+(?!\d)')
    intro_pattern = re.compile(
        r'^(సరే అండి|సరేనండి|సరే|ఓకే అండి|ఓకే|ఖచ్చితంగా అండి|ఖచ్చితంగా|ధన్యవాదాలు అండి|థాంక్యూ అండి|అర్థమైంది అండి|నమస్కారం అండి|హలో అండి|తప్పకుండా అండి|sure|okay|alright|thank you|great|understood|no problem)\s*,\s*',
        re.IGNORECASE
    )

    try:
        async for chunk in stream:
            delta = chunk.choices[0].delta.content if chunk.choices else None
            if not delta:
                continue
            buffer += delta

            words = buffer.split()

            # Check if there is an intro acknowledgment to yield early
            if yielded_count == 0 and len(words) < min_words:
                m = intro_pattern.match(buffer.strip())
                if m:
                    intro = m.group(0).strip()
                    remainder = buffer.strip()[m.end():].lstrip()
                    buffer = remainder
                    yielded_count += 1
                    yield intro
                continue

            # Look for natural clause boundary (. ? ! , ; \n)
            matches = list(clause_pattern.finditer(buffer))
            if matches:
                last_match = matches[-1]
                end_pos = last_match.end()
                candidate = buffer[:end_pos].strip()
                cand_words = candidate.split()

                # Guard against cutting right after a dangling number/particle
                if cand_words and (cand_words[-1] in ('నుండి', 'వరకు', 'శాతం') and len(cand_words) > 1 and cand_words[-2].isdigit()):
                    pass  # Keep together
                elif len(cand_words) >= min_words or (len(words) >= max_words):
                    buffer = buffer[end_pos:].lstrip()
                    if candidate:
                        yielded_count += 1
                        yield candidate
                        # If candidate ends with '?', we've asked our single question
                        if "?" in candidate:
                            return
            elif len(words) >= max_words:
                # Force chunk boundary at pronoun or break word if possible
                cut_idx = target_words
                for i in range(max_words, min_words, -1):
                    if i < len(words):
                        curr_w = words[i]
                        prev_w = words[i - 1]
                        # Don't split currency amounts or percentages
                        if curr_w in ('వరకు', 'నుండి', 'శాతం', 'కోటి', 'లక్ష'):
                            continue
                        if curr_w in ('మీరు', 'మేము', 'వారు', 'లేదా', 'మరియు', 'అయితే', 'కానీ'):
                            cut_idx = i
                            break
                        if prev_w in ('వరకు', 'అందిస్తున్నాము', 'చేయడానికి', 'కోసం'):
                            cut_idx = i
                            break
                chunk_text = " ".join(words[:cut_idx])
                buffer = " ".join(words[cut_idx:])
                yielded_count += 1
                yield chunk_text
                if "?" in chunk_text:
                    return

        remainder = buffer.strip()
        if remainder:
            # If multiple questions were produced in remainder, retain only up to the first question mark
            if "?" in remainder:
                q_part = remainder.split("?")[0].strip() + "?"
                yielded_count += 1
                yield q_part
            else:
                yielded_count += 1
                yield remainder

    except Exception as exc:
        print(f"[ERROR] Exception during LLM stream iteration: {repr(exc)}")
        if yielded_count == 0:
            yield "సరే అండి, మీ ఆన్సర్ ఇంకోసారి చెప్పగలరా?"
