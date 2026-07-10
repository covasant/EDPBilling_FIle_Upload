from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Central configuration, loaded from .env. Field names map
    case-insensitively to the same environment variable names (FILE_ROOT_PATH,
    DATABASE_URL, etc.)."""

    file_root_path: str
    date_folder_format: str = "%d-%m-%Y"
    poll_interval_minutes: int = 5
    scan_days_back: int = 1
    log_level: str = "INFO"

    # CBOS trade-upload API (Steps 2/3/4/6/7 in cbos_client.py).
    # MOCK -> MockCBOSClient (no network calls, no CBOS_BASE_URL/CBOS_LOGIN_ID needed).
    # REAL -> RealCBOSClient (talks to the actual CBOS host).
    cbos_mode: str = "MOCK"

    # Real CBOS connection settings - only required when cbos_mode=REAL. All
    # five API calls share one base URL; only the path differs per step.
    cbos_base_url: str = ""
    cbos_login_id: str = "CV0001"
    cbos_password: str = ""
    cbos_timeout_seconds: int = 30
    cbos_poll_interval_seconds: int = 2
    cbos_poll_max_attempts: int = 30

    # MockCBOSClient behavior tuning - irrelevant when cbos_mode=REAL.
    cbos_mock_random_success_rate: float = 0.7  # Scenario 3: odds of success for filenames with no success/fail marker
    cbos_mock_pending_polls: int = 2            # how many file_upload_status polls stay PENDING before resolving

    database_url: str

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )


@lru_cache
def get_settings() -> Settings:
    return Settings()
