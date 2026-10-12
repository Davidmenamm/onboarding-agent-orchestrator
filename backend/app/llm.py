"""LLM access with resilience: per-call timeout + retries, model fallback chain, circuit breaker, usage events.
Code never takes over a reasoning decision when every model fails: callers get LLMUnavailable and route to a human."""
import threading
import time
from collections.abc import Callable
from typing import Any, TypeVar

import structlog
from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.language_models import BaseChatModel
from langchain_openai import ChatOpenAI

from app.settings import settings

log = structlog.get_logger()
T = TypeVar("T")

# $ per token (OpenRouter list price for glm-5.3-flashx); used only for the cost KPI.
PRICE_IN, PRICE_OUT = 0.37e-6, 1.25e-6


class LLMUnavailable(Exception):
    pass


class CircuitBreaker:
    # ponytail: per-process breaker; a shared one (Redis/Postgres) only if many workers hammer a dead provider.
    def __init__(self, threshold: int = 5, cooldown_s: float = 60):
        self.threshold, self.cooldown_s = threshold, cooldown_s
        self.failures, self.opened_at = 0, 0.0
        self.lock = threading.Lock()

    def allow(self) -> bool:
        with self.lock:
            return self.failures < self.threshold or time.monotonic() - self.opened_at > self.cooldown_s

    def record(self, ok: bool) -> None:
        with self.lock:
            if ok:
                self.failures = 0
            else:
                self.failures += 1
                if self.failures >= self.threshold:
                    self.opened_at = time.monotonic()


breaker = CircuitBreaker()

# Tests swap this for a scripted fake model: chat_factory = lambda name: FakeModel(...)
chat_factory: Callable[[str], BaseChatModel] = lambda name: ChatOpenAI(
    model=name,
    base_url=settings().openrouter_base_url,
    api_key=settings().openrouter_api_key,  # type: ignore[arg-type]
    timeout=settings().llm_timeout_s,
    max_retries=2,
    max_tokens=2048,  # default asks for 64k and trips OpenRouter credit checks (402)
    temperature=0,
)


def models() -> list[str]:
    return [settings().llm_model, *[m for m in settings().llm_fallback_models.split(",") if m]]


class Usage(BaseCallbackHandler):
    def __init__(self):
        self.input_tokens = self.output_tokens = 0

    def on_llm_end(self, response, **kwargs: Any) -> None:
        for gens in response.generations:
            for g in gens:
                um = getattr(getattr(g, "message", None), "usage_metadata", None) or {}
                self.input_tokens += um.get("input_tokens", 0)
                self.output_tokens += um.get("output_tokens", 0)

    @property
    def cost_usd(self) -> float:
        return round(self.input_tokens * PRICE_IN + self.output_tokens * PRICE_OUT, 6)


def call(fn: Callable[[BaseChatModel], T], on_fallback: Callable[[str, str], None] | None = None) -> T:
    """Run fn(model) with the primary model, then each fallback. Raises LLMUnavailable when all fail or breaker open."""
    if not breaker.allow():
        raise LLMUnavailable("circuit open: LLM provider failing")
    errors = []
    for i, name in enumerate(models()):
        try:
            out = fn(chat_factory(name))
            breaker.record(True)
            return out
        except Exception as e:  # noqa: BLE001 - any provider/parse error -> next model
            errors.append(f"{name}: {type(e).__name__}: {e}"[:300])
            log.warning("llm.failed", model=name, error=errors[-1])
            breaker.record(False)
            if i + 1 < len(models()) and on_fallback:
                on_fallback(name, models()[i + 1])
            if not breaker.allow():
                break
    raise LLMUnavailable("; ".join(errors))
