import hashlib
import hmac
import secrets
from typing import Optional
from fastapi import Request
from app.database import db_session


def _hash_password(password: str, salt: Optional[str] = None) -> str:
    use_salt = salt or secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), use_salt.encode("utf-8"), 120_000).hex()
    return f"{use_salt}${digest}"


def _verify_password(password: str, stored_hash: str) -> bool:
    if not password or not stored_hash or "$" not in stored_hash:
        return False
    salt, expected_hex = stored_hash.split("$", 1)
    actual_hex = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode("utf-8"), 120_000).hex()
    return hmac.compare_digest(actual_hex, expected_hex)


def ensure_default_profile_for_user(user_id: int) -> None:
    with db_session() as conn:
        existing = conn.execute("SELECT id FROM user_profile WHERE user_id = ?", (user_id,)).fetchone()
        if existing:
            return
        conn.execute(
            """
            INSERT INTO user_profile (
                user_id, home_location, max_commute_min, max_commute_km, digest_time,
                search_keywords, negative_patterns, cv_text, cv_skills_json,
                use_jobs_cz, use_prace_cz, use_jenprace_cz, use_linkedin, use_uradprace_cz,
                email_digest_enabled, email_digest_frequency, email_recipient, email_min_score,
                min_display_score, auto_delete_days, custom_llm_api_key
            ) VALUES (?, '', 30, 20, '07:30', '', '', '', '[]', 0, 0, 0, 0, 0, 0, 'daily', '', 50, 40, 30, NULL)
            """,
            (user_id,),
        )


def create_user(username: str, password: str, is_admin: int = 0) -> tuple[bool, str]:
    clean_user = (username or "").strip()
    if len(clean_user) < 2 or len(clean_user) > 60:
        return False, "Uživatelské jméno musí mít 2 až 60 znaků."
    if len(password or "") < 6:
        return False, "Heslo musí mít alespoň 6 znaků."

    pw_hash = _hash_password(password)
    try:
        with db_session() as conn:
            cur = conn.execute(
                "INSERT INTO users (username, password_hash, is_admin) VALUES (?, ?, ?)",
                (clean_user, pw_hash, int(is_admin)),
            )
            uid = int(cur.lastrowid)
        ensure_default_profile_for_user(uid)
        return True, "Účet byl vytvořen."
    except Exception as exc:
        if "unique" in str(exc).lower():
            return False, "Toto uživatelské jméno už existuje."
        return False, f"Nepodařilo se vytvořit účet: {type(exc).__name__}"


def authenticate_user(username: str, password: str) -> Optional[dict]:
    clean_user = (username or "").strip()
    if not clean_user or not password:
        return None
    with db_session() as conn:
        row = conn.execute(
            "SELECT id, username, password_hash, is_admin FROM users WHERE LOWER(username) = LOWER(?)",
            (clean_user,),
        ).fetchone()
    if not row or not _verify_password(password, row["password_hash"]):
        return None
    ensure_default_profile_for_user(int(row["id"]))
    return {"id": int(row["id"]), "username": row["username"], "is_admin": int(row["is_admin"])}


def get_current_user(request: Request) -> Optional[dict]:
    session = getattr(request, "session", None)
    if not session:
        return None
    raw_uid = session.get("user_id")
    if raw_uid is None:
        return None
    try:
        uid = int(raw_uid)
    except (TypeError, ValueError):
        return None
    with db_session() as conn:
        row = conn.execute("SELECT id, username, is_admin FROM users WHERE id = ?", (uid,)).fetchone()
    if not row:
        return None
    return {"id": int(row["id"]), "username": row["username"], "is_admin": int(row["is_admin"])}