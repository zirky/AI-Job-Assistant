import re
import secrets
from pathlib import Path
from typing import Optional
from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from app.auth import authenticate_user, create_user, get_current_user
from app.config import settings
from app.database import db_session
from app.services.cleanup import delete_expired_offers
from app.services.distance import estimate_commute, geocode_location
from app.services.job_boards import run_job_boards_cycle
from app.services.matcher import evaluate_pending_offers
from app.services.notifier import maybe_send_email_digest
from app.services.onboarding import suggest_companies_from_profile
from app.services.scraper import run_scrape_cycle

router = APIRouter()
templates = Jinja2Templates(directory="app/web/templates")


def sanitize_cv_text(raw_text: str) -> str:
    cleaned = re.sub(r"[\w.+-]+@[\w-]+\.[\w.-]+", "[EMAIL ODSTRANĚN]", raw_text)
    cleaned = re.sub(r"(\+420|\+421)?\s*\d{3}[\s-]?\d{3}[\s-]?\d{3}", "[TELEFON ODSTRANĚN]", cleaned)
    return cleaned.strip()


def _normalize_geo_query(city: str) -> str:
    c = city.strip()
    low = c.lower()
    if low in {"vítkovice", "poruba", "hrusov", "hrušov", "mariánské hory", "slezská ostrava"}:
        return f"{c}, Ostrava, Czechia"
    if low in {"pudlov", "skřečoň", "vrbice", "záblatí", "nový bohumín", "starý bohumín"}:
        return f"{c}, Bohumín, Czechia"
    return f"{c}, Czechia"


def _require_user(request: Request) -> Optional[dict]:
    return get_current_user(request)


def _user_cv_path(user_id: int) -> Path:
    if user_id == 1:
        return Path(settings.cv_path)
    base = Path(settings.cv_path)
    return base.parent / f"{base.stem}_u{user_id}{base.suffix or '.txt'}"


def _get_valid_invite(code: str) -> Optional[dict]:
    clean = (code or "").strip()
    if not clean:
        return None
    with db_session() as conn:
        row = conn.execute(
            """
            SELECT id, code, created_by_user_id, used_by_user_id, created_at, used_at
            FROM invite_codes
            WHERE code = ? AND used_by_user_id IS NULL
            """,
            (clean,),
        ).fetchone()
    return dict(row) if row else None


@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request, error: str = "", registered: int = 0):
    if get_current_user(request):
        return RedirectResponse(url="/", status_code=303)
    return templates.TemplateResponse(
        "login.html",
        {
            "request": request,
            "active_tab": "login",
            "current_user": None,
            "error": error,
            "registered": registered,
            "allow_registration": settings.allow_registration,
        },
    )


@router.post("/login")
async def login_submit(request: Request, username: str = Form(...), password: str = Form(...)):
    user = authenticate_user(username, password)
    if not user:
        return templates.TemplateResponse(
            "login.html",
            {
                "request": request,
                "active_tab": "login",
                "current_user": None,
                "error": "Neplatné uživatelské jméno nebo heslo.",
                "registered": 0,
                "allow_registration": settings.allow_registration,
            },
            status_code=401,
        )
    request.session["user_id"] = user["id"]
    return RedirectResponse(url="/", status_code=303)


@router.get("/register", response_class=HTMLResponse)
async def register_page(request: Request, invite: str = "", error: str = ""):
    if get_current_user(request):
        return RedirectResponse(url="/", status_code=303)
    clean_invite = (invite or "").strip()
    invite_valid = _get_valid_invite(clean_invite) is not None
    if not settings.allow_registration and not invite_valid:
        if clean_invite:
            return RedirectResponse(
                url="/login?error=Pozv%C3%A1nkov%C3%BD+k%C3%B3d+je+neplatn%C3%BD+nebo+ji%C5%BE+byl+pou%C5%BEit.",
                status_code=303,
            )
        return RedirectResponse(url="/login?error=Registrace+je+na+pozv%C3%A1nku.", status_code=303)

    return templates.TemplateResponse(
        "register.html",
        {
            "request": request,
            "active_tab": "register",
            "current_user": None,
            "invite_code": clean_invite,
            "error": error,
        },
    )


