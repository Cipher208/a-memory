from __future__ import annotations

"""
Middleware Pipeline — chain of handlers for intercepting and modifying requests.
Analogous to middleware_pipeline.py from ariel.
"""

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class MiddlewareContext:
    """Context passed through the pipeline."""

    tool_name: str = ""
    user_id: str = "default"
    args: dict = field(default_factory=dict)
    result: Any = None
    metadata: dict = field(default_factory=dict)
    start_time: float = 0.0
    blocked: bool = False
    block_reason: str = ""


MiddlewareNext = Callable[[MiddlewareContext], Any]


class Middleware:
    """Base middleware class."""

    name: str = "base"

    async def process(self, ctx: MiddlewareContext, next: MiddlewareNext) -> Any:
        return await next(ctx)


class RateLimitMiddleware(Middleware):
    """Rate limiting for requests."""

    name = "rate_limit"

    def __init__(self, max_per_minute: int = 100):
        self._max = max_per_minute
        self._requests: dict[str, list[float]] = {}

    async def process(self, ctx: MiddlewareContext, next: MiddlewareNext) -> Any:
        now = time.time()
        user_requests = self._requests.setdefault(ctx.user_id, [])
        user_requests[:] = [t for t in user_requests if now - t < 60]

        if len(user_requests) >= self._max:
            ctx.blocked = True
            ctx.block_reason = f"Rate limit exceeded ({self._max}/min)"
            logger.warning(f"Rate limit hit for user {ctx.user_id}")
            return {"error": ctx.block_reason}

        user_requests.append(now)
        return await next(ctx)


class AuditMiddleware(Middleware):
    """Audit logging for all calls."""

    name = "audit"

    async def process(self, ctx: MiddlewareContext, next: MiddlewareNext) -> Any:
        ctx.start_time = time.time()
        logger.info(f"Tool call: {ctx.tool_name} (user={ctx.user_id})")

        result = await next(ctx)

        elapsed = time.time() - ctx.start_time
        logger.info(f"Tool completed: {ctx.tool_name} in {elapsed:.3f}s")
        ctx.metadata["elapsed"] = elapsed
        return result


class ImportanceGateMiddleware(Middleware):
    """Noise filter using ImportanceScorer — only passes important messages."""

    def __init__(
        self,
        min_length: int = 15,
        threshold: float = 0.3,
        technical_weight: float = 0.3,
        question_weight: float = 0.2,
        scorer=None,
        memory_kind_hint: str | None = None,
    ):
        self._min_length = min_length
        self._threshold = threshold
        self._technical_weight = technical_weight
        self._question_weight = question_weight
        self.memory_kind_hint = memory_kind_hint

        if scorer is None:
            from shared.importance import ImportanceScorer

            self._scorer = ImportanceScorer()
        else:
            self._scorer = scorer

    async def process(self, ctx: MiddlewareContext, next: MiddlewareNext) -> Any:
        if ctx.tool_name not in (
            "memory_user_remember",
            "memory_agent_remember",
            "memory_user_episode_save",
            "memory_agent_episode_save",
        ):
            return await next(ctx)

        text = ctx.args.get("text", ctx.args.get("value", ctx.args.get("summary", "")))

        kind = ctx.args.get("memory_kind", self.memory_kind_hint)
        signals = self._scorer.score(text=text, kind=kind, is_technical_context=self._technical_weight > 0)
        score = signals.total()

        ctx.metadata["importance_signals"] = signals

        # Dynamic threshold integration
        from shared.adaptive import adaptive_threshold

        threshold = await adaptive_threshold.get_threshold()
        await adaptive_threshold.update(score)

        from shared.metrics import metrics

        metrics.memory_ops_total.labels(action=ctx.tool_name, layer=ctx.user_id).inc()

        if score < threshold:
            ctx.blocked = True
            ctx.block_reason = f"below_importance_threshold({score:.2f}, threshold={threshold:.2f})"
            metrics.importance_filtered_total.labels(reason="below_threshold").inc()
            return ctx

        ctx.args["importance"] = score
        return await next(ctx)

    def calculate_score(self, text: str) -> float:
        """Calculate importance score using ImportanceScorer."""
        signals = self._scorer.score(text=text, kind=self.memory_kind_hint)
        return signals.total()


class ValidationMiddleware(Middleware):
    """Parameter validation."""

    name = "validation"

    async def process(self, ctx: MiddlewareContext, next: MiddlewareNext) -> Any:
        if not ctx.user_id:
            ctx.blocked = True
            ctx.block_reason = "user_id is required"
            return {"error": ctx.block_reason}

        if ctx.tool_name in ("memory_user_remember", "memory_agent_remember") and not ctx.args.get("key"):
            ctx.blocked = True
            ctx.block_reason = "key is required"
            return {"error": ctx.block_reason}

        return await next(ctx)


class DedupMiddleware(Middleware):
    """Request deduplication."""

    name = "dedup"

    def __init__(self, window_seconds: int = 5):
        self._window = window_seconds
        self._recent: dict[str, float] = {}

    async def process(self, ctx: MiddlewareContext, next: MiddlewareNext) -> Any:
        now = time.time()
        key = f"{ctx.user_id}:{ctx.tool_name}:{sorted(ctx.args.items())!s}"

        if key in self._recent and now - self._recent[key] < self._window:
            ctx.metadata["deduped"] = True
            return {"status": "deduped"}

        self._recent[key] = now
        return await next(ctx)


class MiddlewarePipeline:
    """Middleware chain."""

    def __init__(self):
        self._middlewares: list[Middleware] = []

    def add(self, middleware: Middleware) -> MiddlewarePipeline:
        self._middlewares.append(middleware)
        return self

    async def execute(self, ctx: MiddlewareContext, handler: Callable) -> Any:
        async def _run(index: int, ctx: MiddlewareContext) -> Any:
            if index >= len(self._middlewares):
                result = handler(ctx)
                if hasattr(result, "__await__"):
                    return await result
                return result
            return await self._middlewares[index].process(ctx, lambda c: _run(index + 1, c))

        return await _run(0, ctx)

    def list_middlewares(self) -> list[str]:
        return [m.name for m in self._middlewares]


# Default pipeline
default_pipeline = MiddlewarePipeline()
default_pipeline.add(ValidationMiddleware())
default_pipeline.add(RateLimitMiddleware())
default_pipeline.add(ImportanceGateMiddleware())
default_pipeline.add(AuditMiddleware())
default_pipeline.add(DedupMiddleware())
