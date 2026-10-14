"""Unit tests for the retry module (exponential backoff executor)."""

from typing import Any

import pytest

from pg_mcp.config.settings import ResilienceConfig
from pg_mcp.resilience.retry import RetryExecutor


def make_config(max_retries: int, retry_delay: float = 0.1) -> ResilienceConfig:
    """Build a ResilienceConfig with fast delays for testing."""
    return ResilienceConfig(
        max_retries=max_retries,
        retry_delay=retry_delay,
        backoff_factor=2.0,
        retry_enabled=True,
    )


class TestRetryExecutor:
    """Tests for RetryExecutor exponential backoff behavior."""

    @pytest.mark.asyncio
    async def test_success_first_attempt_no_retry(self) -> None:
        """A callable succeeding on first attempt runs exactly once."""
        executor = RetryExecutor(make_config(max_retries=3))
        calls: list[int] = []

        async def ok() -> str:
            calls.append(1)
            return "done"

        result = await executor.execute(ok)
        assert result == "done"
        assert len(calls) == 1

    @pytest.mark.asyncio
    async def test_retries_until_success(self) -> None:
        """Transient failures are retried until the callable succeeds."""
        executor = RetryExecutor(make_config(max_retries=3))

        calls: list[int] = []

        async def flaky() -> str:
            calls.append(1)
            if len(calls) < 3:
                raise RuntimeError("transient")
            return "ok"

        result = await executor.execute(flaky)
        assert result == "ok"
        assert len(calls) == 3

    @pytest.mark.asyncio
    async def test_raises_after_all_retries_exhausted(self) -> None:
        """When all attempts fail, the last exception propagates."""
        executor = RetryExecutor(make_config(max_retries=2))

        calls: list[int] = []

        async def always_fails() -> None:
            calls.append(1)
            raise RuntimeError("boom")

        with pytest.raises(RuntimeError, match="boom"):
            await executor.execute(always_fails)

        # initial attempt + max_retries
        assert len(calls) == 3

    @pytest.mark.asyncio
    async def test_retryable_predicate_skips_retry(self) -> None:
        """Non-retryable exceptions propagate immediately without retries."""
        executor = RetryExecutor(make_config(max_retries=5))
        calls: list[int] = []

        async def fails_permanently() -> None:
            calls.append(1)
            raise ValueError("fatal")

        with pytest.raises(ValueError, match="fatal"):
            await executor.execute(fails_permanently, retryable=lambda e: False)

        assert len(calls) == 1

    @pytest.mark.asyncio
    async def test_on_retry_receives_error_and_attempt(self) -> None:
        """The on_retry callback receives the error and the retry number."""
        executor = RetryExecutor(make_config(max_retries=2))

        calls: list[int] = []
        seen: list[tuple[str, int]] = []

        async def flaky() -> str:
            calls.append(1)
            if len(calls) < 2:
                raise RuntimeError("first-fail")
            return "ok"

        def on_retry(error: Exception, attempt: int) -> None:
            seen.append((str(error), attempt))

        result = await executor.execute(flaky, on_retry=on_retry)
        assert result == "ok"
        assert seen == [("first-fail", 0)]

    @pytest.mark.asyncio
    async def test_exponential_backoff_delays(self) -> None:
        """Delay doubles with the backoff factor between retry attempts."""
        executor = RetryExecutor(make_config(max_retries=2, retry_delay=0.1))
        delays: list[float] = []

        async def always_fails() -> None:
            raise RuntimeError("x")

        original_compute = executor._compute_delay

        def spy_delay(attempt: int) -> float:
            d = original_compute(attempt)
            delays.append(d)
            return 0.0  # keep test fast

        executor._compute_delay = spy_delay  # type: ignore[method-assign]
        try:
            with pytest.raises(RuntimeError, match="x"):
                await executor.execute(always_fails)
        finally:
            executor._compute_delay = original_compute  # type: ignore[method-assign]

        # exponential: retry_delay * backoff^attempt
        assert delays == [0.1, 0.2]

    @pytest.mark.asyncio
    async def test_retry_disabled_runs_once(self) -> None:
        """When retry_enabled=False the callable runs once with no retries."""
        config = make_config(max_retries=3)
        config.retry_enabled = False
        executor = RetryExecutor(config)
        calls: list[int] = []

        async def fails() -> None:
            calls.append(1)
            raise RuntimeError("no-retry")

        with pytest.raises(RuntimeError, match="no-retry"):
            await executor.execute(fails)

        assert len(calls) == 1

    @pytest.mark.asyncio
    async def test_passes_args_and_kwargs(self) -> None:
        """Positional and keyword arguments are forwarded to the callable."""
        executor = RetryExecutor(make_config(max_retries=1))

        async def add(a: int, b: int = 0) -> int:
            return a + b

        result = await executor.execute(add, 2, b=3)
        assert result == 5


class TestRetryExecutorFromResilienceConfig:
    """RetryExecutor reads retry knobs from ResilienceConfig."""

    def test_reads_config_values(self) -> None:
        config = ResilienceConfig(
            max_retries=4,
            retry_delay=0.5,
            backoff_factor=3.0,
            retry_enabled=False,
        )
        executor = RetryExecutor(config)
        assert executor.max_retries == 4
        assert executor.retry_delay == 0.5
        assert executor.backoff_factor == 3.0
        assert executor.retry_enabled is False

    def test_compute_delay(self) -> None:
        config = ResilienceConfig(retry_delay=1.0, backoff_factor=2.0)
        executor = RetryExecutor(config)
        cast: dict[str, Any] = {}
        assert executor._compute_delay(0) == pytest.approx(1.0)
        assert executor._compute_delay(2) == pytest.approx(4.0)
        assert not cast