@router.post("/register")
async def register_submit(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    password_confirm: str = Form(...),
    invite_code: str = Form(""),
):
    clean_invite = (invite_code or "").strip()
    invite_row = _get_valid_invite(clean_invite)

    if not settings.allow_registration and not invite_row:
        return templates.TemplateResponse(
            "register.html",
            {
                "request": request,
                "active_tab": "register",
                "current_user": None,
                "invite_code": clean_invite,
                "error": "Pro registraci je vyžadován platný nepoužitý pozvánkový kód.",
            },
            status_code=400,
        )

    if password != password_confirm:
        return templates.TemplateResponse(
            "register.html",
            {
                "request": request,
                "active_tab": "register",
                "current_user": None,
                "invite_code": clean_invite,
                "error": "Hesla se neshodují.",
            },
            status_code=400,
        )

    ok, msg = create_user(username, password, is_admin=0)
    if not ok:
        return templates.TemplateResponse(
            "register.html",
            {
                "request": request,
                "active_tab": "register",
                "current_user": None,
                "invite_code": clean_invite,
                "error": msg,
            },
            status_code=400,
        )

    if invite_row:
        with db_session() as conn:
            created_user = conn.execute(
                "SELECT id FROM users WHERE LOWER(username) = LOWER(?)",
                (username.strip(),),
            ).fetchone()
            if created_user:
                conn.execute(
                    """
                    UPDATE invite_codes
                    SET used_by_user_id = ?, used_at = CURRENT_TIMESTAMP
                    WHERE id = ? AND used_by_user_id IS NULL
                    """,
                    (int(created_user["id"]), int(invite_row["id"])),
                )

    return RedirectResponse(url="/login?registered=1", status_code=303)


@router.post("/logout")
async def logout_submit(request: Request):
    request.session.clear()
    return RedirectResponse(url="/login", status_code=303)


@router.post("/invites/create")
async def create_invite_code(request: Request):
    user = _require_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=303)

    new_code = secrets.token_urlsafe(9)
    with db_session() as conn:
        conn.execute(
            "INSERT INTO invite_codes (code, created_by_user_id) VALUES (?, ?)",
            (new_code, int(user["id"])),
        )
    return RedirectResponse(url=f"/settings?new_invite={new_code}", status_code=303)


@router.get("/", response_class=HTMLResponse)
async def jobs_page(
    request: Request,
    filter_status: str = "all",
    show_low: int = 0,
    checked: int = 0,
    added: int = 0,
    board_added: int = 0,
    evaluated: int = 0,
    deleted_old: int = 0,
    errors: int = 0,
):
    user = _require_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=303)
    uid = user["id"]

    with db_session() as conn:
        profile = conn.execute("SELECT * FROM user_profile WHERE user_id = ?", (uid,)).fetchone()
        min_display_score = (
            int(profile["min_display_score"])
            if profile and "min_display_score" in profile.keys() and profile["min_display_score"] is not None
            else 40
        )
        max_commute_min = (
            int(profile["max_commute_min"])
            if profile and "max_commute_min" in profile.keys() and profile["max_commute_min"] is not None
            else None
        )
        max_commute_km = (
            float(profile["max_commute_km"])
            if profile and "max_commute_km" in profile.keys() and profile["max_commute_km"] is not None
            else None
        )

        query = """
            SELECT j.*, e.match_score, e.skill_intersections, e.pros, e.cons, e.cv_tips,
                   c.name AS company_official_name
            FROM job_offers j
            LEFT JOIN job_evaluations e ON e.job_id = j.id
            LEFT JOIN companies c ON c.id = j.company_id
            WHERE j.user_id = ? AND j.status != 'hidden'
        """
        params = [uid]
        if filter_status != "all":
            query += " AND j.status = ?"
            params.append(filter_status)
        if not show_low:
            query += " AND (e.match_score IS NULL OR e.match_score >= ?)"
            params.append(min_display_score)
        if max_commute_km is not None:
            query += " AND (COALESCE(j.commute_km, c.commute_km) IS NULL OR COALESCE(j.commute_km, c.commute_km) <= ?)"
            params.append(max_commute_km)
        if max_commute_min is not None:
            query += " AND (COALESCE(j.commute_minutes, c.commute_minutes) IS NULL OR COALESCE(j.commute_minutes, c.commute_minutes) <= ?)"
            params.append(max_commute_min)

        query += " ORDER BY j.is_agency ASC, COALESCE(e.match_score, 0) DESC, j.first_seen_at DESC"
        offers = conn.execute(query, params).fetchall()

    return templates.TemplateResponse(
        "jobs.html",
        {
            "request": request,
            "active_tab": "jobs",
            "current_user": user,
            "profile": profile,
            "offers": offers,
            "filter_status": filter_status,
            "show_low": show_low,
            "min_display_score": min_display_score,
            "scrape_summary": {
                "checked": checked,
                "added": added,
                "board_added": board_added,
                "evaluated": evaluated,
                "deleted_old": deleted_old,
                "errors": errors,
            }
            if (checked or added or board_added or evaluated or deleted_old or errors)
            else None,
        },
    )


