# components/db.py

import os
from pathlib import Path
from typing import List, Dict, Any, Optional
import psycopg2
from dotenv import load_dotenv
import database

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

get_db_pool = database.get_db_pool
save_call_record = database.save_call_record
save_recording_data = database.save_recording_data


def fetch_all_calls(loan_type: Optional[str] = None) -> Dict[str, Any]:
    """Retrieve recorded calls from PostgreSQL public.ai_agent_calls table."""
    p = get_db_pool()
    if not p:
        return {"error": "Database pool not available"}
    conn = None
    try:
        conn = p.getconn()
        with conn.cursor() as cur:
            normalized_lt = (loan_type or "").strip().lower()
            if normalized_lt:
                cur.execute("""
                    SELECT id, customer_name, phone_number, loan_type, loan_amount,
                           call_status, lead_status, call_duration_seconds,
                           conversation_json, created_at, call_sid, loan_purpose
                    FROM public.ai_agent_calls
                    WHERE LOWER(COALESCE(loan_type, '')) = %s
                    ORDER BY id DESC;
                """, (normalized_lt,))
            else:
                cur.execute("""
                    SELECT id, customer_name, phone_number, loan_type, loan_amount,
                           call_status, lead_status, call_duration_seconds,
                           conversation_json, created_at, call_sid, loan_purpose
                    FROM public.ai_agent_calls
                    ORDER BY id ASC;
                """)
            rows = cur.fetchall()
            calls = []
            for r in rows:
                conv = r[8] or []
                sys_texts = " ".join([
                    m.get("content", "")
                    for m in conv
                    if isinstance(m, dict) and m.get("role") == "system"
                ]).lower()
                status = (r[5] or "").lower()
                purpose = (r[11] or "").lower()

                user_left = False
                call_outcome = status
                if "busy" in status or "busy" in purpose or "user_busy" in sys_texts or "busy" in sys_texts:
                    call_outcome = "busy"
                elif "user left" in sys_texts or "disconnected midway" in sys_texts or "user left" in purpose:
                    user_left = True
                    call_outcome = "user_left_call"
                elif status in ("no_answer", "failed") or "no-answer" in sys_texts or "no_user_response" in sys_texts or "call_rejected" in sys_texts or "failed" in sys_texts:
                    call_outcome = "no_answer"
                elif status == "completed":
                    call_outcome = "completed"

                calls.append({
                    "id": r[0],
                    "customer_name": r[1],
                    "phone_number": r[2],
                    "loan_type": r[3],
                    "loan_amount": float(r[4]) if r[4] is not None else None,
                    "call_status": r[5],
                    "lead_status": r[6],
                    "call_duration_seconds": r[7],
                    "conversation_json": r[8],
                    "created_at": str(r[9]),
                    "call_sid": r[10],
                    "loan_purpose": r[11],
                    "user_left_call": user_left,
                    "call_outcome": call_outcome,
                })
            return {"count": len(calls), "calls": calls}
    except Exception as exc:
        return {"error": str(exc)}
    finally:
        if conn and p:
            try:
                p.putconn(conn)
            except Exception:
                pass


def fetch_call_recording(call_id: int, all_call_recordings_dir: Optional[Path] = None) -> tuple[Optional[bytes], Optional[str]]:
    """Retrieve audio bytes and call_sid from DB or sync from disk if needed."""
    p = get_db_pool()
    if not p:
        return None, None
    if all_call_recordings_dir is None:
        all_call_recordings_dir = Path(__file__).resolve().parent.parent / "allcallrecordings"
    conn = None
    try:
        conn = p.getconn()
        with conn.cursor() as cur:
            cur.execute("SELECT recording_data, call_sid FROM public.ai_agent_calls WHERE id = %s;", (call_id,))
            row = cur.fetchone()
            if not row:
                return None, None
            audio_bytes = bytes(row[0]) if row[0] else None
            call_sid = row[1]
            if not audio_bytes and call_sid:
                disk_file = all_call_recordings_dir / f"call_{call_sid}.mp3"
                if disk_file.exists():
                    try:
                        audio_bytes = disk_file.read_bytes()
                        cur.execute(
                            "UPDATE public.ai_agent_calls SET recording_data = %s WHERE id = %s;",
                            (psycopg2.Binary(audio_bytes), call_id)
                        )
                        conn.commit()
                    except Exception as sync_err:
                        print("Notice: Error syncing disk audio to DB:", repr(sync_err))
            return audio_bytes, call_sid
    finally:
        if conn and p:
            try:
                p.putconn(conn)
            except Exception:
                pass


