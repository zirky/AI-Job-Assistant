import asyncio
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
import html
import smtplib
import sys
from typing import Any, Dict, List, Optional
from app.config import settings
from app.database import db_session


def _log(msg: str) -> None:
    print(f"[notifier] {msg}", file=sys.stdout, flush=True)


def _ensure_notified_column() -> None:
    with db_session() as conn:
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(job_offers)").fetchall()}
        if "notified_at" not in cols:
            conn.execute("ALTER TABLE job_offers ADD COLUMN notified_at TEXT;")


def _parse_iso_dt(raw: Optional[str]) -> Optional[datetime]:
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(str(raw).strip())
        if dt.tzinfo is None:
            return dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return None


def _is_email_digest_due(profile: Dict[str, Any], now_utc: datetime) -> bool:
    if not profile.get("email_digest_enabled"):
        return False
    recipient = (profile.get("email_recipient") or "").strip()
    if not recipient or "@" not in recipient:
        return False

    freq = (profile.get("email_digest_frequency") or "daily").strip().lower()
    if freq not in {"daily", "weekly"}:
        freq = "daily"

    last_sent = _parse_iso_dt(profile.get("last_email_digest_at"))
    if last_sent is None:
        return True

    if freq == "weekly":
        return (now_utc - last_sent) >= timedelta(days=6, hours=12)
    return (now_utc - last_sent) >= timedelta(hours=20)


def _fetch_digest_offers(
    uid: int,
    profile: Dict[str, Any],
    since_utc: Optional[datetime],
    force: bool,
) -> List[Dict[str, Any]]:
    _ensure_notified_column()
    min_score = int(profile.get("email_min_score") or 0)
    max_km = profile.get("max_commute_km")

    params: List[Any] = [uid, min_score]
    unnotified_sql = "" if force else " AND j.notified_at IS NULL "

    since_sql = ""
    if since_utc is not None and not force:
        since_str = since_utc.strftime("%Y-%m-%d %H:%M:%S")
        since_sql = " AND datetime(j.first_seen_at) >= datetime(?) "
        params.append(since_str)

    km_sql = ""
    if max_km is not None:
        km_sql = " AND (COALESCE(j.commute_km, c.commute_km) IS NULL OR COALESCE(j.commute_km, c.commute_km) <= (? + 10.0)) "
        params.append(float(max_km))

    with db_session() as conn:
        rows = conn.execute(
            f"""
            SELECT j.id, j.title, j.company_name_raw, j.url, j.board_name, j.source_type,
                   j.is_agency, j.commute_minutes, j.commute_km, j.first_seen_at,
                   COALESCE(e.match_score, 0) AS match_score, e.pros, e.cons
            FROM job_offers j
            LEFT JOIN companies c ON c.id = j.company_id AND c.user_id = j.user_id
            LEFT JOIN job_evaluations e ON e.job_id = j.id
            WHERE j.user_id = ?
              AND j.status != 'hidden'
              AND COALESCE(e.match_score, 0) >= ?
              {unnotified_sql}
              {since_sql}
              {km_sql}
            ORDER BY j.is_agency ASC, COALESCE(e.match_score, 0) DESC, j.first_seen_at DESC
            LIMIT 40
            """,
            params,
        ).fetchall()
    return [dict(r) for r in rows]


def _send_smtp_email(recipient: str, subject: str, plain_body: str, html_body: str) -> None:
    smtp_host = (getattr(settings, "smtp_host", "") or "").strip()
    if not smtp_host:
        raise RuntimeError("SMTP_HOST není nastaveno.")

    sender = (
        getattr(settings, "smtp_from", "")
        or getattr(settings, "smtp_user", "")
        or f"noreply@{smtp_host}"
    ).strip()
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = sender
    msg["To"] = recipient
    msg.set_content(plain_body)
    msg.add_alternative(html_body, subtype="html")

    port = int(getattr(settings, "smtp_port", 587) or 587)
    use_tls = bool(getattr(settings, "smtp_tls", True))
    smtp_user = (getattr(settings, "smtp_user", "") or "").strip()
    smtp_password = getattr(settings, "smtp_password", "") or ""

    with smtplib.SMTP(smtp_host, port, timeout=20) as server:
        server.ehlo()
        if use_tls:
            server.starttls()
            server.ehlo()
        if smtp_user:
            server.login(smtp_user, smtp_password)
        server.send_message(msg)