@router.post("/jobs/run-now")
async def run_jobs_now(request: Request, include_boards: int = Form(0)):
    user = _require_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=303)
    uid = user["id"]

    try:
        deleted_old = delete_expired_offers(uid)
    except TypeError:
        deleted_old = delete_expired_offers()

    try:
        stats = await run_scrape_cycle(uid)
    except TypeError:
        stats = await run_scrape_cycle()

    board_added = 0
    board_errors = 0
    if include_boards:
        try:
            b_stats = await run_job_boards_cycle(user_id=uid)
        except TypeError:
            b_stats = await run_job_boards_cycle()
        board_added = b_stats.get("new_offers", 0)
        board_errors = b_stats.get("errors", 0)

    try:
        evaluated = await evaluate_pending_offers(limit=30, user_id=uid)
    except TypeError:
        evaluated = await evaluate_pending_offers(limit=30)

    total_errors = stats.get("errors", 0) + board_errors
    return RedirectResponse(
        url=(
            f"/?checked={stats['companies_checked']}&added={stats['new_offers']}"
            f"&board_added={board_added}&evaluated={evaluated}&deleted_old={deleted_old}&errors={total_errors}"
        ),
        status_code=303,
    )


@router.post("/jobs/{job_id}/status")
async def update_job_status(request: Request, job_id: int, status: str = Form(...)):
    user = _require_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=303)
    if status in {"new", "interested", "applied", "hidden"}:
        with db_session() as conn:
            conn.execute(
                "UPDATE job_offers SET status = ? WHERE id = ? AND user_id = ?",
                (status, job_id, user["id"]),
            )
    return RedirectResponse(url="/", status_code=303)


@router.get("/companies", response_class=HTMLResponse)
async def companies_page(request: Request, suggested: int = 0, no_ai: int = 0, cleared: int = 0):
    user = _require_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=303)
    uid = user["id"]

    with db_session() as conn:
        profile = conn.execute("SELECT * FROM user_profile WHERE user_id = ?", (uid,)).fetchone()
        companies = conn.execute(
            """
            SELECT * FROM companies
            WHERE user_id = ? AND is_blacklisted = 0
            ORDER BY is_active DESC, commute_minutes ASC NULLS LAST, name ASC
            """,
            (uid,),
        ).fetchall()

    return templates.TemplateResponse(
        "companies.html",
        {
            "request": request,
            "active_tab": "companies",
            "current_user": user,
            "profile": profile,
            "companies": companies,
            "suggested": suggested,
            "no_ai": no_ai,
            "cleared": cleared,
        },
    )


@router.post("/companies/clear-ai")
async def clear_ai_companies(request: Request):
    user = _require_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=303)
    with db_session() as conn:
        cur = conn.execute(
            "DELETE FROM companies WHERE user_id = ? AND source = 'ai_suggested'",
            (user["id"],),
        )
        deleted = cur.rowcount
    return RedirectResponse(url=f"/companies?cleared={deleted}", status_code=303)


