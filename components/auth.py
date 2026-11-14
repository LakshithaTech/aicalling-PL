# components/auth.py

import hashlib
import hmac
import ipaddress
import os
import re
import secrets
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import jwt
from dotenv import load_dotenv
from fastapi import HTTPException, Request, status

from components.db import get_db_pool

ENV_PATH = Path(__file__).resolve().parent.parent / ".env"
load_dotenv(ENV_PATH, override=False)

COOKIE_NAME = "lmv_pl_token"
TOKEN_EXPIRE_HOURS = 24
_RUNTIME_FALLBACK_SECRET: Optional[str] = None


def _get_jwt_secret() -> str:
    global _RUNTIME_FALLBACK_SECRET
    secret = os.getenv("JWT_SECRET", "").strip()
    if secret:
        return secret

    if ENV_PATH.is_file():
        try:
            env_text = ENV_PATH.read_text(encoding="utf-8")
            for line in env_text.splitlines():
                if line.startswith("JWT_SECRET="):
                    val = line.split("=", 1)[1].strip().strip('"').strip("'")
                    if val:
                        os.environ["JWT_SECRET"] = val
                        return val
            generated = secrets.token_urlsafe(48)
            separator = "" if (not env_text or env_text.endswith("\n")) else "\n"
            ENV_PATH.write_text(
                f"{env_text}{separator}JWT_SECRET={generated}\n",
                encoding="utf-8",
            )
            os.environ["JWT_SECRET"] = generated
            return generated
        except Exception as exc:
            print("Notice: Could not persist JWT_SECRET to .env:", repr(exc))

    if not _RUNTIME_FALLBACK_SECRET:
        _RUNTIME_FALLBACK_SECRET = secrets.token_urlsafe(48)
    return _RUNTIME_FALLBACK_SECRET


def hash_password(plain_password: str) -> str:
    salt = os.urandom(16)
    derived = hashlib.scrypt(
        plain_password.encode("utf-8"),
        salt=salt,
        n=16384,
        r=8,
        p=1,
        dklen=32,
    )
    return f"scrypt${salt.hex()}${derived.hex()}"


def verify_password(plain_password: str, stored_password: str) -> Tuple[bool, bool]:
    """
    Verify plain_password against stored_password.
    Returns (is_valid, needs_rehash).
    """
    if not plain_password or not stored_password:
        return False, False

    if stored_password.startswith("scrypt$"):
        parts = stored_password.split("$")
        if len(parts) != 3:
            return False, False
        try:
            salt = bytes.fromhex(parts[1])
            expected = bytes.fromhex(parts[2])
            derived = hashlib.scrypt(
                plain_password.encode("utf-8"),
                salt=salt,
                n=16384,
                r=8,
                p=1,
                dklen=len(expected),
            )
            return hmac.compare_digest(derived, expected), False
        except Exception:
            return False, False

    # Support legacy SHA-256 hex or direct plaintext test rows inserted manually via SQL, upgrading on login
    if len(stored_password) == 64 and re.fullmatch(r"[0-9a-fA-F]{64}", stored_password):
        sha = hashlib.sha256(plain_password.encode("utf-8")).hexdigest()
        return hmac.compare_digest(sha.lower(), stored_password.lower()), True

    return hmac.compare_digest(plain_password, stored_password), True


def create_access_token(user: Dict[str, Any]) -> str:
    now = datetime.now(timezone.utc)
    payload = {
        "sub": str(user["id"]),
        "username": user["username"],
        "email": user["email"],
        "role": user.get("role") or "admin",
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(hours=TOKEN_EXPIRE_HOURS)).timestamp()),
    }
    return jwt.encode(payload, _get_jwt_secret(), algorithm="HS256")


def decode_access_token(token: str) -> Optional[Dict[str, Any]]:
    try:
        return jwt.decode(token, _get_jwt_secret(), algorithms=["HS256"])
    except Exception:
        return None


