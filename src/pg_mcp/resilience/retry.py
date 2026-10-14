"""Retry module with exponential backoff.

This module provides a configurable retry executor used by the query
orchestrator and other components that need resilient retries around
transient failures (LLM calls, validation feedback loops, etc.).
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Generic, TypeVar

from pg_mcp.config.settings import ResilienceConfig

logger = logging.getLogger(__name__)

T = TypeVar("T")

__all__ = ["RetryExecutor"]


class RetryExecutor(Generic[T]):
    """Execute async callables with exponential backoff retries.

    The executor combines the `max_retries`, `retry_delay`, `backoff_factor`
    and `retry_enabled` knobs from `ResilienceConfig` into a small, reusable
    component, so call sites don't re-implement backoff loops.

    Attributes:
        max_retries: Maximum number of retries after the first attempt.
        retry_delay: Base delay (seconds) before the first retry.
        backoff_factor: Multiplier applied to the delay for each attempt.
        retry_enabled: Master switch; when disabled, `execute` runs the
            callable exactly once and propagates any raised error.

    Example:
        >>> retry = RetryExecutor(config)
        >>> sql = await retry.execute(
        ...     generator.generate,
        ...     retryable=lambda e: isinstance(e, LLMError),
        ... )
    """

    def __init__(self, config: ResilienceConfig) -> None:
        """Initialize retry executor from resilience configuration.

        Args:
            config: Resilience configuration with retry parameters.
        """
        self.max_retries = config.max_retries
        self.retry_delay = config.retry_delay
        self.backoff_factor = config.backoff_factor
        self.retry_enabled = config.retry_enabled

    def _compute_delay(self, attempt: int) -> float:
        """Compute wait time (seconds) before the given retry attempt.

        Args:
            attempt: Zero-based retry attempt number (0 = first retry).

        Returns:
            float: Delay in seconds: `retry_delay * backoff_factor ** attempt`.
        """
        return self.retry_delay * (self.backoff_factor**attempt)

    async def execute(
        self,
        func: Callable[..., Awaitable[T]],
        *args: object,
        retryable: Callable[[Exception], bool] | None = None,
        on_retry: Callable[[Exception, int], None] | None = None,
        **kwargs: object,
    ) -> T:
        """Run `func` with exponential backoff retries.

        Args:
            func: Async callable to execute.
            *args: Positional arguments passed to `func`.
            retryable: Predicate deciding whether an exception should be
                retried. Defaults to retrying any exception.
            on_retry: Optional callback invoked before each retry with
                (exception, retry_attempt_number). Useful for logging or
                feeding error feedback into the next attempt.
            **kwargs: Keyword arguments passed to `func`.

        Returns:
            The result of the first successful `func` invocation.

        Raises:
            Exception: The last exception raised by `func` when it is not
                retryable, or when all attempts are exhausted.
        """
        if not self.retry_enabled:
            return await func(*args, **kwargs)

        last_error: Exception
        for attempt in range(self.max_retries + 1):
            try:
                return await func(*args, **kwargs)
            except Exception as e:
                last_error = e
                can_retry = retryable(e) if retryable is not None else True

                if attempt >= self.max_retries or not can_retry:
                    logger.debug(
                        "Retry executor gave up",
                        extra={"attempt": attempt + 1, "error": str(e)},
                    )
                    raise

                if on_retry is not None:
                    on_retry(e, attempt)

                delay = self._compute_delay(attempt)
                logger.warning(
                    "Retrying after transient failure",
                    extra={"attempt": attempt + 1, "delay": delay, "error": str(e)},
                )
                await asyncio.sleep(delay)

        # Unreachable: the loop always returns or raises.
        raise last_error  # pragma: no cover