@router.post("/companies/suggest")
async def suggest_companies(request: Request):
    user = _require_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=303)
    uid = user["id"]

    with db_session() as conn:
        profile_row = conn.execute("SELECT * FROM user_profile WHERE user_id = ?", (uid,)).fetchone()
        existing_rows = conn.execute(
            "SELECT LOWER(name) AS lname FROM companies WHERE user_id = ?",
            (uid,),
        ).fetchall()

    if not profile_row:
        return RedirectResponse(url="/companies?no_ai=1", status_code=303)

    profile_dict = dict(profile_row)
    suggestions = await suggest_companies_from_profile(profile_dict)
    if not suggestions:
        return RedirectResponse(url="/companies?no_ai=1", status_code=303)

    existing_names = {r["lname"] for r in existing_rows}
    max_commute = profile_dict.get("max_commute_min") or 30
    max_km = profile_dict.get("max_commute_km") or 20
    added_count = 0

    for item in suggestions:
        name = item["name"].strip()
        if not name or name.lower() in existing_names:
            continue
        raw_city = item["city"].strip()
        city = raw_city.split("/")[0].strip() if "/" in raw_city else raw_city
        career_url = item["career_url"].strip()
        notes = item["notes"].strip()

        lat, lon, commute, commute_km = None, None, None, None
        if city:
            coords = await geocode_location(_normalize_geo_query(city))
            if coords:
                lat, lon = coords
                if profile_dict.get("home_lat") is not None and profile_dict.get("home_lon") is not None:
                    commute, commute_km = await estimate_commute(
                        profile_dict["home_lat"], profile_dict["home_lon"], lat, lon
                    )

        if commute is not None and commute > (max_commute + 10):
            continue
        if commute_km is not None and float(commute_km) > (float(max_km) + 10.0):
            continue

        note_parts = []
        if notes:
            note_parts.append(f"[Návrh AI] {notes}")
        else:
            note_parts.append("[Návrh AI]")
        if commute_km is not None:
            note_parts.append(f"({commute_km} km)")
        if (commute is not None and commute > max_commute) or (commute_km is not None and float(commute_km) > float(max_km)):
            note_parts.append("(nad preferovaným limitem)")

        with db_session() as conn:
            conn.execute(
                """
                INSERT INTO companies (
                    user_id, name, city, lat, lon, commute_minutes, commute_km,
                    career_url, scraper_type, is_active, source, notes
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'auto', 1, 'ai_suggested', ?)
                """,
                (uid, name, city, lat, lon, commute, commute_km, career_url, " ".join(note_parts)),
            )
        existing_names.add(name.lower())
        added_count += 1

    return RedirectResponse(url=f"/companies?suggested={added_count}", status_code=303)


@router.post("/companies/add")
async def add_company(
    request: Request,
    name: str = Form(...),
    city: str = Form(""),
    career_url: str = Form(""),
    scraper_type: str = Form("auto"),
    notes: str = Form(""),
):
    user = _require_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=303)
    uid = user["id"]

    lat, lon, commute, commute_km = None, None, None, None
    if city.strip():
        coords = await geocode_location(_normalize_geo_query(city))
        if coords:
            lat, lon = coords
            with db_session() as conn:
                profile = conn.execute(
                    "SELECT home_lat, home_lon FROM user_profile WHERE user_id = ?",
                    (uid,),
                ).fetchone()
                if profile and profile["home_lat"] is not None and profile["home_lon"] is not None:
                    commute, commute_km = await estimate_commute(profile["home_lat"], profile["home_lon"], lat, lon)
    with db_session() as conn:
        conn.execute(
            """
            INSERT INTO companies (
                user_id, name, city, lat, lon, commute_minutes, commute_km,
                career_url, scraper_type, source, notes
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'user_added', ?)
            """,
            (uid, name.strip(), city.strip(), lat, lon, commute, commute_km, career_url.strip(), scraper_type, notes.strip()),
        )
    return RedirectResponse(url="/companies", status_code=303)


@router.post("/companies/{company_id}/toggle")
async def toggle_company(request: Request, company_id: int):
    user = _require_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=303)
    with db_session() as conn:
        conn.execute(
            "UPDATE companies SET is_active = CASE WHEN is_active = 1 THEN 0 ELSE 1 END WHERE id = ? AND user_id = ?",
            (company_id, user["id"]),
        )
    return RedirectResponse(url="/companies", status_code=303)


@router.post("/companies/{company_id}/delete")
async def delete_company(request: Request, company_id: int):
    user = _require_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=303)
    with db_session() as conn:
        conn.execute("DELETE FROM companies WHERE id = ? AND user_id = ?", (company_id, user["id"]))
    return RedirectResponse(url="/companies", status_code=303)


