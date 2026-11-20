import asyncio
from contextlib import asynccontextmanager
from datetime import datetime
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from app.config import settings
from app.database import db_session, init_db
from app.services.cleanup import delete_expired_offers
from app.services.job_boards import run_job_boards_cycle
from app.services.matcher import evaluate_pending_offers
from app.services.notifier import maybe_send_email_digest
from app.services.scraper import run_scrape_cycle
from app.web.routes import router

_cycle_lock = asyncio.Lock()


async def run_scheduled_pipeline() -> None:
    async with _cycle_lock:
        await asyncio.to_thread(delete_expired_offers)
        await run_scrape_cycle()
        await run_job_boards_cycle()
        await evaluate_pending_offers(limit=25)
        await maybe_send_email_digest(force=False)


async def _daily_digest_loop() -> None:
    while True:
        try:
            await asyncio.sleep(30)
            now = datetime.now()
            today_str = now.strftime("%Y-%m-%d")
            hhmm_str = now.strftime("%H:%M")

            with db_session() as conn:
                row = conn.execute(
                    "SELECT digest_time, last_digest_run_date FROM user_profile WHERE id = 1"
                ).fetchone()

            if not row:
                continue

            target_time = (row["digest_time"] or settings.default_digest_time or "07:30").strip()
            last_run_date = (row["last_digest_run_date"] or "").strip()

            if last_run_date != today_str and hhmm_str >= target_time:
                with db_session() as conn:
                    conn.execute(
                        "UPDATE user_profile SET last_digest_run_date = ? WHERE id = 1",
                        (today_str,),
                    )
                print(
                    f"[scheduler] Spouštím denní běh (plánováno {target_time}, aktuálně {hhmm_str}, {today_str})",
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
app.mount("/static", StaticFiles(directory="app/web/static"), name="static")
app.include_router(router)