def _cleanup_recording_files(call_sid: Optional[str], all_call_recordings_dir: Optional[Path] = None) -> None:
    """Remove local .mp3 / .ulaw / .wav recording files for a deleted call_sid, ignoring missing files."""
    if not call_sid:
        return
    safe_sid = Path(str(call_sid)).name
    if not safe_sid or safe_sid != str(call_sid):
        return
    recordings_dir = all_call_recordings_dir or (Path(__file__).resolve().parent.parent / "allcallrecordings")
    for ext in ("mp3", "ulaw", "wav"):
        candidate = recordings_dir / f"call_{safe_sid}.{ext}"
        try:
            if candidate.is_file():
                candidate.unlink()
        except Exception as file_err:
            print(f"Notice: Could not delete local recording {candidate}:", repr(file_err))


def delete_personal_loan_call(call_id: int, all_call_recordings_dir: Optional[Path] = None) -> Dict[str, Any]:
    """Delete a single PostgreSQL call row strictly if loan_type == 'personal_loan'."""
    p = get_db_pool()
    if not p:
        return {"success": False, "error": "Database pool not available"}
    conn = None
    try:
        conn = p.getconn()
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, call_sid, loan_type FROM public.ai_agent_calls WHERE id = %s;",
                (call_id,),
            )
            row = cur.fetchone()
            if not row:
                return {"success": False, "not_found": True, "error": f"Call ID #{call_id} not found"}

            loan_type_val = str(row[2] or "").strip().lower()
            if loan_type_val != "personal_loan":
                return {
                    "success": False,
                    "forbidden": True,
                    "error": f"Refused: Call ID #{call_id} has loan_type='{row[2]}' (only 'personal_loan' rows can be deleted here).",
                }

            cur.execute(
                """
                DELETE FROM public.ai_agent_calls
                WHERE id = %s AND LOWER(COALESCE(loan_type, '')) = 'personal_loan'
                RETURNING id, call_sid;
                """,
                (call_id,),
            )
            deleted = cur.fetchone()
        conn.commit()
        if not deleted:
            return {"success": False, "not_found": True, "error": f"Call ID #{call_id} not found"}

        _cleanup_recording_files(deleted[1], all_call_recordings_dir)
        return {"success": True, "deleted_id": deleted[0], "call_sid": deleted[1]}
    except Exception as exc:
        if conn:
            try:
                conn.rollback()
            except Exception:
                pass
        return {"success": False, "error": str(exc)}
    finally:
        if conn and p:
            try:
                p.putconn(conn)
            except Exception:
                pass


def delete_personal_loan_calls_batch(
    call_ids: Optional[List[int]] = None,
    delete_test_only: bool = False,
    all_call_recordings_dir: Optional[Path] = None,
) -> Dict[str, Any]:
    """Batch-delete PostgreSQL call rows strictly restricted to loan_type == 'personal_loan'."""
    p = get_db_pool()
    if not p:
        return {"success": False, "error": "Database pool not available"}

    conn = None
    try:
        conn = p.getconn()
        deleted_rows = []
        with conn.cursor() as cur:
            if delete_test_only and not call_ids:
                return {"success": False, "bad_request": True,
                        "error": "Bulk deletion by test phone number is disabled. Supply explicit call IDs."}
            else:
                clean_ids = sorted({int(cid) for cid in (call_ids or []) if int(cid) > 0})
                if not clean_ids:
                    return {"success": False, "bad_request": True, "error": "No valid Personal Loan call IDs provided"}
                cur.execute(
                    """
                    DELETE FROM public.ai_agent_calls
                    WHERE id = ANY(%s)
                      AND LOWER(COALESCE(loan_type, '')) = 'personal_loan'
                    RETURNING id, call_sid;
                    """,
                    (clean_ids,),
                )
                deleted_rows = cur.fetchall()
        conn.commit()

        for _, sid in deleted_rows:
            _cleanup_recording_files(sid, all_call_recordings_dir)

        return {
            "success": True,
            "deleted_count": len(deleted_rows),
            "deleted_ids": [r[0] for r in deleted_rows],
        }
    except Exception as exc:
        if conn:
            try:
                conn.rollback()
            except Exception:
                pass
        return {"success": False, "error": str(exc)}
    finally:
        if conn and p:
            try:
                p.putconn(conn)
            except Exception:
                pass
