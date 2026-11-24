import asyncio
from contextlib import asynccontextmanager
from datetime import datetime
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware
from app.auth import create_user, ensure_default_profile_for_user
from app.config import settings
from app.database import db_session, init_db
from app.services.cleanup import delete_expired_offers
from app.services.job_boards import run_job_boards_cycle
from app.services.matcher import evaluate_pending_offers
from app.services.notifier import maybe_send_email_digest
from app.services.scraper import run_scrape_cycle
from app.web.routes import router

_cycle_lock = asyncio.Lock()


def _bootstrap_users() -> None:
    if settings.default_admin_user and settings.default_admin_password:
        create_user(settings.default_admin_user, settings.default_admin_password, is_admin=1)
    with db_session() as conn:
        user_rows = conn.execute("SELECT id FROM users ORDER BY id ASC").fetchall()
    for r in user_rows:
        ensure_default_profile_for_user(int(r["id"]))


async def run_scheduled_pipeline() -> None:
    async with _cycle_lock:
        with db_session() as conn:
            user_rows = conn.execute("SELECT id FROM users ORDER BY id ASC").fetchall()
        user_ids = [int(r["id"]) for r in user_rows] if user_rows else [1]

        for uid in user_ids:
            try:
                delete_expired_offers(uid)
            except TypeError:
                delete_expired_offers()
            try:
                await run_scrape_cycle(uid)
            except TypeError:
                await run_scrape_cycle()
            try:
                await run_job_boards_cycle(user_id=uid)
            except TypeError:
                await run_job_boards_cycle()
            try:
                await evaluate_pending_offers(limit=25, user_id=uid)
            except TypeError:
                await evaluate_pending_offers(limit=25)
            try:
                await maybe_send_email_digest(force=False, user_id=uid)
            except TypeError:
                await maybe_send_email_digest(force=False)


async def _daily_digest_loop() -> None:
    while True:
        try:
            await asyncio.sleep(30)
            now = datetime.now()
            today_str = now.strftime("%Y-%m-%d")
            hhmm_str = now.strftime("%H:%M")

            with db_session() as conn:
                rows = conn.execute(
                    "SELECT user_id, digest_time, last_digest_run_date FROM user_profile"
                ).fetchall()

            due_any = False
            for row in rows:
                target_time = (row["digest_time"] or settings.default_digest_time or "07:30").strip()
                last_run_date = (row["last_digest_run_date"] or "").strip()
                if last_run_date != today_str and hhmm_str >= target_time:
                    due_any = True
                    with db_session() as conn:
                        conn.execute(
                            "UPDATE user_profile SET last_digest_run_date = ? WHERE user_id = ?",
                            (today_str, row["user_id"]),
                        )

            if due_any:
                print(
                    f"[scheduler] Spouštím plánovaný běh (aktuálně {hhmm_str}, {today_str})",
                    flush=True,
                )
                await run_scheduled_pipeline()
        except asyncio.CancelledError:
            break
        except Exception as exc:
            print(f"[scheduler] Chyba plánovače: {type(exc).__name__}: {exc}", flush=True)


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings.ensure_dirs()
    init_db()
    _bootstrap_users()
    task = asyncio.create_task(_daily_digest_loop())
    try:
        yield
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


app = FastAPI(title="AI Job Assistant", lifespan=lifespan)
app.add_middleware(
    SessionMiddleware,
    secret_key=settings.app_secret_key,
    session_cookie=settings.session_cookie_name,
    https_only=settings.session_https_only,
)
app.mount("/static", StaticFiles(directory="app/web/static"), name="static")
app.include_router(router)