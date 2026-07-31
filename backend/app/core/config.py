"""Application settings. Every value is environment-driven — no hard-coded config."""
from functools import lru_cache
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    # --- application -------------------------------------------------
    APP_NAME: str = "Institutional Equity Research Platform"
    APP_VERSION: str = "1.0.0"
    ENVIRONMENT: Literal["development", "staging", "production"] = "development"
    DEBUG: bool = True
    API_V1_PREFIX: str = "/api/v1"

    # --- persistence -------------------------------------------------
    # Defaults to SQLite so the stack runs with zero infrastructure;
    # Railway/production injects a Postgres DSN via env.
    DATABASE_URL: str = "sqlite+pysqlite:///./ierp.db"
    DB_ECHO: bool = False

    # --- cache -------------------------------------------------------
    REDIS_URL: str | None = None
    CACHE_TTL_SECONDS: int = 300

    # --- auth (Clerk) ------------------------------------------------
    # Clerk was Module 1's identity shim. Module 10 replaced it with a
    # first-party identity system; these remain so an existing deployment
    # keeps working, but `NATIVE_AUTH` is the supported path.
    CLERK_SECRET_KEY: str | None = None
    CLERK_PUBLISHABLE_KEY: str | None = None
    CLERK_JWT_ISSUER: str | None = None
    AUTH_DEV_MODE: bool = True

    # --- Module 10: identity, secrets, sessions -----------------------
    #: Signs every JWT. MUST be set in production — the app refuses to sign
    #: tokens with a generated key when ENVIRONMENT=production.
    SECRET_KEY: str | None = None
    #: Derives the envelope key for encrypted-at-rest tenant secrets. Falls
    #: back to SECRET_KEY so a small deployment needs one secret, not two.
    ENCRYPTION_KEY: str | None = None
    JWT_ISSUER: str = "ierp"
    ACCESS_TOKEN_TTL_SECONDS: int = 900            # 15 minutes
    REFRESH_TOKEN_TTL_SECONDS: int = 2_592_000     # 30 days
    #: Native email/OAuth/magic-link authentication. When False the platform
    #: falls back to the labelled development identity, exactly as Modules
    #: 1-9 behaved, so nothing already built stops working.
    NATIVE_AUTH: bool = False
    #: Cookie transport. Refresh tokens are httpOnly cookies; Secure is
    #: forced on in production regardless of this value.
    COOKIE_DOMAIN: str | None = None
    COOKIE_SECURE: bool = False
    COOKIE_SAMESITE: Literal["lax", "strict", "none"] = "lax"
    CSRF_ENABLED: bool = True
    #: Lock an account after this many consecutive failures.
    MAX_FAILED_LOGINS: int = 8
    LOGIN_LOCKOUT_SECONDS: int = 900

    # --- OAuth providers ----------------------------------------------
    # Absent credentials disable the provider cleanly: /auth/providers stops
    # advertising it and the button disappears. No provider is required.
    GOOGLE_CLIENT_ID: str | None = None
    GOOGLE_CLIENT_SECRET: str | None = None
    GITHUB_CLIENT_ID: str | None = None
    GITHUB_CLIENT_SECRET: str | None = None
    OAUTH_REDIRECT_BASE: str = "http://localhost:3000"

    # --- email ---------------------------------------------------------
    # With no SMTP host configured the platform uses a console transport that
    # records the message and logs the link, so verification, reset and magic
    # link all work end-to-end in development.
    SMTP_HOST: str | None = None
    SMTP_PORT: int = 587
    SMTP_USERNAME: str | None = None
    SMTP_PASSWORD: str | None = None
    SMTP_FROM: str = "no-reply@ierp.local"
    SMTP_FROM_NAME: str = "Equity Research Platform"
    EMAIL_LINK_BASE: str = "http://localhost:3000"

    # --- multi-tenancy --------------------------------------------------
    #: Tenant that owns rows written before Module 10 existed. Created by the
    #: platform seed and used by the backfill.
    DEFAULT_TENANT_SLUG: str = "demo-capital"
    #: Registering a new email creates its own organisation. Turn off for a
    #: single-tenant deployment where an admin invites everyone.
    ALLOW_SELF_SIGNUP: bool = True
    SIGNUP_DEFAULT_PLAN: str = "free"

    # --- rate limiting ---------------------------------------------------
    RATE_LIMIT_ENABLED: bool = True
    #: In-process sliding window by default; Redis when REDIS_URL is set, so
    #: the limit is shared across replicas rather than per-process.
    RATE_LIMIT_BACKEND: Literal["memory", "redis"] = "memory"

    # --- document storage (Module 7) --------------------------------
    #: "local" writes to DOCUMENT_STORAGE_PATH — a Railway Volume in
    #: production. "s3" / "r2" / "minio" use the S3-compatible client.
    #: The original upload is retained permanently: without it a re-index
    #: cannot re-parse, and a failed ingestion loses the document.
    DOCUMENT_STORAGE_BACKEND: Literal["local", "s3", "r2", "minio"] = "local"
    #: Mount path of the Railway Volume attached to the API service.
    DOCUMENT_STORAGE_PATH: str = "/data/documents"
    DOCUMENT_S3_BUCKET: str | None = None
    DOCUMENT_S3_ENDPOINT: str | None = None
    DOCUMENT_S3_REGION: str | None = None
    DOCUMENT_S3_ACCESS_KEY: str | None = None
    DOCUMENT_S3_SECRET_KEY: str | None = None
    #: Hard ceiling on a single upload. 500–1000 page annual reports with
    #: scanned plates run to a few hundred megabytes.
    DOCUMENT_MAX_UPLOAD_MB: int = 256
    #: Refuse an upload when the volume would drop below this afterwards.
    DOCUMENT_MIN_FREE_DISK_MB: int = 512
    #: Retain the source bytes when a document row is deleted. Off by
    #: default: a deliberate delete should not leave orphaned files.
    DOCUMENT_KEEP_BYTES_ON_DELETE: bool = False

    # --- observability ----------------------------------------------------
    LOG_LEVEL: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    LOG_FORMAT: Literal["json", "console"] = "console"
    METRICS_ENABLED: bool = True
    #: How long request-metric buckets and audit rows are retained.
    METRICS_RETENTION_DAYS: int = 30
    AUDIT_RETENTION_DAYS: int = 365

    # --- background jobs ---------------------------------------------------
    #: An in-process worker thread. Adequate for one instance; set False and
    #: run `python -m app.worker` when scaling out.
    WORKER_ENABLED: bool = False
    WORKER_POLL_SECONDS: float = 2.0
    WORKER_CONCURRENCY: int = 2
    WORKER_LEASE_SECONDS: int = 300
    SCHEDULER_ENABLED: bool = False

    # --- backup -------------------------------------------------------------
    BACKUP_DIR: str = "./backups"
    BACKUP_RETENTION_COUNT: int = 14

    # --- AI providers ------------------------------------------------
    # Keys are optional. With none set the platform runs normally and the AI
    # layer reports itself unavailable rather than failing requests.
    OPENROUTER_API_KEY: str | None = None
    OPENAI_API_KEY: str | None = None
    ANTHROPIC_API_KEY: str | None = None
    GEMINI_API_KEY: str | None = None
    AI_PREFERRED_PROVIDER: str | None = None
    AI_TEMPERATURE: float = 0.2
    AI_MAX_TOKENS: int = 2000
    #: Enables a deterministic offline provider for demos and tests.
    AI_MOCK_MODE: bool = True

    # --- cors --------------------------------------------------------
    CORS_ORIGINS: list[str] = Field(
        default_factory=lambda: ["http://localhost:3000", "http://127.0.0.1:3000"]
    )

    # --- domain defaults (from the workbook spec) --------------------
    BASE_CURRENCY: str = "INR"
    DISPLAY_UNIT: str = "cr"
    HISTORICAL_YEARS: int = 10
    FORECAST_YEARS: int = 10

    @property
    def is_production(self) -> bool:
        return self.ENVIRONMENT == "production"

    @property
    def auth_enabled(self) -> bool:
        """True when a real identity is required.

        Either the Module 10 native identity system, or the legacy Clerk shim.
        When neither is on, `get_current_user` returns the clearly-labelled
        development identity.
        """
        if self.NATIVE_AUTH:
            return True
        return bool(self.CLERK_SECRET_KEY) and not self.AUTH_DEV_MODE

    @property
    def cookie_secure(self) -> bool:
        """Secure cookies are mandatory in production, configurable below it
        so the stack works over plain http on localhost."""
        return True if self.is_production else self.COOKIE_SECURE

    @property
    def oauth_providers(self) -> list[str]:
        """Providers with complete credentials. Anything else is not offered
        to the user, rather than offered and failing on click."""
        available: list[str] = []
        if self.GOOGLE_CLIENT_ID and self.GOOGLE_CLIENT_SECRET:
            available.append("google")
        if self.GITHUB_CLIENT_ID and self.GITHUB_CLIENT_SECRET:
            available.append("github")
        return available

    @property
    def email_configured(self) -> bool:
        return bool(self.SMTP_HOST)

    @property
    def ai_configured(self) -> bool:
        """Is at least one live LLM provider credentialled?

        The offline provider is always present, so the analyst never errors —
        but it returns deterministic template prose, which is a materially
        different product. Reported as degraded, never as blocking.
        """
        return any((
            self.GEMINI_API_KEY, self.OPENROUTER_API_KEY,
            self.OPENAI_API_KEY, self.ANTHROPIC_API_KEY,
        ))

    def production_blocking_problems(self) -> list[str]:
        """Configuration that makes serving traffic actively *unsafe*.

        These are the conditions under which the process must not receive
        requests at all: it cannot sign a token securely, it is leaking stack
        traces, it is running the development identity where every caller is a
        super admin, or it is storing multi-tenant data in a single-writer
        file. A load balancer should take the instance out of rotation.
        """
        problems: list[str] = []
        if not self.is_production:
            return problems
        if not self.SECRET_KEY:
            problems.append("SECRET_KEY is unset — tokens cannot be signed safely.")
        if not self.ENCRYPTION_KEY and not self.SECRET_KEY:
            problems.append("ENCRYPTION_KEY is unset — stored secrets cannot be enveloped.")
        if self.DEBUG:
            problems.append("DEBUG is on in production.")
        if not self.NATIVE_AUTH and self.AUTH_DEV_MODE:
            problems.append("Development identity is active in production.")
        if self.DATABASE_URL.startswith("sqlite"):
            problems.append("SQLite is not a production database for a multi-tenant service.")
        if any(o.startswith("http://") for o in self.CORS_ORIGINS):
            problems.append("A plaintext http origin is allowed by CORS.")
        return problems

    def production_degraded_problems(self) -> list[str]:
        """Configuration that leaves a *feature* unavailable but the service
        safe to use.

        Deliberately separated from the blocking set. Without SMTP the sign-up
        verification and password-reset e-mails cannot be sent, which is a real
        gap worth reporting loudly — but every other module (financials,
        valuation, scoring, reports, portfolio) is perfectly serviceable, and
        refusing all traffic over a missing mail relay takes down the whole
        platform to protect one flow.
        """
        problems: list[str] = []
        if not self.is_production:
            return problems
        if not self.email_configured:
            problems.append("No SMTP host — verification and reset emails cannot be delivered.")
        if not self.ai_configured:
            problems.append("No AI provider key — the analyst serves offline template output.")
        return problems

    def production_readiness_problems(self) -> list[str]:
        """Every production concern, blocking and degrading alike.

        Retained as the single list used by the production-readiness report and
        the admin panel, so operators still see one complete picture.
        """
        return self.production_blocking_problems() + self.production_degraded_problems()


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