@router.get("/settings", response_class=HTMLResponse)
async def settings_page(
    request: Request,
    saved: int = 0,
    digest_sent: int = 0,
    cleaned_count: int = -1,
    new_invite: str = "",
    delete_error: str = "",
):
    user = _require_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=303)
    with db_session() as conn:
        profile = conn.execute("SELECT * FROM user_profile WHERE user_id = ?", (user["id"],)).fetchone()
        invites = conn.execute(
            """
            SELECT i.code, i.created_at, i.used_at, u.username AS used_by_username
            FROM invite_codes i
            LEFT JOIN users u ON u.id = i.used_by_user_id
            ORDER BY i.id DESC
            LIMIT 20
            """
        ).fetchall()
    return templates.TemplateResponse(
        "settings.html",
        {
            "request": request,
            "active_tab": "settings",
            "current_user": user,
            "profile": profile,
            "invites": invites,
            "new_invite": (new_invite or "").strip(),
            "saved": saved,
            "digest_sent": digest_sent,
            "cleaned_count": cleaned_count if cleaned_count >= 0 else None,
            "delete_error": (delete_error or "").strip(),
            "llm_configured": bool(settings.llm_api_key and settings.llm_api_key != "vas_api_klic_zde"),
            "llm_model": settings.llm_model,
            "smtp_configured": bool(settings.smtp_host),
        },
    )


@router.post("/settings")
async def save_settings(
    request: Request,
    home_location: str = Form(""),
    max_commute_min: int = Form(30),
    max_commute_km: int = Form(20),
    min_salary: str = Form(""),
    digest_time: str = Form("07:30"),
    is_paused: str = Form(""),
    custom_llm_api_key: str = Form(""),
    search_keywords: str = Form(""),
    negative_patterns: str = Form(""),
    cv_text: str = Form(""),
    use_jobs_cz: str = Form(""),
    use_prace_cz: str = Form(""),
    use_jenprace_cz: str = Form(""),
    use_dobraprace_cz: str = Form(""),
    use_profesia_cz: str = Form(""),
    use_kariera_cz: str = Form(""),
    use_pracezarohem_cz: str = Form(""),
    use_linkedin: str = Form(""),
    use_uradprace_cz: str = Form(""),
    email_digest_enabled: str = Form(""),
    email_digest_frequency: str = Form("daily"),
    email_recipient: str = Form(""),
    email_min_score: int = Form(50),
    min_display_score: int = Form(40),
    auto_delete_days: int = Form(30),
):
    user = _require_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=303)
    uid = user["id"]

    home_lat, home_lon = None, None
    if home_location.strip():
        coords = await geocode_location(_normalize_geo_query(home_location))
        if coords:
            home_lat, home_lon = coords
    salary_val = int(min_salary) if min_salary.strip().isdigit() else None
    clean_cv = sanitize_cv_text(cv_text)
    settings.ensure_dirs()
    _user_cv_path(uid).write_text(clean_cv, encoding="utf-8")

    b_paused = 1 if is_paused else 0
    b_jobs = 1 if use_jobs_cz else 0
    b_prace = 1 if use_prace_cz else 0
    b_jenprace = 1 if use_jenprace_cz else 0
    b_dobraprace = 1 if use_dobraprace_cz else 0
    b_profesia = 1 if use_profesia_cz else 0
    b_kariera = 1 if use_kariera_cz else 0
    b_pracezarohem = 1 if use_pracezarohem_cz else 0
    b_linkedin = 1 if use_linkedin else 0
    b_uradprace = 1 if use_uradprace_cz else 0
    b_email = 1 if email_digest_enabled else 0
    freq_val = email_digest_frequency.strip().lower() if email_digest_frequency.strip().lower() in {"daily", "weekly"} else "daily"
    score_val = max(0, min(100, int(email_min_score)))
    display_score_val = max(0, min(100, int(min_display_score)))
    delete_days_val = max(0, min(365, int(auto_delete_days)))
    custom_key_val = custom_llm_api_key.strip() or None

    with db_session() as conn:
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(user_profile)").fetchall()}
        if "custom_llm_api_key" not in cols:
            conn.execute("ALTER TABLE user_profile ADD COLUMN custom_llm_api_key TEXT;")
        if "is_paused" not in cols:
            conn.execute("ALTER TABLE user_profile ADD COLUMN is_paused INTEGER NOT NULL DEFAULT 0;")

        conn.execute(
            """
            UPDATE user_profile
            SET home_location = ?, home_lat = ?, home_lon = ?, max_commute_min = ?, max_commute_km = ?,
                min_salary = ?, cv_text = ?, search_keywords = ?, negative_patterns = ?, digest_time = ?,
                is_paused = ?,
                use_jobs_cz = ?, use_prace_cz = ?, use_jenprace_cz = ?,
                use_dobraprace_cz = ?, use_profesia_cz = ?, use_kariera_cz = ?, use_pracezarohem_cz = ?,
                use_linkedin = ?, use_uradprace_cz = ?,
                email_digest_enabled = ?, email_digest_frequency = ?, email_recipient = ?, email_min_score = ?,
                min_display_score = ?, auto_delete_days = ?, custom_llm_api_key = ?,
                updated_at = CURRENT_TIMESTAMP
            WHERE user_id = ?
            """,
            (
                home_location.strip(),
                home_lat,
                home_lon,
                max_commute_min,
                max_commute_km,
                salary_val,
                clean_cv,
                search_keywords.strip(),
                negative_patterns.strip(),
                digest_time.strip() or "07:30",
                b_paused,
                b_jobs,
                b_prace,
                b_jenprace,
                b_dobraprace,
                b_profesia,
                b_kariera,
                b_pracezarohem,
                b_linkedin,
                b_uradprace,
                b_email,
                freq_val,
                email_recipient.strip(),
                score_val,
                display_score_val,
                delete_days_val,
                custom_key_val,
                uid,
            ),
        )
        if home_lat is not None and home_lon is not None:
            companies = conn.execute(
                "SELECT id, lat, lon FROM companies WHERE user_id = ? AND lat IS NOT NULL AND lon IS NOT NULL",
                (uid,),
            ).fetchall()
            for comp in companies:
                minutes, km = await estimate_commute(home_lat, home_lon, comp["lat"], comp["lon"])
                conn.execute(
                    "UPDATE companies SET commute_minutes = ?, commute_km = ? WHERE id = ? AND user_id = ?",
                    (minutes, km, comp["id"], uid),
                )
    return RedirectResponse(url="/settings?saved=1", status_code=303)


