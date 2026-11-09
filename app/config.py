from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    """Jetriever's fixed retrieval and service configuration."""

    database_path: Path
    openrouter_api_key: str
    service_api_key: str
    jev_url: str
    jev_model: str
    jev_timeout_seconds: float
    jev_concurrency: int
    jev_max_retries: int
    backend_retry_after_seconds: int
    result_limit: int
    retention_days: int
    audit_log_path: Path = Path("./log/audit.jsonl")
    audit_log_max_bytes: int = 100 * 1024 * 1024
    audit_log_backups: int = 20

    @classmethod
    def from_env(cls) -> Settings:
        settings = cls(
            database_path=Path(os.getenv("DATABASE_PATH", "./data/jetriever.sqlite3")),
            openrouter_api_key=os.getenv("OPENROUTER_API_KEY", "").strip(),
            service_api_key=os.getenv("JETRIEVER_API_KEY", "").strip(),
            jev_url=os.getenv(
                "JEV_URL", "https://openrouter.ai/api/alpha/decisions"
            ).strip(),
            jev_model=os.getenv("JEV_MODEL", "~typesafe/jev-latest").strip(),
            jev_timeout_seconds=float(os.getenv("JEV_TIMEOUT_SECONDS", "20")),
            jev_concurrency=int(os.getenv("JEV_CONCURRENCY", "32")),
            jev_max_retries=int(os.getenv("JEV_MAX_RETRIES", "5")),
            backend_retry_after_seconds=int(
                os.getenv("BACKEND_RETRY_AFTER_SECONDS", "15")
            ),
            result_limit=int(os.getenv("JETRIEVER_RESULT_LIMIT", "5")),
            retention_days=int(os.getenv("RETENTION_DAYS", "30")),
            audit_log_path=Path(os.getenv("AUDIT_LOG_PATH", "./log/audit.jsonl")),
            audit_log_max_bytes=int(
                os.getenv("AUDIT_LOG_MAX_BYTES", str(100 * 1024 * 1024))
            ),
            audit_log_backups=int(os.getenv("AUDIT_LOG_BACKUPS", "20")),
        )
        if not settings.jev_url:
            raise ValueError("JEV_URL cannot be empty")
        if not settings.jev_model:
            raise ValueError("JEV_MODEL cannot be empty")
        if settings.jev_timeout_seconds <= 0:
            raise ValueError("JEV_TIMEOUT_SECONDS must be positive")
        if settings.jev_concurrency < 1:
            raise ValueError("JEV_CONCURRENCY must be at least 1")
        if settings.jev_max_retries < 1:
            raise ValueError("JEV_MAX_RETRIES must be at least 1")
        if settings.backend_retry_after_seconds < 1:
            raise ValueError("BACKEND_RETRY_AFTER_SECONDS must be at least 1")
        if settings.result_limit != 5:
            raise ValueError(
                "JETRIEVER_RESULT_LIMIT is fixed at 5 for the submitted version"
            )
        if settings.retention_days < 1 or settings.retention_days > 30:
            raise ValueError("RETENTION_DAYS must be between 1 and 30")
        if settings.audit_log_max_bytes < 1:
            raise ValueError("AUDIT_LOG_MAX_BYTES must be positive")
        if settings.audit_log_backups < 1:
            raise ValueError("AUDIT_LOG_BACKUPS must be positive")
        return settings