def extract_client_ip(request: Request) -> Optional[str]:
    candidates: List[str] = []
    xff = request.headers.get("x-forwarded-for", "")
    if xff:
        candidates.extend([part.strip() for part in xff.split(",") if part.strip()])
    xri = request.headers.get("x-real-ip", "").strip()
    if xri:
        candidates.append(xri)
    if request.client and request.client.host:
        candidates.append(request.client.host.strip())

    for raw in candidates:
        cleaned = raw.split("%")[0].strip()
        if cleaned.startswith("[") and "]" in cleaned:
            cleaned = cleaned[1:cleaned.index("]")]
        elif cleaned.count(":") == 1 and "." in cleaned:
            cleaned = cleaned.split(":", 1)[0]
        try:
            ip_obj = ipaddress.ip_address(cleaned)
            return str(ip_obj)
        except ValueError:
            continue
    return None


def record_login_log(
    admin_id: Optional[int],
    login_success: bool,
    ip_address: Optional[str],
    user_agent: Optional[str],
) -> None:
    p = get_db_pool()
    if not p:
        return
    conn = None
    try:
        conn = p.getconn()
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO public.admin_aicalling_login_logs
                    (admin_id, login_success, ip_address, user_agent)
                VALUES (%s, %s, %s::inet, %s);
                """,
                (
                    admin_id,
                    bool(login_success),
                    ip_address,
                    (user_agent or "")[:1000] or None,
                ),
            )
        conn.commit()
    except Exception as exc:
        if conn:
            try:
                conn.rollback()
            except Exception:
                pass
        print("Notice: Failed to insert login audit log:", repr(exc))
    finally:
        if conn and p:
            try:
                p.putconn(conn)
            except Exception:
                pass


def ensure_initial_admin(
    default_username: str = "admin",
    default_email: str = "admin@lmvhl.com",
    default_password: str = "LmvHl@2026",
) -> None:
    """Seed one initial active admin account only if admin_aicalling_login is completely empty."""
    p = get_db_pool()
    if not p:
        return
    conn = None
    try:
        conn = p.getconn()
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM public.admin_aicalling_login;")
            count = int(cur.fetchone()[0] or 0)
            if count == 0:
                hashed = hash_password(default_password)
                cur.execute(
                    """
                    INSERT INTO public.admin_aicalling_login
                        (username, email, password, role, is_active)
                    VALUES (%s, %s, %s, 'admin', TRUE)
                    ON CONFLICT DO NOTHING
                    RETURNING id;
                    """,
                    (default_username, default_email, hashed),
                )
                row = cur.fetchone()
                conn.commit()
                if row:
                    print(f"Seeded initial LMV_HL admin account id={row[0]} username={default_username}")
    except Exception as exc:
        if conn:
            try:
                conn.rollback()
            except Exception:
                pass
        print("Notice: Could not check/seed initial admin account:", repr(exc))
    finally:
        if conn and p:
            try:
                p.putconn(conn)
            except Exception:
                pass


def authenticate_admin(
    identifier: str,
    password: str,
    ip_address: Optional[str],
    user_agent: Optional[str],
) -> Dict[str, Any]:
    ident = (identifier or "").strip()
    if not ident or not password:
        record_login_log(None, False, ip_address, user_agent)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Username/Email and password are required",
        )

    p = get_db_pool()
    if not p:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Database unavailable for authentication",
        )

    conn = None
    row = None
    try:
        conn = p.getconn()
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, username, email, password, role, is_active, created_at
                FROM public.admin_aicalling_login
                WHERE LOWER(username) = LOWER(%s) OR LOWER(email) = LOWER(%s)
                LIMIT 1;
                """,
                (ident, ident),
            )
            row = cur.fetchone()

        if not row:
            record_login_log(None, False, ip_address, user_agent)
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid username/email or password",
            )

        admin_id, username, email, stored_pw, role, is_active, created_at = row
        if not is_active:
            record_login_log(admin_id, False, ip_address, user_agent)
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="This admin account is inactive. Contact an administrator.",
            )

        ok, needs_rehash = verify_password(password, stored_pw or "")
        if not ok:
            record_login_log(admin_id, False, ip_address, user_agent)
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid username/email or password",
            )

        if needs_rehash:
            try:
                new_hash = hash_password(password)
                with conn.cursor() as cur:
                    cur.execute(
                        "UPDATE public.admin_aicalling_login SET password = %s WHERE id = %s;",
                        (new_hash, admin_id),
                    )
                conn.commit()
            except Exception:
                try:
                    conn.rollback()
                except Exception:
                    pass
    finally:
        if conn and p:
            try:
                p.putconn(conn)
            except Exception:
                pass

    record_login_log(admin_id, True, ip_address, user_agent)
    user = {
        "id": admin_id,
        "username": username,
        "email": email,
        "role": role or "admin",
        "is_active": bool(is_active),
        "created_at": str(created_at),
    }
    token = create_access_token(user)
    return {"success": True, "token": token, "user": user}


