"""
LLM client factory and retry-with-fallback wrapper.
Primary resolved by get_llm() priority: OpenRouter > Groq > Google Gemini.
Fallback: Claude Haiku (configurable, set ANTHROPIC_API_KEY).
Adding a new provider only requires extending invoke_with_fallback's
chain-of-responsibility — the call sites stay the same.
"""

import asyncio
from typing import Any

import httpx
import structlog
from langchain_anthropic import ChatAnthropic
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_google_genai import ChatGoogleGenerativeAI, GoogleGenerativeAIEmbeddings
from langchain_groq import ChatGroq
from langchain_openai import ChatOpenAI
from tenacity import AsyncRetrying, retry_if_exception, stop_after_attempt, wait_exponential

from agent.config import settings

logger = structlog.get_logger(__name__)


def _is_transient_llm_error(exc: BaseException) -> bool:
    """Retry on network blips, timeouts, rate limits, and 5xx — never on 4xx (except 429)."""
    if isinstance(exc, httpx.TimeoutException):
        return True
    if isinstance(exc, httpx.TransportError):
        return True
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code == 429 or 500 <= exc.response.status_code < 600
    status = getattr(exc, "status_code", None)
    if isinstance(status, int):
        return status == 429 or 500 <= status < 600
    return False


def _log_llm_retry(retry_state) -> None:
    logger.warning(
        "llm_retry",
        attempt=retry_state.attempt_number,
        error=str(retry_state.outcome.exception()),
        error_type=type(retry_state.outcome.exception()).__name__,
    )


def get_llm(temperature: float = 0.0) -> BaseChatModel:
    # Whenever the provider actually running in production changes, re-run
    # `python -m evals.run_evals` and do not trust the old 15/15 result for
    # the new provider — different models have different confidence
    # calibration and prompt-injection resistance.
    if settings.OPENROUTER_API_KEY:
        return ChatOpenAI(
            model=settings.OPENROUTER_MODEL,
            api_key=settings.OPENROUTER_API_KEY.get_secret_value(),
            temperature=temperature,
            timeout=30,
            base_url="https://openrouter.ai/api/v1",
            default_headers={
                "HTTP-Referer": "https://github.com/anomalyco/cs-agent",
                "X-Title": "CS-Agent",
            },
        )
    if settings.GROQ_API_KEY:
        return ChatGroq(
            model=settings.GROQ_MODEL,
            api_key=settings.GROQ_API_KEY.get_secret_value(),
            temperature=temperature,
            timeout=30,
        )
    if settings.GOOGLE_API_KEY:
        return ChatGoogleGenerativeAI(
            model=settings.GEMINI_MODEL,
            google_api_key=settings.GOOGLE_API_KEY.get_secret_value(),
            temperature=temperature,
            timeout=30,
        )
    raise RuntimeError(
        "No LLM API key configured. Set OPENROUTER_API_KEY, GROQ_API_KEY, or GOOGLE_API_KEY "
        "in your .env file."
    )


def get_fallback_llm(temperature: float = 0.0) -> ChatAnthropic | None:
    """Return a Claude client if ANTHROPIC_API_KEY is configured, else None."""
    if not settings.ANTHROPIC_API_KEY:
        return None
    return ChatAnthropic(
        model=settings.FALLBACK_MODEL,
        anthropic_api_key=settings.ANTHROPIC_API_KEY.get_secret_value(),
        temperature=temperature,
        timeout=30,
    )


async def invoke_with_fallback(
    primary_chain: Any,
    fallback_chain: Any,
    messages: list,
    primary_model_name: str,
    fallback_model_name: str,
) -> tuple[dict[str, Any], str]:
    """Retry ``primary_chain`` up to 3 times (exponential backoff 1s/2s/4s) on transient
    errors only.  If all retries are exhausted *and* ``fallback_chain`` is available, try
    the fallback once.  Returns ``(raw_result, model_name)`` so the caller can record which
    model actually answered in the trace.

    If no fallback is configured, or the fallback also fails, the original exception from
    the last primary attempt is re-raised.
    """
    last_error: Exception | None = None

    try:
        async for attempt in AsyncRetrying(
            stop=stop_after_attempt(3),
            wait=wait_exponential(multiplier=1, min=1, max=8),
            retry=retry_if_exception(_is_transient_llm_error),
            before_sleep=_log_llm_retry,
            reraise=True,
        ):
            with attempt:
                result = await primary_chain.ainvoke(messages)
                return result, primary_model_name
    except Exception as e:
        last_error = e

    if fallback_chain is not None:
        logger.info(
            "fallback_llm_triggered",
            reason=str(last_error),
            primary_model=primary_model_name,
            fallback_model=fallback_model_name,
        )
        try:
            result = await fallback_chain.ainvoke(messages)
            return result, fallback_model_name
        except Exception as e:
            logger.error("fallback_llm_also_failed", error=str(e), model=fallback_model_name)
            raise last_error from e

    raise last_error  # type: ignore[misc]


_embeddings_client: GoogleGenerativeAIEmbeddings | None = None


def get_embeddings_client() -> GoogleGenerativeAIEmbeddings:
    global _embeddings_client
    if not settings.GOOGLE_API_KEY:
        raise RuntimeError("GOOGLE_API_KEY is not set. Add it to your .env file.")
    if _embeddings_client is None:
        _embeddings_client = GoogleGenerativeAIEmbeddings(
            model="models/gemini-embedding-001",
            google_api_key=settings.GOOGLE_API_KEY.get_secret_value(),
        )
    return _embeddings_client


async def embed_text(text: str, timeout: float = 15.0) -> list[float]:
    client = get_embeddings_client()
    return await asyncio.wait_for(client.aembed_query(text), timeout=timeout)