async def _send_digest_for_user(uid: int, force: bool = False) -> bool:
    with db_session() as conn:
        row = conn.execute("SELECT * FROM user_profile WHERE user_id = ?", (uid,)).fetchone()
        if not row:
            return False
        profile = dict(row)

    now_utc = datetime.now(timezone.utc)
    if not force and not _is_email_digest_due(profile, now_utc):
        return False

    recipient = (profile.get("email_recipient") or "").strip()
    if not recipient or "@" not in recipient:
        _log(f"E-mailový digest (user_id={uid}) je zapnutý, ale chybí platný cílový e-mail.")
        return False

    if not (getattr(settings, "smtp_host", "") or "").strip():
        _log("E-mailový digest přeskočen – není nakonfigurováno SMTP_HOST.")
        return False

    freq = (profile.get("email_digest_frequency") or "daily").strip().lower()
    if freq not in {"daily", "weekly"}:
        freq = "daily"

    last_sent = _parse_iso_dt(profile.get("last_email_digest_at"))
    if last_sent is not None:
        since_utc: Optional[datetime] = last_sent
    else:
        days_back = 7 if freq == "weekly" else 1
        since_utc = now_utc - timedelta(days=days_back)

    offers = _fetch_digest_offers(uid, profile, since_utc=since_utc, force=force)
    if not offers and not force:
        _log(f"Žádné nové nabídky pro {freq} e-mailový výpis (user_id={uid}), e-mail se neposílá.")
        return False

    label_cz = "týdenní" if freq == "weekly" else "denní"
    subject = f"AI Job Assistant – {label_cz} přehled nabídek ({len(offers)})"

    plain_lines = [
        f"AI Job Assistant – {label_cz} výpis nových nabídek",
        f"Vygenerováno: {datetime.now().strftime('%Y-%m-%d %H:%M')}",
        f"Počet nabídek: {len(offers)}",
        "",
    ]
    html_items: List[str] = []

    for o in offers:
        km_txt = f"{o['commute_km']} km" if o.get("commute_km") is not None else "km neuvedeno"
        min_txt = f" / {o['commute_minutes']} min" if o.get("commute_minutes") is not None else ""
        ag_txt = " [Agentura]" if o.get("is_agency") else ""
        src_txt = o.get("board_name") or o.get("source_type") or ""
        score_val = o.get("match_score", 0)
        title_txt = o.get("title") or "Bez názvu"
        comp_txt = o.get("company_name_raw") or "Neznámá firma"
        url_txt = o.get("url") or ""

        plain_lines.append(
            f"- [{score_val} b.] {title_txt} — {comp_txt} ({km_txt}{min_txt}, {src_txt}{ag_txt})\n  {url_txt}"
        )
        html_items.append(
            f"<li style='margin-bottom:10px;'>"
            f"<strong>[{html.escape(str(score_val))} b.] {html.escape(title_txt)}</strong> – "
            f"{html.escape(comp_txt)} "
            f"<span style='color:#555;'>({html.escape(km_txt + min_txt)}, {html.escape(src_txt)}{html.escape(ag_txt)})</span><br/>"
            f"<a href='{html.escape(url_txt)}'>{html.escape(url_txt)}</a>"
            f"</li>"
        )

    if not offers:
        plain_lines.append("Za sledované období nebyly nalezeny žádné nové odpovídající nabídky.")
        html_body = "<p>Za sledované období nebyly nalezeny žádné nové odpovídající nabídky.</p>"
    else:
        html_body = (
            f"<h3>AI Job Assistant – {html.escape(label_cz)} výpis nabídek</h3>"
            f"<p>Nalezeno nabídek: <strong>{len(offers)}</strong></p>"
            f"<ul>{''.join(html_items)}</ul>"
        )

    plain_body = "\n".join(plain_lines)

    try:
        await asyncio.to_thread(_send_smtp_email, recipient, subject, plain_body, html_body)
    except Exception as exc:
        _log(f"Chyba při odesílání e-mailu na {recipient} (user_id={uid}): {type(exc).__name__}: {exc}")
        return False

    now_utc_iso = now_utc.strftime("%Y-%m-%d %H:%M:%S")
    offer_ids = [int(o["id"]) for o in offers if o.get("id") is not None]
    with db_session() as conn:
        conn.execute(
            "UPDATE user_profile SET last_email_digest_at = ? WHERE user_id = ?",
            (now_utc_iso, uid),
        )
        if offer_ids:
            placeholders = ",".join("?" for _ in offer_ids)
            conn.execute(
                f"UPDATE job_offers SET notified_at = ? WHERE user_id = ? AND id IN ({placeholders})",
                [now_utc_iso, uid, *offer_ids],
            )
    _log(f"Odeslán {label_cz} e-mailový digest ({len(offers)} nabídek) na {recipient} (user_id={uid}).")
    return True


async def maybe_send_email_digest(force: bool = False, user_id: Optional[int] = None) -> bool:
    if user_id is not None:
        return await _send_digest_for_user(int(user_id), force=force)

    with db_session() as conn:
        user_ids = [int(r["id"]) for r in conn.execute("SELECT id FROM users ORDER BY id ASC").fetchall()]
    if not user_ids:
        user_ids = [1]

    any_sent = False
    for uid in user_ids:
        if await _send_digest_for_user(uid, force=force):
            any_sent = True
    return any_sent