from functools import lru_cache
from logging import DEBUG

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Central configuration, loaded from .env. Field names map
    case-insensitively to the same environment variable names (FILE_ROOT_PATH,
    DATABASE_URL, etc.)."""

    file_root_path: str
    date_folder_format: str = "%d-%m-%Y"
    poll_interval_seconds: int = 30
    scan_days_back: int = 1
    log_level: str = "INFO"

    # CBOS trade-upload API (Steps 2/3/4/6/7 in cbos_client.py).
    # MOCK -> MockCBOSClient (no network calls, no CBOS_BASE_URL/CBOS_LOGIN_ID needed).
    # REAL -> CBOSClient (talks to the actual CBOS host).
    cbos_mode: str = "MOCK"
    # LOG_LEVEL: str=DEBUG

    # Real CBOS connection settings - only required when cbos_mode=REAL.
    # Per EDP_Trade_Process_API_Documentation_v4.pdf, GTG/CHK calls
    # (file_process_status, get_expected_filename) live on one host, and the
    # CORE process/brokerage APIs (getNewTradeProcess, chunk upload, upload
    # settings, file entry, trigger) live on a different host. Kept as two
    # separate settings instead of one shared base URL.
    cbos_gtg_base_url: str = "http://10.167.202.234:8087"
    cbos_core_base_url: str = "http://10.167.202.164:8003"
    cbos_login_id: str = "CV0001"
    cbos_password: str = "Master#123"
    cbos_timeout_seconds: int = 30
    cbos_poll_interval_seconds: int = 2
    cbos_poll_max_attempts: int = 10

    # Step 8 in the doc ("Trigger Trade Process") always runs once per
    # segment/date, after every matched file in that batch has been
    # uploaded and registered - never per individual file.
    cbos_trigger_after_upload: bool = True

    # Step 4 file chunking (upload_file_chunks in cbos_client.py). Files are
    # read and uploaded chunk_size_kb at a time instead of loading the whole
    # file into memory; a file smaller than chunk_size_kb still uploads as a
    # single CurrentChunk=1/TotalChunks=1 call. KB-based (rather than MB) so
    # small test files can be split into multiple chunks without needing a
    # huge sample file.
    chunk_size_kb: int = 51200  # 50 MB
    cbos_chunk_retry_attempts: int = 3

    # MockCBOSClient behavior tuning - irrelevant when cbos_mode=REAL.
    cbos_mock_random_success_rate: float = 0.7  # Scenario 3: odds of success for filenames with no success/fail marker
    cbos_mock_pending_polls: int = 2            # how many file_upload_status polls stay PENDING before resolving

    # File-to-UploadID matching (see app/services/upload_matching.py). Column
    # count validation only applies to delimited text files (csv/txt); it's
    # skipped (not failed) for binary/unknown formats such as .xlsx, since
    # counting columns there needs a different reader than a plain text split.
    upload_match_validate_columns: bool = True
    upload_match_delimiter: str = ","

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
