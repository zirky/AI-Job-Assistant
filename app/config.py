import secrets
from pathlib import Path
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    app_host: str = "0.0.0.0"
    app_port: int = 8080
    app_secret_key: str = secrets.token_urlsafe(32)
    session_cookie_name: str = "jobs_assistant_session"
    session_https_only: bool = False
    allow_registration: bool = False
    default_admin_user: str = ""
    default_admin_password: str = ""

    database_path: str = "data/jobs_assistant.db"
    cv_path: str = "data/cv_clean.txt"
    llm_api_base: str = "https://openrouter.ai/api/v1"
    llm_api_key: str = ""
    llm_model: str = "qwen/qwen-2.5-72b-instruct:free"
    gemini_api_key: str = ""
    gemini_model: str = "gemini-3.1-flash-lite"
    llm_fallback_models: str = ""
    ollama_base_url: str = ""
    ollama_model: str = "qwen3:1.7b"
    default_digest_time: str = "07:30"
    ntfy_topic_url: str = ""

    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    smtp_from: str = ""
    smtp_tls: bool = True

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    def ensure_dirs(self) -> None:
        Path(self.database_path).parent.mkdir(parents=True, exist_ok=True)
        Path(self.cv_path).parent.mkdir(parents=True, exist_ok=True)


settings = Settings()