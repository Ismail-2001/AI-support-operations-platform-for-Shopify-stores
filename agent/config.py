"""
Central configuration. All environment variables are read exactly once, here.
Nothing else in the codebase should call os.environ directly.
"""

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # --- Tenant (required — every deployed instance has exactly one client) ---
    TENANT_NAME: str

    # --- LLM ---
    # Provider priority: OPENROUTER_API_KEY > GROQ_API_KEY > GOOGLE_API_KEY
    # OpenRouter is the eval-validated primary. Re-run evals if you change this.
    OPENROUTER_API_KEY: SecretStr | None = None
    OPENROUTER_MODEL: str = "openai/gpt-4o-mini"
    GROQ_API_KEY: SecretStr | None = None
    GROQ_MODEL: str = "llama-3.3-70b-versatile"
    GOOGLE_API_KEY: SecretStr | None = None
    GEMINI_MODEL: str = "gemini-2.0-flash"
    ANTHROPIC_API_KEY: SecretStr | None = None
    FALLBACK_MODEL: str = "claude-haiku-4-5-20251001"

    # --- Shopify (per-store Admin API access token) ---
    SHOPIFY_SHOP_DOMAIN: str | None = None  # e.g. "my-store.myshopify.com"
    SHOPIFY_ACCESS_TOKEN: SecretStr | None = None
    SHOPIFY_API_VERSION: str = "2024-10"

    # --- Gorgias ---
    GORGIAS_DOMAIN: str | None = None  # e.g. "my-store" (becomes my-store.gorgias.com)
    GORGIAS_EMAIL: str | None = None  # login email used for Basic Auth
    GORGIAS_API_KEY: SecretStr | None = None
    GORGIAS_WEBHOOK_SECRET: str | None = None  # shared secret checked on the Gorgias webhook

    # --- Generic inbound channel webhook (WhatsApp/chat-widget/etc via /webhooks/inbound) ---
    INBOUND_WEBHOOK_SECRET: str | None = None

    # --- API security ---
    # Every /support/* endpoint EXCEPT the webhook endpoints requires this key in the
    # X-API-Key header. Webhooks use their own shared secrets instead (see above), since
    # Gorgias/Twilio/etc can't be configured with a custom auth header as easily.
    API_KEY: SecretStr | None = None
    REQUIRE_API_KEY: bool = True
    # Comma-separated list of origins allowed to call this API from a browser (your dashboard's
    # domain). Empty = no browser origins allowed (server-to-server calls are unaffected by CORS).
    ALLOWED_ORIGINS: str = ""
    # requests per minute, per client IP, for ticket-creation-type endpoints
    RATE_LIMIT_PER_MINUTE: int = 60
    # stricter limit for the refund action endpoint specifically
    REFUND_RATE_LIMIT_PER_MINUTE: int = 10
    # stricter limit for the resend-order action endpoint specifically
    RESEND_RATE_LIMIT_PER_MINUTE: int = 10

    # --- Automation policy ---
    AUTO_SEND_ENABLED: bool = False  # if False, every reply is a draft awaiting human approval
    AUTO_SEND_MIN_CONFIDENCE: float = 0.85
    AUTO_SEND_BLOCKED_CATEGORIES: str = (
        "refund,complaint,legal,other"  # comma-separated, never auto-sent
    )

    # --- Cost governance ---
    # When today's LLM spend crosses this, auto-send is force-disabled (tickets still get
    # classified/drafted, just held for human review) until a human investigates. 0 = no cap.
    DAILY_COST_CAP_USD: float = 5.0

    # --- Storage ---
    DB_PATH: str = "cs_agent.db"

    ENV: str = "development"


_DEFAULT_DB_PATH = "cs_agent.db"


def resolve_db_path(db_path: str, tenant_name: str) -> str:
    """Auto-prefix DB_PATH with the tenant name when using the default path
    and a tenant is configured — prevents copy-pasted .env files from
    accidentally sharing a database file between clients."""
    if db_path == _DEFAULT_DB_PATH and tenant_name:
        return f"cs_agent_{tenant_name}.db"
    return db_path


settings = Settings()
settings.DB_PATH = resolve_db_path(settings.DB_PATH, settings.TENANT_NAME)
