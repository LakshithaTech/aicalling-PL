"""
PostgreSQL database integration for AI Voice Agent.
Saves call conversations and lead details into public.ai_agent_calls.
"""

import os
import re
import json
from datetime import datetime, timezone
from typing import List, Dict, Any, Optional
import psycopg2
from psycopg2 import pool
from dotenv import load_dotenv

load_dotenv()

DB_HOST = os.getenv("DB_HOST", "")
DB_PORT = int(os.getenv("DB_PORT", "5432"))
DB_NAME = os.getenv("DB_NAME", "")
DB_USER = os.getenv("DB_USER", "")
DB_PASSWORD = os.getenv("DB_PASSWORD", "")
DB_SSL = os.getenv("DB_SSL", "true").lower() in ("true", "1", "yes")

_db_pool: Optional[pool.ThreadedConnectionPool] = None


def get_db_pool() -> Optional[pool.ThreadedConnectionPool]:
    """Initialize or return the thread-safe connection pool."""
    global _db_pool
    if _db_pool is None:
        if not DB_HOST or not DB_PASSWORD:
            print("Database configuration missing (DB_HOST or DB_PASSWORD). Database operations disabled.")
            return None
        try:
            _db_pool = pool.ThreadedConnectionPool(
                minconn=1,
                maxconn=10,
                host=DB_HOST,
                port=DB_PORT,
                dbname=DB_NAME,
                user=DB_USER,
                password=DB_PASSWORD,
                sslmode="require" if DB_SSL else "prefer",
                options="-c search_path=mfbro,public",
                connect_timeout=15,
            )
            print("PostgreSQL connection pool initialized successfully.")
        except Exception as exc:
            print("Failed to initialize PostgreSQL pool:", repr(exc))
            _db_pool = None
    return _db_pool


def determine_lead_status(conversation_history: List[Dict[str, Any]]) -> str:
    """
    Determine lead status based on conversation history.
    Allowed values: 'new', 'interested', 'not_interested', 'follow_up', 'converted'
    """
    user_texts = [
        msg.get("content", "").lower()
        for msg in conversation_history
        if msg.get("role") == "user"
    ]
    if not user_texts:
        return "new"

    full_user_text = " ".join(user_texts)

    # Check for disinterest / rejection
    if re.search(r"\b(not interested|don'?t want|no loan|no need|stop calling)\b", full_user_text):
        return "not_interested"

    # If first answer was a plain rejection
    if len(user_texts) == 1 and user_texts[0].strip(" .!").lower() in ("no", "nahi", "vaddu"):
        return "not_interested"

    # If customer answered multiple questions, they are interested
    if len(user_texts) >= 2:
        return "interested"

    return "new"


def extract_loan_amount(conversation_history: List[Dict[str, Any]]) -> Optional[float]:
    """Attempt to extract loan amount or limit from conversation."""
    for msg in conversation_history:
        if msg.get("role") == "user":
            text = msg.get("content", "").lower()
            # Check for patterns like "1 lakh", "2 lakh", "50 thousand", etc.
            lakh_match = re.search(r"(\d+(?:\.\d+)?)\s*(?:lakh|lac|lakhs)", text)
            if lakh_match:
                try:
                    return float(lakh_match.group(1)) * 100000.0
                except ValueError:
                    pass
            if "one lakh" in text:
                return 100000.0
            if "two lakh" in text:
                return 200000.0
            if "five lakh" in text:
                return 500000.0
            num_match = re.search(r"\b(\d{4,8})\b", text)
            if num_match:
                try:
                    return float(num_match.group(1))
                except ValueError:
                    pass
    return None


