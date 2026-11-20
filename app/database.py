import sqlite3
from contextlib import contextmanager
from typing import Generator
from app.config import settings


def get_connection() -> sqlite3.Connection:
    settings.ensure_dirs()
    conn = sqlite3.connect(settings.database_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON;")
    return conn


@contextmanager
def db_session() -> Generator[sqlite3.Connection, None, None]:
    conn = get_connection()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _ensure_column(conn: sqlite3.Connection, table: str, column: str, col_def: str) -> None:
    cols = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    if column not in cols:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {col_def};")


def init_db() -> None:
    with db_session() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS user_profile (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                home_location TEXT NOT NULL DEFAULT '',
                home_lat REAL,
                home_lon REAL,
                max_commute_min INTEGER NOT NULL DEFAULT 30,
                max_commute_km INTEGER NOT NULL DEFAULT 20,
                min_salary INTEGER,
                cv_text TEXT NOT NULL DEFAULT '',
                cv_skills_json TEXT NOT NULL DEFAULT '[]',
                negative_patterns TEXT NOT NULL DEFAULT '',
                digest_time TEXT NOT NULL DEFAULT '07:30',
                use_jobs_cz INTEGER NOT NULL DEFAULT 0,
                use_prace_cz INTEGER NOT NULL DEFAULT 0,
                use_jenprace_cz INTEGER NOT NULL DEFAULT 0,
                use_linkedin INTEGER NOT NULL DEFAULT 0,
                use_uradprace_cz INTEGER NOT NULL DEFAULT 0,
                email_digest_enabled INTEGER NOT NULL DEFAULT 0,
                email_digest_frequency TEXT NOT NULL DEFAULT 'daily',
                email_recipient TEXT NOT NULL DEFAULT '',
                email_min_score INTEGER NOT NULL DEFAULT 50,
                min_display_score INTEGER NOT NULL DEFAULT 40,
                auto_delete_days INTEGER NOT NULL DEFAULT 30,
                last_digest_run_date TEXT NOT NULL DEFAULT '',
                last_email_digest_at TEXT,
                updated_at TEXT DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS companies (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                city TEXT DEFAULT '',
                lat REAL,
                lon REAL,
                commute_minutes INTEGER,
                commute_km REAL,
                career_url TEXT DEFAULT '',
                scraper_type TEXT NOT NULL DEFAULT 'auto',
                is_active INTEGER NOT NULL DEFAULT 1,
                is_blacklisted INTEGER NOT NULL DEFAULT 0,
                source TEXT NOT NULL DEFAULT 'user_added',
                last_status TEXT NOT NULL DEFAULT 'pending',
                last_checked_at TEXT,
                notes TEXT DEFAULT '',
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS job_offers (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                company_id INTEGER REFERENCES companies(id) ON DELETE SET NULL,
                company_name_raw TEXT NOT NULL DEFAULT '',
                title TEXT NOT NULL,
                url TEXT NOT NULL UNIQUE,
                source_type TEXT NOT NULL DEFAULT 'direct_company',
                board_name TEXT,
                is_agency INTEGER NOT NULL DEFAULT 0,
                duplicate_of_id INTEGER REFERENCES job_offers(id) ON DELETE SET NULL,
                commute_minutes INTEGER,
                commute_km REAL,
                salary_text TEXT DEFAULT '',
                salary_min INTEGER,
                description_clean TEXT DEFAULT '',
                content_hash TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'new',
                first_seen_at TEXT DEFAULT CURRENT_TIMESTAMP,
                last_seen_at TEXT DEFAULT CURRENT_TIMESTAMP,
                notified_at TEXT
            );

            CREATE TABLE IF NOT EXISTS job_evaluations (
                job_id INTEGER PRIMARY KEY REFERENCES job_offers(id) ON DELETE CASCADE,
                match_score INTEGER NOT NULL DEFAULT 0,
                skill_intersections TEXT NOT NULL DEFAULT '[]',
                pros TEXT NOT NULL DEFAULT '',
                cons TEXT NOT NULL DEFAULT '',
                cv_tips TEXT NOT NULL DEFAULT '',
                evaluated_hash TEXT NOT NULL,
                evaluated_at TEXT DEFAULT CURRENT_TIMESTAMP
            );
            """
        )
        _ensure_column(conn, "user_profile", "max_commute_km", "INTEGER NOT NULL DEFAULT 20")
        _ensure_column(conn, "user_profile", "use_jobs_cz", "INTEGER NOT NULL DEFAULT 0")
        _ensure_column(conn, "user_profile", "use_prace_cz", "INTEGER NOT NULL DEFAULT 0")
        _ensure_column(conn, "user_profile", "use_jenprace_cz", "INTEGER NOT NULL DEFAULT 0")
        _ensure_column(conn, "user_profile", "use_linkedin", "INTEGER NOT NULL DEFAULT 0")
        _ensure_column(conn, "user_profile", "use_uradprace_cz", "INTEGER NOT NULL DEFAULT 0")
        _ensure_column(conn, "user_profile", "email_digest_enabled", "INTEGER NOT NULL DEFAULT 0")
        _ensure_column(conn, "user_profile", "email_digest_frequency", "TEXT NOT NULL DEFAULT 'daily'")
        _ensure_column(conn, "user_profile", "email_recipient", "TEXT NOT NULL DEFAULT ''")
        _ensure_column(conn, "user_profile", "email_min_score", "INTEGER NOT NULL DEFAULT 50")
        _ensure_column(conn, "user_profile", "min_display_score", "INTEGER NOT NULL DEFAULT 40")
        _ensure_column(conn, "user_profile", "auto_delete_days", "INTEGER NOT NULL DEFAULT 30")
        _ensure_column(conn, "user_profile", "last_digest_run_date", "TEXT NOT NULL DEFAULT ''")
        _ensure_column(conn, "user_profile", "last_email_digest_at", "TEXT")
        _ensure_column(conn, "companies", "commute_km", "REAL")
        _ensure_column(conn, "job_offers", "commute_km", "REAL")
        _ensure_column(conn, "job_offers", "board_name", "TEXT")
        _ensure_column(conn, "job_offers", "notified_at", "TEXT")

        conn.execute(
            """
            INSERT OR IGNORE INTO user_profile (
                id, max_commute_min, max_commute_km, digest_time,
                use_jobs_cz, use_prace_cz, use_jenprace_cz, use_linkedin, use_uradprace_cz,
                email_digest_enabled, email_digest_frequency, email_recipient, email_min_score,
                min_display_score, auto_delete_days
            ) VALUES (1, 30, 20, '07:30', 0, 0, 0, 0, 0, 0, 'daily', '', 50, 40, 30);
            """
        )