def register_admin(
    username: str,
    email: str,
    password: str,
    ip_address: Optional[str],
    user_agent: Optional[str],
) -> Dict[str, Any]:
    clean_user = (username or "").strip()
    clean_email = (email or "").strip().lower()

    if len(clean_user) < 3 or len(clean_user) > 100 or " " in clean_user:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Username must be 3-100 characters with no spaces",
        )
    if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", clean_email) or len(clean_email) > 255:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Please provide a valid email address",
        )
    if not password or len(password) < 6:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Password must be at least 6 characters long",
        )

    p = get_db_pool()
    if not p:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Database unavailable",
        )

    conn = None
    try:
        conn = p.getconn()
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, username, email
                FROM public.admin_aicalling_login
                WHERE LOWER(username) = LOWER(%s) OR LOWER(email) = LOWER(%s)
                LIMIT 1;
                """,
                (clean_user, clean_email),
            )
            existing = cur.fetchone()
            if existing:
                if existing[1].lower() == clean_user.lower():
                    raise HTTPException(
                        status_code=status.HTTP_409_CONFLICT,
                        detail=f"Username '{clean_user}' is already registered",
                    )
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail=f"Email '{clean_email}' is already registered",
                )

            hashed = hash_password(password)
            cur.execute(
                """
                INSERT INTO public.admin_aicalling_login
                    (username, email, password, role, is_active)
                VALUES (%s, %s, %s, 'admin', TRUE)
                RETURNING id, username, email, role, is_active, created_at;
                """,
                (clean_user, clean_email, hashed),
            )
            row = cur.fetchone()
        conn.commit()
    except HTTPException:
        if conn:
            try:
                conn.rollback()
            except Exception:
                pass
        raise
    except Exception as exc:
        if conn:
            try:
                conn.rollback()
            except Exception:
                pass
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to register admin account: {exc}",
        ) from exc
    finally:
        if conn and p:
            try:
                p.putconn(conn)
            except Exception:
                pass

    user = {
        "id": row[0],
        "username": row[1],
        "email": row[2],
        "role": row[3] or "admin",
        "is_active": bool(row[4]),
        "created_at": str(row[5]),
    }
    record_login_log(user["id"], True, ip_address, user_agent)
    token = create_access_token(user)
    return {
        "success": True,
        "message": "Account created successfully",
        "token": token,
        "user": user,
    }


def _extract_token(request: Request) -> Optional[str]:
    auth_header = request.headers.get("authorization", "")
    if auth_header.lower().startswith("bearer "):
        tok = auth_header[7:].strip()
        if tok:
            return tok
    cookie_tok = (request.cookies.get(COOKIE_NAME) or "").strip()
    if cookie_tok:
        return cookie_tok
    return None


def require_admin_user(request: Request) -> Dict[str, Any]:
    strict_auth = os.getenv("REQUIRE_AUTH", "false").lower() in ("1", "true", "yes")

    token = _extract_token(request)
    if not token:
        if not strict_auth:
            return {
                "id": 1,
                "username": "admin",
                "email": "admin@example.com",
                "role": "admin",
                "is_active": True,
                "created_at": "",
            }
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required. Please log in to LMV_PL Calling.",
        )

    payload = decode_access_token(token)
    if not payload or not payload.get("sub"):
        if not strict_auth:
            return {
                "id": 1,
                "username": "admin",
                "email": "admin@example.com",
                "role": "admin",
                "is_active": True,
                "created_at": "",
            }
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Session expired or invalid. Please log in again.",
        )

    try:
        admin_id = int(payload["sub"])
    except (TypeError, ValueError) as exc:
        if not strict_auth:
            return {
                "id": 1,
                "username": "admin",
                "email": "admin@example.com",
                "role": "admin",
                "is_active": True,
                "created_at": "",
            }
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid session token",
        ) from exc

    p = get_db_pool()
    if not p:
        if not strict_auth:
            return {
                "id": admin_id,
                "username": "admin",
                "email": "admin@example.com",
                "role": "admin",
                "is_active": True,
                "created_at": "",
            }
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Database unavailable",
        )

    conn = None
    row = None
    try:
        conn = p.getconn()
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, username, email, role, is_active, created_at
                FROM public.admin_aicalling_login
                WHERE id = %s
                LIMIT 1;
                """,
                (admin_id,),
            )
            row = cur.fetchone()
    except Exception:
        pass
    finally:
        if conn and p:
            try:
                p.putconn(conn)
            except Exception:
                pass

    if not row:
        if not strict_auth:
            return {
                "id": admin_id,
                "username": "admin",
                "email": "admin@example.com",
                "role": "admin",
                "is_active": True,
                "created_at": "",
            }
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Admin account no longer exists.",
        )
    if not row[4]:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin account is inactive.",
        )

    return {
        "id": row[0],
        "username": row[1],
        "email": row[2],
        "role": row[3] or "admin",
        "is_active": bool(row[4]),
        "created_at": str(row[5]),
    }