@router.post("/settings/delete-account")
async def delete_account(
    request: Request,
    confirm_username: str = Form(""),
    confirm_permanent: str = Form(""),
):
    user = _require_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=303)

    expected_username = (user.get("username") or "").strip()
    if not confirm_permanent or confirm_username.strip() != expected_username:
        return RedirectResponse(
            url="/settings?delete_error=Pro+smaz%C3%A1n%C3%AD+mus%C3%ADte+za%C5%A1krtnout+potvrzen%C3%AD+a+p%C5%99esn%C4%9B+opsat+sv%C3%A9+u%C5%BEivatelsk%C3%A9+jm%C3%A9no.",
            status_code=303,
        )

    uid = int(user["id"])
    with db_session() as conn:
        target = conn.execute("SELECT id, is_admin FROM users WHERE id = ?", (uid,)).fetchone()
        if not target:
            return RedirectResponse(url="/login", status_code=303)
        if int(target["is_admin"] or 0) == 1:
            other_admins = conn.execute(
                "SELECT COUNT(*) AS cnt FROM users WHERE is_admin = 1 AND id != ?",
                (uid,),
            ).fetchone()
            if not other_admins or int(other_admins["cnt"] or 0) == 0:
                return RedirectResponse(
                    url="/settings?delete_error=Posledn%C3%AD+administr%C3%A1torsk%C3%BD+%C3%BA%C4%8Det+nelze+smazat.",
                    status_code=303,
                )
        conn.execute("DELETE FROM users WHERE id = ?", (uid,))

    cv_file = _user_cv_path(uid)
    try:
        if cv_file.exists() and uid != 1:
            cv_file.unlink()
    except OSError:
        pass

    request.session.clear()
    return RedirectResponse(url="/login?error=%C3%9A%C4%8Det+a+v%C5%A1echna+jeho+data+byla+smaz%C3%A1na.", status_code=303)


@router.post("/settings/cleanup-now")
async def cleanup_old_offers_now(request: Request):
    user = _require_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=303)
    try:
        deleted = delete_expired_offers(user["id"])
    except TypeError:
        deleted = delete_expired_offers()
    return RedirectResponse(url=f"/settings?cleaned_count={deleted}", status_code=303)


@router.post("/settings/test-digest")
async def test_email_digest(request: Request):
    user = _require_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=303)
    try:
        sent = await maybe_send_email_digest(force=True, user_id=user["id"])
    except TypeError:
        sent = await maybe_send_email_digest(force=True)
    return RedirectResponse(url=f"/settings?digest_sent={1 if sent else 0}", status_code=303)