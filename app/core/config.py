from functools import lru_cache

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


def reveal(value: "SecretStr | str | None") -> str:
    """The plain text behind a setting, whether or not it is a SecretStr.

    Credential fields are SecretStr so that a stray `logger.debug(settings)`, a
    ValidationError traceback, or an error-tracking integration that serialises locals
    renders `**********` instead of the password — failing closed rather than open.

    Use this at every read. `SecretStr("")` is TRUTHY, so the fail-fast "is this
    configured?" checks in the clients (`if not getattr(settings, name)`) silently stop
    working the moment a field becomes a SecretStr — they would build a REAL client with
    an empty password instead of refusing. Going through here keeps those checks honest.
    """
    if value is None:
        return ""
    return value.get_secret_value() if isinstance(value, SecretStr) else str(value)


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

    # Cap on batches waiting for the worker. Sized well above the handful of real
    # segment/date combinations, so it should never bind in normal operation - it is a
    # backstop that turns "intake silently outruns the worker" into a 503 someone can
    # see. 0 disables the bound.
    batch_queue_maxsize: int = 256

    # How long shutdown waits for the queue worker to finish its current batch. A batch
    # can legitimately take minutes, so this is a bound on the WAIT, not a promise the
    # batch will finish — the thread is a daemon and the process exits regardless.
    worker_shutdown_grace_seconds: float = 30.0

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
    cbos_password: SecretStr = SecretStr("")
    cbos_timeout_seconds: int = 30  # JSON calls
    cbos_upload_timeout_seconds: int = 300  # Step 5 multipart chunk upload - much longer than JSON
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

    # Checksum verification (manifest_service.verify_checksums) opt-out, keyed
    # by "EXCHANGE:kind" pairs matching a manifest file's "exchange"/"kind"
    # metadata (e.g. "MCX:product_master"). Listed files still must exist, but
    # neither size_bytes nor sha256 are compared - for slow-changing reference
    # files (product masters etc.) whose declared checksum may go stale between
    # manifest generation and upload. Comma-separated in .env:
    # CHECKSUM_SKIP_KINDS=MCX:product_master,EQ:product_master
    checksum_skip_kinds: str = ""

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
    cbos_mock_random_success_rate: float = (
        0.7  # Scenario 3: odds of success for filenames with no success/fail marker
    )
    # Makes Step 1 answer "holiday" in MOCK mode, so the skip-the-batch branch
    # is reachable without waiting for a real market holiday.
    cbos_mock_holiday: bool = False
    cbos_mock_pending_polls: int = (
        2  # how many file_upload_status polls stay PENDING before resolving
    )

    # File-to-UploadID matching (see app/services/upload_matching.py). Column
    # count validation only applies to delimited text files (csv/txt); it's
    # skipped (not failed) for binary/unknown formats such as .xlsx, since
    # counting columns there needs a different reader than a plain text split.
    upload_match_validate_columns: bool = True
    upload_match_delimiter: str = ","

    # Send CBOS the filename DATE it asks for (Step 40's DateBasis), when the exchange
    # stamped the file a different day. Default ON because without it those uploads are
    # rejected outright — "FILE NAME TRADE DATE(T-1) MISMATCH". A kill switch rather than
    # an opt-in: the rewrite asserts something about the file's CONTENTS that cannot be
    # checked here (see app/services/cbos_filename.py), so there has to be a way to stop
    # it without a deploy. The file on disk is never renamed either way.
    cbos_rewrite_upload_filename_date: bool = True

    database_url: str

    # ---- Settlement segment (DP File Upload API) --------------------------
    # A second, unrelated upstream ("DP upload master" system, see
    # DP_FileUpload_API_Integration_Guide docx) sharing this same FastAPI app
    # and .env. Auth is a Session-Value header (seskey|user_id), not
    # LOGINID/PASSWORD-in-body like CBOS trade-upload above. One call =
    # one file; the whole 7-step flow (getdetailsuploadmaster -> validate ->
    # chunk upload -> finalize -> poll -> conditional process) runs
    # synchronously inside a single POST /settlements/uploads, since the
    # settlement orchestrator (a separate service) owns scheduling/retry and
    # expects a call-and-get-result contract, not a queued job.
    cbos_setl_mode: str = "MOCK"

    # Where the settlement file-download bot drops files before the
    # orchestrator calls POST /settlements/uploads with just a file_name -
    # this service looks the file up here rather than receiving bytes.
    cbos_setl_shared_folder_path: str = ""

    # Real DP upload API connection settings - only required when
    # cbos_setl_mode=REAL. The two source docs disagree on host/prefix
    # (gateway :44300 + /api/dp/upload/ vs. an observed :8002 host mixing
    # /api/dp/upload/ and /v1/api/dp/upload/) - left blank pending
    # confirmation, no default guessed.
    cbos_setl_base_url: str = ""
    cbos_setl_api_prefix: str = ""

    # Session-Value header: "<seskey>|<user_id>". Static config for now
    # (mirrors cbos_login_id/cbos_password above) - whether seskey needs its
    # own login/refresh call is unconfirmed.
    cbos_setl_seskey: SecretStr = SecretStr("")
    cbos_setl_user_id: str = ""
    cbos_setl_created_by: str = ""

    cbos_setl_timeout_seconds: int = 30  # JSON calls
    cbos_setl_upload_timeout_seconds: int = 300  # chunk upload - longer than JSON

    cbos_setl_poll_interval_seconds: int = 5
    cbos_setl_poll_max_attempts: int = 60

    cbos_setl_max_retries: int = 2
    cbos_setl_retry_delay_seconds: int = 2

    # Chunked upload (uploadchunks, Step 4). Doc specifies a hard 5MB/chunk
    # limit and 512MB total request size - unlike billing's CHUNK_SIZE_KB,
    # this isn't a free tuning knob, just made configurable rather than
    # hardcoded.
    chunk_setl_size_kb: int = 5120  # 5 MB per chunk

    # MockDPUploadClient tuning - irrelevant when cbos_setl_mode=REAL.
    cbos_setl_mock_random_success_rate: float = 0.9
    cbos_setl_mock_pending_polls: int = 2

    # ---- NSDL SPEED-e settlement upload -----------------------------------
    # A THIRD upstream, unrelated to both of the above: the "NSDL Speedy" file
    # upload API (see app/clients/nsdl_speede_client.py). Different host,
    # different endpoint names, no auth header at all - LOGINID travels in the
    # body. It ingests the 12 Margin Pledge reports the SPEED-e download bot
    # pulls from eservices.nsdl.com.
    nsdl_speede_mode: str = "MOCK"

    nsdl_speede_base_url: str = ""  # e.g. http://10.167.202.164:8009
    # The API doc says /api/settlement; UAT answers 404 there and 200 on
    # /v1/api/settlement (confirmed 2026-08-06), which is also the prefix the
    # settlement automation workflow.json uses against this same host. Same
    # /api vs /v1/api disagreement the DP upload docs have.
    nsdl_speede_api_prefix: str = "/v1/api/settlement"

    # Travels in the body as LOGINID (call 1) / LOGINID (call 4) - this API has
    # no session header, unlike the DP upload API above.
    nsdl_speede_login_id: str = ""
    nsdl_speede_group_name: str = "NSDL"

    # ROOT of where the SPEED-e download bot drops its files - not the day's
    # folder. The bot creates one dated folder per run
    # (<NSDL_SPEEDE_DOWNLOAD_DIR>/nsdl_speede_<DDMMYYYY>/, see the download
    # repo's src/portals/nsdl_speede/run.py), so this must be its parent and
    # the day is appended per request.
    nsdl_speede_shared_folder_path: str = ""

    # The dated sub-folder, strftime-formatted from the request's trade_date.
    # Must stay in step with the bot's naming; both sides derive it from the
    # same trade_date, so they agree without either knowing the other. Set to
    # "" to read files straight out of the root (useful when uploading a folder
    # assembled by hand).
    nsdl_speede_date_folder_format: str = "nsdl_speede_%d%m%Y"

    nsdl_speede_timeout_seconds: int = 60
    nsdl_speede_upload_timeout_seconds: int = 600  # chunk PUTs; 58MB files observed

    nsdl_speede_poll_interval_seconds: int = 5
    nsdl_speede_poll_max_attempts: int = 60

    # SaveSettlementPromodalUploadChunkFile. Their own UI was observed at ~11
    # chunks for a 37MB file; 5MB is within that and matches the DP API's cap.
    nsdl_speede_chunk_size_kb: int = 5120

    # Every SPEED-e export carries exactly one header row (confirmed against
    # real samples for all 4 report types) and the upload API has no
    # server-side validate call, so both of these are ours to enforce.
    nsdl_speede_strip_header: bool = True
    nsdl_speede_validate_columns: bool = True

    # No SPEED-e export ends with a line terminator, and CBOS drops the final
    # unterminated line: UPLOADID 24 loaded 72,921 rows from a file holding
    # 72,922 (UAT, 2026-08-06, TRANID 339086). Sending a closing newline is
    # what a normal CSV writer would emit anyway. Set false only if a load is
    # ever found to gain a phantom empty row.
    nsdl_speede_append_trailing_newline: bool = True

    # MockNsdlSpeedeClient tuning - irrelevant when nsdl_speede_mode=REAL.
    nsdl_speede_mock_pending_polls: int = 1

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