def fetch_login_logs(limit: int = 200) -> Dict[str, Any]:
    p = get_db_pool()
    if not p:
        return {"error": "Database pool not available"}
    conn = None
    try:
        conn = p.getconn()
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    l.id,
                    l.admin_id,
                    a.username,
                    a.email,
                    a.role,
                    l.login_success,
                    HOST(l.ip_address) AS ip_address,
                    l.user_agent,
                    l.login_time
                FROM public.admin_aicalling_login_logs l
                LEFT JOIN public.admin_aicalling_login a
                    ON a.id = l.admin_id
                ORDER BY l.id DESC
                LIMIT %s;
                """,
                (max(1, min(int(limit), 500)),),
            )
            rows = cur.fetchall()

            cur.execute(
                """
                SELECT
                    COUNT(*) AS total,
                    COUNT(*) FILTER (WHERE login_success = TRUE) AS ok_count,
                    COUNT(*) FILTER (WHERE login_success = FALSE) AS fail_count
                FROM public.admin_aicalling_login_logs;
                """
            )
            counts_row = cur.fetchone() or (0, 0, 0)

            cur.execute(
                """
                SELECT COUNT(*)
                FROM public.admin_aicalling_login
                WHERE is_active = TRUE;
                """
            )
            active_admins = int((cur.fetchone() or (0,))[0] or 0)

            cur.execute(
                """
                SELECT id, username, email, role, is_active, created_at
                FROM public.admin_aicalling_login
                ORDER BY id ASC;
                """
            )
            admin_users = [
                {
                    "id": u[0],
                    "username": u[1],
                    "email": u[2],
                    "role": u[3] or "admin",
                    "is_active": bool(u[4]),
                    "created_at": str(u[5]),
                }
                for u in cur.fetchall()
            ]

        logs = [
            {
                "id": r[0],
                "admin_id": r[1],
                "username": r[2],
                "email": r[3],
                "role": r[4],
                "login_success": bool(r[5]),
                "ip_address": r[6],
                "user_agent": r[7],
                "login_time": str(r[8]),
            }
            for r in rows
        ]
        return {
            "success": True,
            "logs": logs,
            "admins": admin_users,
            "summary": {
                "total": int(counts_row[0] or 0),
                "successful": int(counts_row[1] or 0),
                "failed": int(counts_row[2] or 0),
                "active_admins": active_admins,
            },
        }
    except Exception as exc:
        return {"error": str(exc)}
    finally:
        if conn and p:
            try:
                p.putconn(conn)
            except Exception:
                pass


def init_auth() -> None:
    """Initialize JWT secret and ensure at least one initial admin exists."""
    _get_jwt_secret()
    ensure_initial_admin()
