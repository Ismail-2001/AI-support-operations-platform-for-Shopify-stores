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
    # Max products the KB sync job will index per run (paginated at 250/page upstream).
    PRODUCT_SYNC_LIMIT: int = 1000

    # --- Gorgias ---
    GORGIAS_DOMAIN: str | None = None  # e.g. "my-store" (becomes my-store.gorgias.com)
    GORGIAS_EMAIL: str | None = None  # login email used for Basic Auth
    GORGIAS_API_KEY: SecretStr | None = None
    GORGIAS_WEBHOOK_SECRET: str | None = None  # shared secret checked on the Gorgias webhook

    # --- Subscriptions (Recharge / Skio) ---
    # Recharge admin REST API — header X-Recharge-Access-Token.
    # https://developer.rechargepayments.com
    RECHARGE_API_TOKEN: SecretStr | None = None
    # Skio GraphQL API — header "authorization: API <token>".
    # https://code.skio.com
    SKIO_API_TOKEN: SecretStr | None = None
    # Which provider manages this store's subscriptions: "auto" (first connected wins:
    # recharge then skio), "recharge", or "skio". Set explicitly when both are connected.
    SUBSCRIPTION_PROVIDER: str = "auto"
    # Recharge exposes no pause endpoint — pause is implemented as "push the next
    # charge out by N days", the same reschedule mechanism the Recharge portal uses.
    SUBSCRIPTION_PAUSE_DAYS: int = 30

    # --- Return labels (ShipEngine) ---
    # https://docs.shipstation.com/apis/shipengine — header API-Key.
    SHIPENGINE_API_KEY: SecretStr | None = None
    # Return window measured from the order's created_at date.
    RETURN_WINDOW_DAYS: int = 30
    # Where prepaid return labels ship TO (the store's return address). Required for
    # rate quotes and label purchase — checked at request time, not at boot.
    RETURN_ADDRESS_NAME: str | None = None
    RETURN_ADDRESS1: str | None = None
    RETURN_ADDRESS2: str | None = None
    RETURN_CITY: str | None = None
    RETURN_STATE: str | None = None
    RETURN_ZIP: str | None = None
    RETURN_COUNTRY: str = "US"
    RETURN_PHONE: str | None = None
    # Fallback package dimensions for rate estimates when the order has no weight data.
    RETURN_PACKAGE_WEIGHT_OZ: float = 16.0
    RETURN_PACKAGE_LENGTH_IN: float = 10.0
    RETURN_PACKAGE_WIDTH_IN: float = 8.0
    RETURN_PACKAGE_HEIGHT_IN: float = 4.0

    # --- Generic inbound channel webhook (WhatsApp/chat-widget/etc via /webhooks/inbound) ---
    INBOUND_WEBHOOK_SECRET: str | None = None

    # --- Operational alerting ---
    # Optional Slack-compatible webhook URL. We POST {"text": ...} when a circuit
    # opens, a webhook lands in the dead-letter queue, or the daily cost cap is
    # exceeded. Empty = alerting is a no-op (every event still goes to logs).
    ALERT_WEBHOOK_URL: str | None = None

    # --- Storefront chat widget (public /chat endpoints) ---
    # Publishable store key the embeddable widget sends to identify this install.
    # It is NOT a secret that guards money — it only lets a storefront open chat
    # sessions (rate-limited per IP). Auto-generated into app_settings on first
    # use if unset; rotate via POST /support/widget/key.
    WIDGET_KEY: SecretStr | None = None
    # Per-IP message/minute limit for the public /chat endpoints.
    CHAT_RATE_LIMIT_PER_MINUTE: int = 20
    # CORS origin regex for the widget. Default ".*": /chat is public-by-publishable-key
    # and every /support endpoint still requires the private API key, which a foreign
    # page cannot read. Set to your storefront origin(s) to tighten it.
    WIDGET_ALLOWED_ORIGINS: str = ".*"

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
    # stricter limit for the cancel-order / edit-address action endpoints
    ACTION_RATE_LIMIT_PER_MINUTE: int = 10

    # --- Automation policy ---
    AUTO_SEND_ENABLED: bool = False  # if False, every reply is a draft awaiting human approval
    AUTO_SEND_MIN_CONFIDENCE: float = 0.85
    AUTO_SEND_BLOCKED_CATEGORIES: str = (
        # comma-separated, never auto-sent. "subscription" is blocked because the reply
        # can't verify provider state — pauses/cancels always carry a suggested_action
        # (which is independently hard-blocked in graph.decide_auto_send).
        "refund,complaint,legal,other,subscription"
    )
    # Per-category confidence floors — the global AUTO_SEND_MIN_CONFIDENCE above is the
    # fallback for categories not listed here. Tunable per tenant via .env, no code change.
    # Calibration data (see /support/analytics/auto-send) suggests where to set these;
    # they can also be overridden at runtime via PUT /support/automation/thresholds.
    AUTO_SEND_MIN_CONFIDENCE_ORDER_STATUS: float = 0.88
    AUTO_SEND_MIN_CONFIDENCE_SHIPPING: float = 0.88
    AUTO_SEND_MIN_CONFIDENCE_PRODUCT_QUESTION: float = 0.90
    AUTO_SEND_MIN_CONFIDENCE_RETURNS: float = 0.87
    AUTO_SEND_MIN_CONFIDENCE_TECHNICAL: float = 0.90
    AUTO_SEND_MIN_CONFIDENCE_DEFAULT: float = 0.90

    # --- Cost governance ---
    # When today's LLM spend crosses this, auto-send is force-disabled (tickets still get
    # classified/drafted, just held for human review) until a human investigates. 0 = no cap.
    DAILY_COST_CAP_USD: float = 5.0

    # --- Conversation history window ---
    # How many of the MOST RECENT messages get loaded into the LLM prompt per graph
    # run. Keeps token usage and latency flat on long-running tickets instead of
    # growing without bound. 0 = unlimited (full history, previous behaviour).
    # The operator transcript endpoints always return the full thread regardless.
    MAX_HISTORY_MESSAGES: int = 40

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
