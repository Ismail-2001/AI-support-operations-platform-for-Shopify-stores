"""Context-aware proxy for module-level singletons (store, knowledge base, agent).

A ContextProxy forwards every attribute access to the object returned by its
resolver. The resolver reads the ``X-Store-Id`` ContextVar set by the request
middleware, so ``from agent.storage import store`` keeps working while each
request transparently operates on the store selected for that request.

With no store context (background tasks, webhooks, tests, single-store
deploys) the resolver returns None and the proxy forwards to the default
singleton — existing behavior is preserved exactly.

Resolvers are callables that must be cheap and synchronous: the middleware
has already awaited per-store initialization before the ContextVar is set,
so resolvers only read in-memory caches.
"""

from collections.abc import Callable
from typing import Any, TypeVar

T = TypeVar("T")


class ContextProxy:
    """Forwards attribute access/set to the resolved target, else the default."""

    def __init__(self, default: T, resolver: Callable[[], T | None]):
        object.__setattr__(self, "_default", default)
        object.__setattr__(self, "_resolver", resolver)

    def _target(self) -> T:
        target: T | None = object.__getattribute__(self, "_resolver")()
        if target is None:
            return object.__getattribute__(self, "_default")
        return target

    def __getattr__(self, name: str) -> Any:
        return getattr(self._target(), name)

    def __setattr__(self, name: str, value: Any) -> None:
        setattr(self._target(), name, value)

    def __delattr__(self, name: str) -> None:
        delattr(self._target(), name)

    def __repr__(self) -> str:
        return f"<ContextProxy default={object.__getattribute__(self, '_default')!r}>"
