import re
from pathlib import Path
from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
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
    with db_session() as conn:
        profile = conn.execute("SELECT * FROM user_profile WHERE id = 1").fetchone()
        min_display_score = (
            int(profile["min_display_score"])
            if profile and "min_display_score" in profile.keys() and profile["min_display_score"] is not None
            else 40
        )

        query = """
            SELECT j.*, e.match_score, e.skill_intersections, e.pros, e.cons, e.cv_tips,
                   c.name AS company_official_name
            FROM job_offers j
            LEFT JOIN job_evaluations e ON e.job_id = j.id
            LEFT JOIN companies c ON c.id = j.company_id
            WHERE j.status != 'hidden'
        """
        params = []
        if filter_status != "all":
            query += " AND j.status = ?"
            params.append(filter_status)
        if not show_low:
            query += " AND (e.match_score IS NULL OR e.match_score >= ?)"
            params.append(min_display_score)

        query += " ORDER BY j.is_agency ASC, COALESCE(e.match_score, 0) DESC, j.first_seen_at DESC"
        offers = conn.execute(query, params).fetchall()
    return templates.TemplateResponse(
        "jobs.html",
        {
            "request": request,
            "active_tab": "jobs",
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
async def run_jobs_now(include_boards: int = Form(0)):
    deleted_old = delete_expired_offers()
    stats = await run_scrape_cycle()
    board_added = 0
    board_errors = 0
    if include_boards:
        b_stats = await run_job_boards_cycle()
        board_added = b_stats.get("new_offers", 0)
        board_errors = b_stats.get("errors", 0)
    evaluated = await evaluate_pending_offers(limit=15)
    total_errors = stats.get("errors", 0) + board_errors
    return RedirectResponse(
        url=(
            f"/?checked={stats['companies_checked']}&added={stats['new_offers']}"
            f"&board_added={board_added}&evaluated={evaluated}&deleted_old={deleted_old}&errors={total_errors}"
        ),
        status_code=303,
    )


@router.post("/jobs/{job_id}/status")
async def update_job_status(job_id: int, status: str = Form(...)):
    if status in {"new", "interested", "applied", "hidden"}:
        with db_session() as conn:
            conn.execute("UPDATE job_offers SET status = ? WHERE id = ?", (status, job_id))
    return RedirectResponse(url="/", status_code=303)


@router.get("/companies", response_class=HTMLResponse)
async def companies_page(request: Request, suggested: int = 0, no_ai: int = 0, cleared: int = 0):
    with db_session() as conn:
        profile = conn.execute("SELECT * FROM user_profile WHERE id = 1").fetchone()
        companies = conn.execute(
            "SELECT * FROM companies WHERE is_blacklisted = 0 ORDER BY is_active DESC, commute_minutes ASC NULLS LAST, name ASC"
        ).fetchall()
    return templates.TemplateResponse(
        "companies.html",
        {
            "request": request,
            "active_tab": "companies",
            "profile": profile,
            "companies": companies,
            "suggested": suggested,
            "no_ai": no_ai,
            "cleared": cleared,
        },
    )


@router.post("/companies/clear-ai")
async def clear_ai_companies():
    with db_session() as conn:
        cur = conn.execute("DELETE FROM companies WHERE source = 'ai_suggested'")
        deleted = cur.rowcount
    return RedirectResponse(url=f"/companies?cleared={deleted}", status_code=303)


@router.post("/companies/suggest")
async def suggest_companies():
    with db_session() as conn:
        profile_row = conn.execute("SELECT * FROM user_profile WHERE id = 1").fetchone()
        existing_rows = conn.execute("SELECT LOWER(name) AS lname FROM companies").fetchall()

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
                INSERT INTO companies (name, city, lat, lon, commute_minutes, commute_km, career_url, scraper_type, is_active, source, notes)
                VALUES (?, ?, ?, ?, ?, ?, ?, 'auto', 1, 'ai_suggested', ?)
                """,
                (name, city, lat, lon, commute, commute_km, career_url, " ".join(note_parts)),
            )
        existing_names.add(name.lower())
        added_count += 1

    return RedirectResponse(url=f"/companies?suggested={added_count}", status_code=303)


@router.post("/companies/add")
async def add_company(
    name: str = Form(...),
    city: str = Form(""),
    career_url: str = Form(""),
    scraper_type: str = Form("auto"),
    notes: str = Form(""),
):
    lat, lon, commute, commute_km = None, None, None, None
    if city.strip():
        coords = await geocode_location(_normalize_geo_query(city))
        if coords:
            lat, lon = coords
            with db_session() as conn:
                profile = conn.execute("SELECT home_lat, home_lon FROM user_profile WHERE id = 1").fetchone()
                if profile and profile["home_lat"] is not None and profile["home_lon"] is not None:
                    commute, commute_km = await estimate_commute(profile["home_lat"], profile["home_lon"], lat, lon)
    with db_session() as conn:
        conn.execute(
            """
            INSERT INTO companies (name, city, lat, lon, commute_minutes, commute_km, career_url, scraper_type, source, notes)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'user_added', ?)
            """,
            (name.strip(), city.strip(), lat, lon, commute, commute_km, career_url.strip(), scraper_type, notes.strip()),
        )
    return RedirectResponse(url="/companies", status_code=303)


@router.post("/companies/{company_id}/toggle")
async def toggle_company(company_id: int):
    with db_session() as conn:
        conn.execute("UPDATE companies SET is_active = CASE WHEN is_active = 1 THEN 0 ELSE 1 END WHERE id = ?", (company_id,))
    return RedirectResponse(url="/companies", status_code=303)


@router.post("/companies/{company_id}/delete")
async def delete_company(company_id: int):
    with db_session() as conn:
        conn.execute("DELETE FROM companies WHERE id = ?", (company_id,))
    return RedirectResponse(url="/companies", status_code=303)


@router.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request, saved: int = 0, digest_sent: int = 0, cleaned_count: int = -1):
    with db_session() as conn:
        profile = conn.execute("SELECT * FROM user_profile WHERE id = 1").fetchone()
    return templates.TemplateResponse(
        "settings.html",
        {
            "request": request,
            "active_tab": "settings",
            "profile": profile,
            "saved": saved,
            "digest_sent": digest_sent,
            "cleaned_count": cleaned_count if cleaned_count >= 0 else None,
            "llm_configured": bool(settings.llm_api_key and settings.llm_api_key != "vas_api_klic_zde"),
            "llm_model": settings.llm_model,
            "smtp_configured": bool(settings.smtp_host),
        },
    )


@router.post("/settings")
async def save_settings(
    home_location: str = Form(""),
    max_commute_min: int = Form(30),
    max_commute_km: int = Form(20),
    min_salary: str = Form(""),
    digest_time: str = Form("07:30"),
    negative_patterns: str = Form(""),
    cv_text: str = Form(""),
    use_jobs_cz: str = Form(""),
    use_prace_cz: str = Form(""),
    use_jenprace_cz: str = Form(""),
    use_linkedin: str = Form(""),
    use_uradprace_cz: str = Form(""),
    email_digest_enabled: str = Form(""),
    email_digest_frequency: str = Form("daily"),
    email_recipient: str = Form(""),
    email_min_score: int = Form(50),
    min_display_score: int = Form(40),
    auto_delete_days: int = Form(30),
):
    home_lat, home_lon = None, None
    if home_location.strip():
        coords = await geocode_location(_normalize_geo_query(home_location))
        if coords:
            home_lat, home_lon = coords
    salary_val = int(min_salary) if min_salary.strip().isdigit() else None
    clean_cv = sanitize_cv_text(cv_text)
    settings.ensure_dirs()
    Path(settings.cv_path).write_text(clean_cv, encoding="utf-8")

    b_jobs = 1 if use_jobs_cz else 0
    b_prace = 1 if use_prace_cz else 0
    b_jenprace = 1 if use_jenprace_cz else 0
    b_linkedin = 1 if use_linkedin else 0
    b_uradprace = 1 if use_uradprace_cz else 0
    b_email = 1 if email_digest_enabled else 0
    freq_val = email_digest_frequency.strip().lower() if email_digest_frequency.strip().lower() in {"daily", "weekly"} else "daily"
    score_val = max(0, min(100, int(email_min_score)))
    display_score_val = max(0, min(100, int(min_display_score)))
    delete_days_val = max(0, min(365, int(auto_delete_days)))

    with db_session() as conn:
        conn.execute(
            """
            UPDATE user_profile
            SET home_location = ?, home_lat = ?, home_lon = ?, max_commute_min = ?, max_commute_km = ?,
                min_salary = ?, cv_text = ?, negative_patterns = ?, digest_time = ?,
                use_jobs_cz = ?, use_prace_cz = ?, use_jenprace_cz = ?, use_linkedin = ?, use_uradprace_cz = ?,
                email_digest_enabled = ?, email_digest_frequency = ?, email_recipient = ?, email_min_score = ?,
                min_display_score = ?, auto_delete_days = ?,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = 1
            """,
            (
                home_location.strip(),
                home_lat,
                home_lon,
                max_commute_min,
                max_commute_km,
                salary_val,
                clean_cv,
                negative_patterns.strip(),
                digest_time.strip() or "07:30",
                b_jobs,
                b_prace,
                b_jenprace,
                b_linkedin,
                b_uradprace,
                b_email,
                freq_val,
                email_recipient.strip(),
                score_val,
                display_score_val,
                delete_days_val,
            ),
        )
        if home_lat is not None and home_lon is not None:
            companies = conn.execute("SELECT id, lat, lon FROM companies WHERE lat IS NOT NULL AND lon IS NOT NULL").fetchall()
            for comp in companies:
                minutes, km = await estimate_commute(home_lat, home_lon, comp["lat"], comp["lon"])
                conn.execute("UPDATE companies SET commute_minutes = ?, commute_km = ? WHERE id = ?", (minutes, km, comp["id"]))
    return RedirectResponse(url="/settings?saved=1", status_code=303)


@router.post("/settings/cleanup-now")
async def cleanup_old_offers_now():
    deleted = delete_expired_offers()
    return RedirectResponse(url=f"/settings?cleaned_count={deleted}", status_code=303)


@router.post("/settings/test-digest")
async def test_email_digest():
    sent = await maybe_send_email_digest(force=True)
    return RedirectResponse(url=f"/settings?digest_sent={1 if sent else 0}", status_code=303)