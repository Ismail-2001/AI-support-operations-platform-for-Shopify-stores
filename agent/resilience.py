"""Outbound-integration resilience: circuit breakers + decorator wiring.

A circuit breaker stops us from hammering a dead upstream: after
`failure_threshold` consecutive transient failures the circuit opens and calls
fail fast (CircuitOpenError) for `reset_timeout` seconds, then a single trial
call is let through (half-open) - success closes the circuit, failure reopens
it. This turns "every webhook hangs 15s x 3 retries x N tickets" into an
instant, observable 503 while Shopify/Gorgias is down.

Breakers are process-wide per integration (not per store): the failure being
detected is the UPSTREAM service's health, which all tenants share. States are
exposed on /health and /support/health; alerting wires onto `on_open`.
"""

import functools
import threading
import time
from collections.abc import Callable
from typing import Any

import structlog

logger = structlog.get_logger(__name__)

CLOSED = "closed"
OPEN = "open"
HALF_OPEN = "half_open"


class CircuitOpenError(RuntimeError):
    """Raised instead of calling an upstream whose circuit is open.

    Exception handlers map this to 503 CIRCUIT_OPEN so clients can retry later
    instead of waiting out the upstream timeout."""

    def __init__(self, name: str):
        super().__init__(f"Circuit for {name!r} is open")
        self.name = name


class CircuitBreaker:
    def __init__(
        self,
        name: str,
        failure_threshold: int = 5,
        reset_timeout: float = 30.0,
        *,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.name = name
        self.failure_threshold = failure_threshold
        self.reset_timeout = reset_timeout
        self._clock = clock
        self._lock = threading.Lock()
        self._state = CLOSED
        self._failures = 0
        self._opened_at = 0.0
        self._trial_in_flight = False
        # Alerting hooks this (agent/alerting) - called OUTSIDE the lock.
        self.on_open: Callable[[str], None] | None = None

    @property
    def state(self) -> str:
        with self._lock:
            return self._state

    def before_call(self) -> None:
        """Raise CircuitOpenError if the circuit won't allow this call.
        Open circuits past their timeout flip to half-open and let exactly one
        trial call through; everyone else waits for its verdict."""
        with self._lock:
            if self._state == CLOSED:
                return
            if self._state == OPEN:
                if self._clock() - self._opened_at >= self.reset_timeout:
                    self._state = HALF_OPEN
                    self._trial_in_flight = True
                    return
                raise CircuitOpenError(self.name)
            # HALF_OPEN: only the single trial in flight may proceed.
            if self._trial_in_flight:
                raise CircuitOpenError(self.name)
            self._trial_in_flight = True

    def record_success(self) -> None:
        with self._lock:
            previous, self._state = self._state, CLOSED
            self._failures = 0
            self._trial_in_flight = False
        if previous != CLOSED:
            logger.info("circuit_closed", circuit=self.name)

    def record_failure(self) -> None:
        fire_open_hook = False
        with self._lock:
            if self._state == HALF_OPEN:
                # Trial call failed - reopen immediately, full timeout again.
                self._state = OPEN
                self._opened_at = self._clock()
                self._trial_in_flight = False
                fire_open_hook = True
            else:
                self._failures += 1
                if self._state == CLOSED and self._failures >= self.failure_threshold:
                    self._state = OPEN
                    self._opened_at = self._clock()
                    fire_open_hook = True
        if fire_open_hook:
            logger.warning(
                "circuit_opened",
                circuit=self.name,
                failures=self._failures,
                reset_timeout_seconds=self.reset_timeout,
            )
            if self.on_open:
                try:
                    self.on_open(self.name)
                except Exception:
                    logger.error("circuit_on_open_hook_failed", circuit=self.name)

    def record_neutral(self) -> None:
        """The call failed for a reason that says nothing about upstream health
        (our own config/validation error). State unchanged, but any in-flight
        half-open trial must be released so the circuit can't wedge shut."""
        with self._lock:
            self._trial_in_flight = False

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "state": self._state,
                "failures": self._failures,
                "failure_threshold": self.failure_threshold,
                "reset_timeout_seconds": self.reset_timeout,
            }

    def reset(self) -> None:
        """Test helper: back to a clean closed circuit."""
        with self._lock:
            self._state = CLOSED
            self._failures = 0
            self._trial_in_flight = False
            self._opened_at = 0.0


_BREAKERS: dict[str, CircuitBreaker] = {}


def register_breaker(breaker: CircuitBreaker) -> CircuitBreaker:
    _BREAKERS[breaker.name] = breaker
    if breaker.on_open is None:
        from agent.alerting import alert_on_circuit_open

        breaker.on_open = alert_on_circuit_open
    return breaker


def circuit_snapshots() -> dict[str, dict[str, Any]]:
    """All registered breakers' states - surfaced on the health endpoints."""
    return {name: breaker.snapshot() for name, breaker in sorted(_BREAKERS.items())}


def guarded(breaker: CircuitBreaker, is_failure: Callable[[Exception], bool]):
    """Guard an HTTP-calling coroutine with a circuit breaker.

    Stacked ABOVE tenacity's @retry (outermost): `before_call` short-circuits
    before any retry sleep, and one record_* call reflects the LOGICAL call's
    final outcome (after retries). Exceptions `is_failure` rejects are neutral
    (our config/validation problems must not open the circuit)."""

    def decorator(fn):
        @functools.wraps(fn)
        async def wrapper(*args, **kwargs):
            breaker.before_call()
            try:
                result = await fn(*args, **kwargs)
            except Exception as exc:
                if is_failure(exc):
                    breaker.record_failure()
                else:
                    breaker.record_neutral()
                raise
            breaker.record_success()
            return result

        return wrapper

    return decorator
