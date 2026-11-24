import sys
from app.database import db_session


def _log(msg: str) -> None:
    print(f"[cleanup] {msg}", file=sys.stdout, flush=True)


def delete_expired_offers(user_id: int = 1, days_override: int | None = None) -> int:
    """
    Smaže z databáze neaktivní nabídky (status 'new' nebo 'hidden') daného uživatele,
    které nebyly obnoveny déle než nastavený počet dní.
    Nabídky se stavy 'interested' a 'applied' se nikdy nemažou.
    """
    uid = int(user_id)
    with db_session() as conn:
        if days_override is not None:
            days = int(days_override)
        else:
            row = conn.execute(
                "SELECT auto_delete_days FROM user_profile WHERE user_id = ?",
                (uid,),
            ).fetchone()
            if not row:
                return 0
            raw_days = row["auto_delete_days"] if "auto_delete_days" in row.keys() else 30
            days = int(raw_days) if raw_days is not None else 30

        if days <= 0:
            _log(f"Automatické mazání starých nabídek je vypnuto (user_id={uid}, auto_delete_days <= 0).")
            return 0

        modifier = f"-{days} days"
        cur = conn.execute(
            """
            DELETE FROM job_offers
            WHERE user_id = ?
              AND status IN ('new', 'hidden')
              AND datetime(COALESCE(last_seen_at, first_seen_at)) < datetime('now', ?)
            """,
            (uid, modifier),
        )
        deleted = cur.rowcount or 0

    if deleted > 0:
        _log(f"Smazáno {deleted} nabídek starších než {days} dní (user_id={uid}).")
    return deleted