def save_call_record(
    call_sid: Optional[str],
    phone_number: Optional[str],
    conversation_history: List[Dict[str, Any]],
    call_status: str = "completed",
    lead_status: Optional[str] = None,
    loan_type: str = "personal_loan",
    customer_name: Optional[str] = None,
    call_started_at: Optional[datetime] = None,
    call_ended_at: Optional[datetime] = None,
    loan_purpose: Optional[str] = "Personal loan enquiry",
) -> Optional[int]:
    """
    Save or update a call record in public.ai_agent_calls.
    Uses UPSERT on conflict (call_sid) to prevent duplicate rows.
    """
    p = get_db_pool()
    if not p:
        print("Database pool not available. Skipping DB save.")
        return None

    now = datetime.now(timezone.utc)
    started_at = call_started_at or now
    ended_at = call_ended_at or now

    duration = max(0, int((ended_at - started_at).total_seconds()))

    clean_phone = (phone_number or "").strip()
    if not clean_phone:
        clean_phone = "+919999999999"

    # Enforce allowed loan_type
    allowed_loan_types = {
        "home_loan",
        "personal_loan",
        "car_loan",
        "business_loan",
        "education_loan",
        "other",
    }
    if loan_type not in allowed_loan_types:
        loan_type = "personal_loan"

    # Enforce allowed call_status
    allowed_call_status = {"pending", "in_progress", "completed", "failed", "no_answer"}
    if call_status not in allowed_call_status:
        call_status = "completed"

    # Determine or enforce lead_status
    if not lead_status:
        lead_status = determine_lead_status(conversation_history)
    allowed_lead_status = {"new", "interested", "not_interested", "follow_up", "converted"}
    if lead_status not in allowed_lead_status:
        lead_status = "new"

    loan_amount = extract_loan_amount(conversation_history)
    conv_json_str = json.dumps(conversation_history, ensure_ascii=False)

    # Use placeholder call_sid if None
    unique_call_sid = call_sid or f"call_{int(now.timestamp())}_{clean_phone.replace('+', '')}"

    query = """
        INSERT INTO public.ai_agent_calls (
            customer_name,
            phone_number,
            loan_type,
            loan_amount,
            loan_purpose,
            conversation_json,
            call_status,
            call_sid,
            call_duration_seconds,
            call_started_at,
            call_ended_at,
            lead_status,
            created_at,
            updated_at
        ) VALUES (
            %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
        )
        ON CONFLICT (call_sid) DO UPDATE SET
            customer_name = COALESCE(EXCLUDED.customer_name, public.ai_agent_calls.customer_name),
            phone_number = COALESCE(EXCLUDED.phone_number, public.ai_agent_calls.phone_number),
            loan_type = EXCLUDED.loan_type,
            loan_amount = EXCLUDED.loan_amount,
            loan_purpose = EXCLUDED.loan_purpose,
            conversation_json = EXCLUDED.conversation_json,
            call_status = EXCLUDED.call_status,
            call_duration_seconds = EXCLUDED.call_duration_seconds,
            call_started_at = COALESCE(public.ai_agent_calls.call_started_at, EXCLUDED.call_started_at),
            call_ended_at = EXCLUDED.call_ended_at,
            lead_status = EXCLUDED.lead_status,
            updated_at = EXCLUDED.updated_at
        RETURNING id;
    """

    conn = None
    try:
        conn = p.getconn()
        with conn.cursor() as cur:
            cur.execute(
                query,
                (
                    customer_name,
                    clean_phone,
                    loan_type,
                    loan_amount,
                    loan_purpose,
                    conv_json_str,
                    call_status,
                    unique_call_sid,
                    duration,
                    started_at,
                    ended_at,
                    lead_status,
                    now,
                    now,
                ),
            )
            inserted_id = cur.fetchone()[0]
        conn.commit()

        # If recording file already exists on disk, ensure recording_data in DB is populated
        try:
            from pathlib import Path
            rec_file = Path("allcallrecordings") / f"call_{unique_call_sid}.mp3"
            if rec_file.exists():
                with conn.cursor() as cur:
                    cur.execute("""
                        UPDATE public.ai_agent_calls
                        SET recording_data = %s
                        WHERE id = %s AND recording_data IS NULL;
                    """, (psycopg2.Binary(rec_file.read_bytes()), inserted_id))
                conn.commit()
        except Exception as file_sync_err:
            print("Notice: Error checking disk recording during save_call_record:", repr(file_sync_err))

        print(f"Conversation saved to DB successfully! ID: {inserted_id}, CallSid: {unique_call_sid}")
        return inserted_id
    except Exception as exc:
        if conn:
            try:
                conn.rollback()
            except Exception:
                pass
        print("Failed to save call conversation to PostgreSQL:", repr(exc))
        return None
    finally:
        if conn and p:
            try:
                p.putconn(conn)
            except Exception:
                pass


def save_recording_data(call_sid: str, recording_bytes: bytes) -> bool:
    """Save binary recording data into public.ai_agent_calls.recording_data column."""
    p = get_db_pool()
    if not p:
        print("Database pool not available.")
        return False
    conn = None
    try:
        conn = p.getconn()
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE public.ai_agent_calls
                SET recording_data = %s, updated_at = now()
                WHERE call_sid = %s;
            """, (psycopg2.Binary(recording_bytes), call_sid))
            if cur.rowcount == 0:
                # If call record has not yet been inserted by the websocket stream end, insert initial row
                cur.execute("""
                    INSERT INTO public.ai_agent_calls (call_sid, phone_number, recording_data, call_status, created_at, updated_at)
                    VALUES (%s, '+919999999999', %s, 'in_progress', now(), now())
                    ON CONFLICT (call_sid) DO UPDATE
                    SET recording_data = EXCLUDED.recording_data, updated_at = now();
                """, (call_sid, psycopg2.Binary(recording_bytes)))
        conn.commit()
        print(f"Successfully saved {len(recording_bytes)} bytes of recording_data to DB for CallSid: {call_sid}")
        return True
    except Exception as exc:
        if conn:
            try:
                conn.rollback()
            except Exception:
                pass
        print("Failed to save recording_data to DB:", repr(exc))
        return False
    finally:
        if conn and p:
            try:
                p.putconn(conn)
            except Exception:
                pass

