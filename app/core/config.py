from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Central configuration, loaded from .env. Field names map
    case-insensitively to the same environment variable names (FILE_ROOT_PATH,
    DATABASE_URL, etc.)."""

    file_root_path: str
    date_folder_format: str = "%d-%m-%Y"
    log_level: str = "INFO"

    # Batch intake (docs/BATCH_HANDOFF_CONTRACT.md). Work enters ONLY via
    # POST /batches with a manifest, validated against THE schema packaged in
    # edpb-core (edpb_core.manifest) - there is no filesystem scanner.
    # The Step-8 optional-slot allowlist (completeness gate). Code-reviewed
    # YAML; see app/services/optional_slots.py.
    optional_slots_path: str = "app/config/optional_slots.yaml"

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
    # No committed defaults - MOCK mode doesn't need these; REAL mode requires
    # them from .env and CBOSClient fails fast if any are missing. Never commit
    # real hosts/credentials here (see docs/CBOS_HANDOFF_CONTRACT.md).
    cbos_gtg_base_url: str = ""
    cbos_core_base_url: str = ""
    cbos_login_id: str = ""
    cbos_password: str = ""
    cbos_timeout_seconds: int = 30            # JSON calls
    cbos_upload_timeout_seconds: int = 300    # Step 5 multipart chunk upload - much longer than JSON
    cbos_poll_interval_seconds: int = 2
    cbos_poll_max_attempts: int = 10

    # Step 7's "ipaddress" field. The API doc's example fills it with the CORE
    # host's own address (10.167.202.164), NOT the caller's - and the GUID drop
    # folder CBOS reads from lives on that server, so the field may well be about
    # where the file is rather than who sent it. We had been sending the client
    # machine's IP on that assumption, never having checked.
    #
    # Which one CBOS actually wants is an open question with their team, so this
    # is configurable rather than guessed: set it in .env to whatever they
    # confirm. Left empty, it falls back to the detected local IP, preserving
    # the previous behaviour.
    cbos_upload_ip_address: str = ""

    # Whether Step 1 (BeginFileUpload) may actually stop a batch.
    #
    # Default False - observe only: the check runs and its answer is logged, but
    # the batch proceeds regardless. Enforcing it is a way to STOP uploading, and
    # it turns on "any message except SKIP means holiday", a rule taken from one
    # line of the API doc and never yet seen from the real server. If CBOS words
    # its working-day reply differently, enforcing would halt every batch while
    # looking exactly like a quiet day with no files.
    #
    # Set true once a real BeginFileUpload response has been confirmed.
    cbos_holiday_check_enforced: bool = False

    # Bounded retry for the per-batch CBOS setup calls (reserve PROCESSID + fetch
    # upload rules). A transient blip retries instead of hot-looping forever;
    # after cbos_max_retries attempts the batch's files are routed to uploadFailed.
    cbos_max_retries: int = 2
    cbos_retry_delay_seconds: int = 2

    # Step 5 file chunking (BaseCBOSClient.upload_file in cbos_client.py). The file is
    # streamed chunk_size_kb at a time (0-indexed CurrentChunk, TotalChunks=N)
    # instead of loading it into memory whole; a file <= chunk_size_kb goes as a
    # single CurrentChunk=0/TotalChunks=1 call. KB-based so small test files can
    # still be split. Each chunk retries cbos_chunk_retry_attempts times.
    chunk_size_kb: int = 10240  # 10 MB per chunk
    cbos_chunk_retry_attempts: int = 3

    # MockCBOSClient behavior tuning - irrelevant when cbos_mode=REAL.
    cbos_mock_random_success_rate: float = 0.7  # Scenario 3: odds of success for filenames with no success/fail marker
    # Makes Step 1 answer "holiday" in MOCK mode, so the skip-the-batch branch
    # is reachable without waiting for a real market holiday.
    cbos_mock_holiday: bool = False
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


class _SettingsProxy:
    """Lazy accessor for the cached settings.

    Modules do ``from app.core.config import settings`` and read ``settings.x``
    without instantiating ``Settings`` at import time (which requires a full
    ``.env``). Every attribute access goes through the cached ``get_settings()``,
    so a test can set env vars and call ``get_settings.cache_clear()`` to have
    the change take effect - impossible when the settings object was captured
    once in a module global at import.
    """

    def __getattr__(self, name: str):
        return getattr(get_settings(), name)


settings = _SettingsProxy()
