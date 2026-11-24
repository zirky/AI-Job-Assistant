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


def _migrate_multi_user(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT NOT NULL UNIQUE,
            password_hash TEXT NOT NULL,
            is_admin INTEGER NOT NULL DEFAULT 0,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        """
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS invite_codes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            code TEXT NOT NULL UNIQUE,
            created_by_user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
            used_by_user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            used_at TEXT
        );
        """
    )

    profile_cols = {r["name"] for r in conn.execute("PRAGMA table_info(user_profile)").fetchall()}
    profile_sql_row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='user_profile'"
    ).fetchone()
    profile_sql = (profile_sql_row["sql"] or "") if profile_sql_row else ""

    if "user_id" not in profile_cols or "CHECK (id = 1)" in profile_sql:
        conn.execute("PRAGMA foreign_keys = OFF;")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS user_profile_v2 (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL UNIQUE REFERENCES users(id) ON DELETE CASCADE,
                home_location TEXT NOT NULL DEFAULT '',
                home_lat REAL,
                home_lon REAL,
                max_commute_min INTEGER NOT NULL DEFAULT 30,
                max_commute_km INTEGER NOT NULL DEFAULT 20,
                min_salary INTEGER,
                cv_text TEXT NOT NULL DEFAULT '',
                cv_skills_json TEXT NOT NULL DEFAULT '[]',
                search_keywords TEXT NOT NULL DEFAULT '',
                negative_patterns TEXT NOT NULL DEFAULT '',
                digest_time TEXT NOT NULL DEFAULT '07:30',
                use_jobs_cz INTEGER NOT NULL DEFAULT 0,
                use_prace_cz INTEGER NOT NULL DEFAULT 0,
                use_jenprace_cz INTEGER NOT NULL DEFAULT 0,
                use_dobraprace_cz INTEGER NOT NULL DEFAULT 0,
                use_profesia_cz INTEGER NOT NULL DEFAULT 0,
                use_kariera_cz INTEGER NOT NULL DEFAULT 0,
                use_pracezarohem_cz INTEGER NOT NULL DEFAULT 0,
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
                custom_llm_api_key TEXT,
                updated_at TEXT DEFAULT CURRENT_TIMESTAMP
            );
            """
        )
        existing_profiles = [dict(r) for r in conn.execute("SELECT * FROM user_profile").fetchall()]
        if existing_profiles:
            conn.execute(
                """
                INSERT OR IGNORE INTO users (id, username, password_hash, is_admin)
                VALUES (1, 'admin', 'migrated$disabled', 1)
                """
            )
            for p in existing_profiles:
                uid = int(p.get("user_id") or p.get("id") or 1)
                conn.execute(
                    """
                    INSERT OR IGNORE INTO user_profile_v2 (
                        user_id, home_location, home_lat, home_lon, max_commute_min, max_commute_km,
                        min_salary, cv_text, cv_skills_json, search_keywords, negative_patterns, digest_time,
                        use_jobs_cz, use_prace_cz, use_jenprace_cz,
                        use_dobraprace_cz, use_profesia_cz, use_kariera_cz, use_pracezarohem_cz,
                        use_linkedin, use_uradprace_cz,
                        email_digest_enabled, email_digest_frequency, email_recipient, email_min_score,
                        min_display_score, auto_delete_days, last_digest_run_date, last_email_digest_at,
                        custom_llm_api_key, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        uid,
                        p.get("home_location") or "",
                        p.get("home_lat"),
                        p.get("home_lon"),
                        p.get("max_commute_min") if p.get("max_commute_min") is not None else 30,
                        p.get("max_commute_km") if p.get("max_commute_km") is not None else 20,
                        p.get("min_salary"),
                        p.get("cv_text") or "",
                        p.get("cv_skills_json") or "[]",
                        p.get("search_keywords") or "",
                        p.get("negative_patterns") or "",
                        p.get("digest_time") or "07:30",
                        p.get("use_jobs_cz") or 0,
                        p.get("use_prace_cz") or 0,
                        p.get("use_jenprace_cz") or 0,
                        p.get("use_dobraprace_cz") or 0,
                        p.get("use_profesia_cz") or 0,
                        p.get("use_kariera_cz") or 0,
                        p.get("use_pracezarohem_cz") or 0,
                        p.get("use_linkedin") or 0,
                        p.get("use_uradprace_cz") or 0,
                        p.get("email_digest_enabled") or 0,
                        p.get("email_digest_frequency") or "daily",
                        p.get("email_recipient") or "",
                        p.get("email_min_score") if p.get("email_min_score") is not None else 50,
                        p.get("min_display_score") if p.get("min_display_score") is not None else 40,
                        p.get("auto_delete_days") if p.get("auto_delete_days") is not None else 30,
                        p.get("last_digest_run_date") or "",
                        p.get("last_email_digest_at"),
                        p.get("custom_llm_api_key"),
                        p.get("updated_at"),
                    ),
                )
        conn.execute("DROP TABLE user_profile;")
        conn.execute("ALTER TABLE user_profile_v2 RENAME TO user_profile;")
        conn.execute("PRAGMA foreign_keys = ON;")

    _ensure_column(conn, "companies", "user_id", "INTEGER NOT NULL DEFAULT 1")
    _ensure_column(conn, "job_offers", "user_id", "INTEGER NOT NULL DEFAULT 1")

    offers_sql_row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='job_offers'"
    ).fetchone()
    offers_sql = (offers_sql_row["sql"] or "") if offers_sql_row else ""
    if "url TEXT NOT NULL UNIQUE" in offers_sql:
        conn.execute("PRAGMA foreign_keys = OFF;")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS job_offers_v2 (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL DEFAULT 1 REFERENCES users(id) ON DELETE CASCADE,
                company_id INTEGER REFERENCES companies(id) ON DELETE SET NULL,
                company_name_raw TEXT NOT NULL DEFAULT '',
                title TEXT NOT NULL,
                url TEXT NOT NULL,
                source_type TEXT NOT NULL DEFAULT 'direct_company',
                board_name TEXT,
                is_agency INTEGER NOT NULL DEFAULT 0,
                duplicate_of_id INTEGER REFERENCES job_offers_v2(id) ON DELETE SET NULL,
                commute_minutes INTEGER,
                commute_km REAL,
                salary_text TEXT DEFAULT '',
                salary_min INTEGER,
                description_clean TEXT DEFAULT '',
                content_hash TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'new',
                first_seen_at TEXT DEFAULT CURRENT_TIMESTAMP,
                last_seen_at TEXT DEFAULT CURRENT_TIMESTAMP,
                notified_at TEXT,
                UNIQUE(user_id, url)
            );
            """
        )
        conn.execute(
            """
            INSERT OR IGNORE INTO job_offers_v2 (
                id, user_id, company_id, company_name_raw, title, url, source_type, board_name,
                is_agency, duplicate_of_id, commute_minutes, commute_km, salary_text, salary_min,
                description_clean, content_hash, status, first_seen_at, last_seen_at, notified_at
            )
            SELECT
                id, COALESCE(user_id, 1), company_id, company_name_raw, title, url, source_type, board_name,
                is_agency, NULL, commute_minutes, commute_km, salary_text, salary_min,
                description_clean, content_hash, status, first_seen_at, last_seen_at, notified_at
            FROM job_offers;
            """
        )
        conn.execute("DROP TABLE job_offers;")
        conn.execute("ALTER TABLE job_offers_v2 RENAME TO job_offers;")
        conn.execute("PRAGMA foreign_keys = ON;")


def init_db() -> None:
    with db_session() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT NOT NULL UNIQUE,
                password_hash TEXT NOT NULL,
                is_admin INTEGER NOT NULL DEFAULT 0,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS invite_codes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                code TEXT NOT NULL UNIQUE,
                created_by_user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
                used_by_user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                used_at TEXT
            );

            CREATE TABLE IF NOT EXISTS user_profile (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL UNIQUE REFERENCES users(id) ON DELETE CASCADE,
                home_location TEXT NOT NULL DEFAULT '',
                home_lat REAL,
                home_lon REAL,
                max_commute_min INTEGER NOT NULL DEFAULT 30,
                max_commute_km INTEGER NOT NULL DEFAULT 20,
                min_salary INTEGER,
                cv_text TEXT NOT NULL DEFAULT '',
                cv_skills_json TEXT NOT NULL DEFAULT '[]',
                search_keywords TEXT NOT NULL DEFAULT '',
                negative_patterns TEXT NOT NULL DEFAULT '',
                digest_time TEXT NOT NULL DEFAULT '07:30',
                use_jobs_cz INTEGER NOT NULL DEFAULT 0,
                use_prace_cz INTEGER NOT NULL DEFAULT 0,
                use_jenprace_cz INTEGER NOT NULL DEFAULT 0,
                use_dobraprace_cz INTEGER NOT NULL DEFAULT 0,
                use_profesia_cz INTEGER NOT NULL DEFAULT 0,
                use_kariera_cz INTEGER NOT NULL DEFAULT 0,
                use_pracezarohem_cz INTEGER NOT NULL DEFAULT 0,
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
                custom_llm_api_key TEXT,
                updated_at TEXT DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS companies (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL DEFAULT 1 REFERENCES users(id) ON DELETE CASCADE,
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
                user_id INTEGER NOT NULL DEFAULT 1 REFERENCES users(id) ON DELETE CASCADE,
                company_id INTEGER REFERENCES companies(id) ON DELETE SET NULL,
                company_name_raw TEXT NOT NULL DEFAULT '',
                title TEXT NOT NULL,
                url TEXT NOT NULL,
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
                notified_at TEXT,
                UNIQUE(user_id, url)
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
        _migrate_multi_user(conn)
        _ensure_column(conn, "user_profile", "max_commute_km", "INTEGER NOT NULL DEFAULT 20")
        _ensure_column(conn, "user_profile", "search_keywords", "TEXT NOT NULL DEFAULT ''")
        _ensure_column(conn, "user_profile", "use_jobs_cz", "INTEGER NOT NULL DEFAULT 0")
        _ensure_column(conn, "user_profile", "use_prace_cz", "INTEGER NOT NULL DEFAULT 0")
        _ensure_column(conn, "user_profile", "use_jenprace_cz", "INTEGER NOT NULL DEFAULT 0")
        _ensure_column(conn, "user_profile", "use_dobraprace_cz", "INTEGER NOT NULL DEFAULT 0")
        _ensure_column(conn, "user_profile", "use_profesia_cz", "INTEGER NOT NULL DEFAULT 0")
        _ensure_column(conn, "user_profile", "use_kariera_cz", "INTEGER NOT NULL DEFAULT 0")
        _ensure_column(conn, "user_profile", "use_pracezarohem_cz", "INTEGER NOT NULL DEFAULT 0")
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
        _ensure_column(conn, "user_profile", "custom_llm_api_key", "TEXT")
        _ensure_column(conn, "companies", "user_id", "INTEGER NOT NULL DEFAULT 1")
        _ensure_column(conn, "companies", "commute_km", "REAL")
        _ensure_column(conn, "job_offers", "user_id", "INTEGER NOT NULL DEFAULT 1")
        _ensure_column(conn, "job_offers", "commute_km", "REAL")
        _ensure_column(conn, "job_offers", "board_name", "TEXT")
        _ensure_column(conn, "job_offers", "notified_at", "TEXT")