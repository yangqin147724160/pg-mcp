"""Query orchestrator for coordinating the complete query flow.

This module provides the QueryOrchestrator class that coordinates all components
of the query processing pipeline: SQL generation, validation, execution, and result
validation. It wires the resilience primitives (retry executor, circuit breaker,
rate limiters), observability (metrics, request tracing) and token accounting
into a single execution path.
"""

import logging
import time
from typing import Any

from asyncpg import Pool

from pg_mcp.cache.schema_cache import SchemaCache
from pg_mcp.config.settings import ResilienceConfig, ValidationConfig
from pg_mcp.models.errors import (
    DatabaseError,
    ErrorCode,
    LLMError,
    PgMcpError,
    RateLimitExceededError,
    SchemaLoadError,
    SecurityViolationError,
    SQLParseError,
)
from pg_mcp.models.query import (
    ErrorDetail,
    QueryRequest,
    QueryResponse,
    QueryResult,
    ReturnType,
    ValidationResult,
)
from pg_mcp.observability.metrics import MetricsCollector
from pg_mcp.observability.tracing import request_context
from pg_mcp.resilience.circuit_breaker import CircuitBreaker
from pg_mcp.resilience.rate_limiter import RateLimiter
from pg_mcp.resilience.retry import RetryExecutor
from pg_mcp.services.result_validator import ResultValidator
from pg_mcp.services.sql_executor import SQLExecutor
from pg_mcp.services.sql_generator import SQLGenerator
from pg_mcp.services.sql_validator import SQLValidator

logger = logging.getLogger(__name__)


class QueryOrchestrator:
    """Orchestrates the complete query processing pipeline.

    This class coordinates SQL generation, validation, execution, and result
    validation across multiple databases. Every request is traced with a
    request ID (via `request_context`), measured with Prometheus metrics
    (via `MetricsCollector`), rate limited (query + LLM slots) and made
    fault tolerant through the retry executor and circuit breaker.

    Example:
        >>> orchestrator = QueryOrchestrator(
        ...     sql_generator=generator,
        ...     sql_validator=validator,
        ...     sql_executor=executor,
        ...     result_validator=result_validator,
        ...     schema_cache=cache,
        ...     pools={"mydb": pool},
        ...     resilience_config=resilience_config,
        ...     validation_config=validation_config,
        ...     rate_limiters={"query": limiter, "llm": limiter_llm},
        ...     metrics=metrics,
        ... )
        >>> response = await orchestrator.execute_query(QueryRequest(
        ...     question="How many users?",
        ...     database="mydb"
        ... ))
    """

    def __init__(
        self,
        sql_generator: SQLGenerator,
        sql_validator: SQLValidator,
        sql_executor: SQLExecutor,
        result_validator: ResultValidator,
        schema_cache: SchemaCache,
        pools: dict[str, Pool],
        resilience_config: ResilienceConfig,
        validation_config: ValidationConfig,
        rate_limiters: dict[str, RateLimiter] | None = None,
        metrics: MetricsCollector | None = None,
    ) -> None:
        """Initialize query orchestrator.

        Args:
            sql_generator: SQL generation service.
            sql_validator: SQL validation service.
            sql_executor: SQL execution service.
            result_validator: Result validation service.
            schema_cache: Schema cache instance.
            pools: Dictionary mapping database names to connection pools.
            resilience_config: Resilience configuration for retries,
                exponential backoff, circuit breaker and rate limits.
            validation_config: Validation configuration including thresholds.
            rate_limiters: Optional rate limiters keyed by slot name
                ("query", "llm", "default").
            metrics: Optional Prometheus metrics collector used to record
                request counters, latencies and token usage.
        """
        self.sql_generator = sql_generator
        self.sql_validator = sql_validator
        self.sql_executor = sql_executor
        self.result_validator = result_validator
        self.schematicache = schema_cache
        self.pools = pools
        self.rate_limiters = rate_limiters
        self.resilience_config = resilience_config
        self.validation_config = validation_config
        self.metrics = metrics

        # Circuit breaker protecting the LLM pipeline (generation + validation)
        self.circuit_breaker = CircuitBreaker(
            failure_threshold=resilience_config.circuit_breaker_threshold,
            recovery_timeout=resilience_config.circuit_breaker_timeout,
        )

        # Retry executor providing exponential backoff for the LLM pipeline
        self.retry_executor: RetryExecutor[Any] = RetryExecutor(resilience_config)

    async def execute_query(self, request: QueryRequest) -> QueryResponse:
        """Execute complete query flow from question to results (traced).

        This method orchestrates the entire pipeline inside a tracing
        `request_context` and holds the query rate-limiter slot for the
        whole execution:

        1. Generate request_id via request tracing context
        2. Resolve and validate database name
        3. Load schema from cache
        4. Generate and validate SQL with retry logic + circuit breaker
        5. Execute SQL (if return_type == RESULT)
        6. Validate results (optional)
        7. Return structured response

        Args:
            request: Query request containing question and parameters.

        Returns:
            QueryResponse: Complete response with SQL, results, or error information.

        Example:
            >>> response = await orchestrator.execute_query(
            ...     QueryRequest(question="Count all users", return_type="result")
            ... )
            >>> if response.success:
            ...     print(f"Found {response.data.row_count} rows")
        """
        async with request_context() as request_id:
            logger.info(
                "Starting query execution",
                extra={"request_id": request_id, "question": request.question[:100]},
            )

            # Hold the query-level rate limiter slot for the entire request.
            query_limiter = (self.rate_limiters or {}).get("query")
            if query_limiter is not None:
                acquired = await query_limiter.acquire()
                if not acquired:
                    self._record_request_metric("rate_limited", None)
                    raise RateLimitExceededError(
                        message="Too many concurrent queries, try again later",
                        details={"limiter": "query"},
                    )
            else:
                acquired = False

            try:
                return await self._execute_query_traced(request, request_id)
            finally:
                if query_limiter is not None and acquired:
                    query_limiter.release()

    async def _execute_query_traced(
        self, request: QueryRequest, request_id: str
    ) -> QueryResponse:
        """Run the query pipeline inside an established tracing context."""
        start_time = time.monotonic()
        resolved_db: str | None = None

        try:
            # Step 1: Resolve database name
            database_name = self._resolve_database(request.database)
            resolved_db = database_name
            logger.debug(
                "Resolved database",
                extra={"request_id": request_id, "database": database_name},
            )

            # Step 2: Get schema from cache
            schema = self.schematicache.get(database_name)
            if schema is None:
                # Schema not in cache, load it
                pool = self.pools.get(database_name)
                if pool is None:
                    raise DatabaseError(
                        message=f"No connection pool available for database '{database_name}'",
                        details={"database": database_name},
                    )
                try:
                    schema = await self.schematicache.load(database_name, pool)
                except Exception as e:
                    raise SchemaLoadError(
                        message=f"Failed to load schema for database '{database_name}': {e!s}",
                        details={"database": database_name, "error": str(e)},
                    ) from e

            logger.debug(
                "Schema loaded",
                extra={
                    "request_id": request_id,
                    "database": database_name,
                    "tables": len(schema.tables),
                },
            )

            # Step 3: Generate and validate SQL with retry logic
            generated_sql, validation_result, tokens_used = (
                await self._generate_sql_with_retry(
                    question=request.question,
                    schema=schema,
                    request_id=request_id,
                )
            )

            # Step 4: If return_type is SQL, return early
            if request.return_type == ReturnType.SQL:
                logger.info(
                    "Returning SQL only",
                    extra={"request_id": request_id, "sql_length": len(generated_sql)},
                )
                self._record_request_metric("success_sql_only", database_name)
                self._observe_total_duration(start_time)
                return QueryResponse(
                    success=True,
                    generated_sql=generated_sql,
                    validation=validation_result,
                    data=None,
                    error=None,
                    confidence=100,
                    tokens_used=tokens_used,
                )

            # Step 5: Execute SQL
            logger.debug("Executing SQL", extra={"request_id": request_id})
            execution_start = time.monotonic()

            results, total_count = await self.sql_executor.execute(generated_sql)

            execution_time_ms = (time.monotonic() - execution_start) * 1000
            logger.info(
                "SQL executed successfully",
                extra={
                    "request_id": request_id,
                    "row_count": total_count,
                    "execution_time_ms": execution_time_ms,
                },
            )
            if self.metrics is not None:
                self.metrics.observe_db_query_duration(execution_time_ms / 1000)

            # Step 6: Validate results (non-blocking, failures don't fail the request)
            result_confidence = await self._validate_results_safely(
                question=request.question,
                sql=generated_sql,
                results=results,
                row_count=total_count,
                request_id=request_id,
            )

            # Step 7: Build successful response
            query_result = QueryResult(
                columns=list(results[0].keys()) if results else [],
                rows=results,
                row_count=len(results),  # Limited row count (after max_rows applied)
                execution_time_ms=execution_time_ms,
            )

            self._record_request_metric("success", database_name)
            self._observe_total_duration(start_time)

            return QueryResponse(
                success=True,
                generated_sql=generated_sql,
                validation=validation_result,
                data=query_result,
                error=None,
                confidence=result_confidence,
                tokens_used=tokens_used,
            )

        except PgMcpError as e:
            # Handle known application errors
            logger.warning(
                "Query execution failed with known error",
                extra={
                    "request_id": request_id,
                    "error_code": e.code,
                    "error_message": str(e),
                },
            )
            self._record_request_metric("error", resolved_db)
            self._observe_total_duration(start_time)
            if e.code in (ErrorCode.SECURITY_VIOLATION, ErrorCode.SQL_PARSE_ERROR):
                self._record_sql_rejected(e.code.value)
            return QueryResponse(
                success=False,
                generated_sql=None,
                validation=None,
                data=None,
                error=ErrorDetail(
                    code=e.code.value,
                    message=e.message,
                    details=e.details,
                ),
                confidence=0,
                tokens_used=None,
            )
        except Exception as e:
            # Handle unexpected errors
            logger.exception(
                "Query execution failed with unexpected error",
                extra={"request_id": request_id},
            )
            self._record_request_metric("error", resolved_db)
            self._observe_total_duration(start_time)
            return QueryResponse(
                success=False,
                generated_sql=None,
                validation=None,
                data=None,
                error=ErrorDetail(
                    code=ErrorCode.INTERNAL_ERROR.value,
                    message=f"Internal server error: {e!s}",
                    details={"error_type": type(e).__name__},
                ),
                confidence=0,
                tokens_used=None,
            )

    def _resolve_database(self, database: str | None) -> str:
        """Resolve database name from request or auto-select.

        If database is specified, validate it exists.
        If not specified and only one database available, auto-select it.

        Args:
            database: Database name from request (optional).

        Returns:
            str: Resolved database name.

        Raises:
            DatabaseError: If database is invalid or cannot be auto-selected.

        Example:
            >>> name = orchestrator._resolve_database("mydb")  # Validates "mydb" exists
            >>> name = orchestrator._resolve_database(None)  # Auto-selects if only one DB
        """
        if database is not None:
            # Validate specified database exists
            if database not in self.pools:
                raise DatabaseError(
                    message=f"Database '{database}' not found",
                    details={
                        "requested_database": database,
                        "available_databases": list(self.pools.keys()),
                    },
                )
            return database

        # Auto-select if only one database available
        available_dbs = list(self.pools.keys())
        if len(available_dbs) == 0:
            raise DatabaseError(
                message="No databases configured",
                details={},
            )
        if len(available_dbs) == 1:
            return available_dbs[0]

        # Multiple databases, must specify
        raise DatabaseError(
            message="Multiple databases available, please specify which to query",
            details={"available_databases": available_dbs},
        )

    async def _generate_sql_with_retry(
        self,
        question: str,
        schema: Any,
        request_id: str,
    ) -> tuple[str, ValidationResult, int | None]:
        """Generate and validate SQL with retry, circuit breaker and rate limiter.

        The LLM generation + validation pipeline is wrapped in:
        - the LLM rate limiter slot (`rate_limiters["llm"]`),
        - the `RetryExecutor` (exponential backoff; retries only validation
          failures, feeding the error back into the next attempt),
        - the circuit breaker (failure/success accounting).

        Args:
            question: User's natural language question.
            schema: Database schema for context.
            request_id: Request ID for tracking.

        Returns:
            tuple: (generated_sql, validation_result, tokens_used) where
                tokens_used is the total token consumption reported by
                the OpenAI API usage object (None when unavailable).

        Raises:
            LLMError: If circuit breaker is open or generation fails.
            RateLimitExceededError: If the LLM rate limiter rejects the request.
            SecurityViolationError: If SQL fails validation after all retries.
            SQLParseError: If SQL cannot be parsed.

        Example:
            >>> sql, validation, tokens = await orchestrator._generate_sql_with_retry(
            ...     question="Count users",
            ...     schema=db_schema,
            ...     request_id="123",
            ... )
        """
        # Check circuit breaker
        if not self.circuit_breaker.allow_request():
            raise LLMError(
                message="SQL generation service is temporarily unavailable (circuit breaker open)",
                details={
                    "circuit_state": self.circuit_breaker.state,
                    "failure_count": self.circuit_breaker.failure_count,
                },
            )

        # Mutable state shared between retry attempts:
        # - previous_attempt/error_feedback: fed into the next LLM call
        # - last_sql: the SQL produced by the most recent attempt
        attempt_state: dict[str, Any] = {}
        tokens_used: int | None = None
        llm_limiter = (self.rate_limiters or {}).get("llm")
        attempts = 0

        async def _attempt() -> str:
            nonlocal tokens_used, attempts
            attempts += 1

            slot_held = False
            if llm_limiter is not None:
                slot_held = await llm_limiter.acquire()
                if not slot_held:
                    raise RateLimitExceededError(
                        message="LLM rate limit exceeded, try again later",
                        details={"limiter": "llm"},
                    )
            try:
                if self.metrics is not None:
                    self.metrics.increment_llm_call("generate_sql")
                llm_start = time.monotonic()

                generated_sql, tokens = await self.sql_generator.generate(
                    question=question,
                    schema=schema,
                    previous_attempt=attempt_state.get("previous_attempt"),
                    error_feedback=attempt_state.get("error_feedback"),
                )

                if self.metrics is not None:
                    self.metrics.observe_llm_latency(
                        "generate_sql", time.monotonic() - llm_start
                    )
                    if tokens is not None:
                        self.metrics.increment_llm_tokens("generate_sql", tokens)

                tokens_used = tokens
            finally:
                if slot_held and llm_limiter is not None:
                    llm_limiter.release()

            attempt_state["last_sql"] = generated_sql

            try:
                self.sql_validator.validate_or_raise(generated_sql)
            except (SecurityViolationError, SQLParseError) as validation_error:
                logger.warning(
                    "SQL validation failed, retrying with feedback",
                    extra={
                        "request_id": request_id,
                        "attempt": attempts,
                        "error": str(validation_error),
                    },
                )
                raise

            logger.debug(
                "SQL generated",
                extra={"request_id": request_id, "sql_length": len(generated_sql)},
            )
            return generated_sql

        def _on_retry(error: Exception, retry_attempt: int) -> None:
            """Feed the failed attempt and its error into the next retry."""
            attempt_state["previous_attempt"] = attempt_state.get("last_sql")
            attempt_state["error_feedback"] = str(error)
            logger.debug(
                "Preparing retry with error feedback",
                extra={"request_id": request_id, "retry_attempt": retry_attempt},
            )

        try:
            generated_sql = await self.retry_executor.execute(
                _attempt,
                retryable=lambda e: isinstance(e, (SecurityViolationError, SQLParseError)),
                on_retry=_on_retry,
            )
        except (SecurityViolationError, SQLParseError):
            self.circuit_breaker.record_failure()
            logger.error(
                "SQL validation failed after all retries",
                extra={"request_id": request_id, "attempts": attempts},
            )
            raise
        except RateLimitExceededError:
            self.circuit_breaker.record_failure()
            raise
        except Exception as e:
            # Unexpected error during generation
            self.circuit_breaker.record_failure()
            logger.exception(
                "Unexpected error during SQL generation",
                extra={"request_id": request_id},
            )
            raise LLMError(
                message=f"SQL generation failed unexpectedly: {e!s}",
                details={"error_type": type(e).__name__},
            ) from e

        # Validation successful
        self.circuit_breaker.record_success()
        logger.info(
            "SQL generated and validated successfully",
            extra={"request_id": request_id, "attempts": attempts},
        )

        # Build validation result
        validation_result = ValidationResult(
            is_valid=True,
            is_select=True,
            allows_data_modification=False,
            uses_blocked_functions=[],
            error_message=None,
        )

        return generated_sql, validation_result, tokens_used

    async def _validate_results_safely(
        self,
        question: str,
        sql: str,
        results: list[dict[str, Any]],
        row_count: int,
        request_id: str,
    ) -> int:
        """Validate query results with error handling (non-blocking).

        This method attempts to validate results using LLM, but failures
        don't cause the overall query to fail. Returns a confidence score.

        Args:
            question: User's original question.
            sql: Generated SQL query.
            results: Query results.
            row_count: Total row count.
            request_id: Request ID for tracking.

        Returns:
            int: Confidence score (0-100). Returns 100 if validation disabled/fails.

        Example:
            >>> confidence = await orchestrator._validate_results_safely(
            ...     question="Count users",
            ...     sql="SELECT COUNT(*) FROM users",
            ...     results=[{"count": 42}],
            ...     row_count=1,
            ...     request_id="123",
            ... )
        """
        if not self.validation_config.enabled:
            return 100

        try:
            logger.debug(
                "Validating results",
                extra={"request_id": request_id},
            )

            validation_result = await self.result_validator.validate(
                question=question,
                sql=sql,
                results=results,
                row_count=row_count,
            )

            logger.info(
                "Result validation completed",
                extra={
                    "request_id": request_id,
                    "confidence": validation_result.confidence,
                    "is_acceptable": validation_result.is_acceptable,
                },
            )

            return validation_result.confidence

        except Exception as e:
            # Log but don't fail the query
            logger.warning(
                "Result validation failed, continuing with default confidence",
                extra={
                    "request_id": request_id,
                    "error": str(e),
                },
            )
            return 100  # Default to high confidence if validation fails

    def _record_request_metric(self, status: str, database: str | None) -> None:
        """Record the query request counter in Prometheus metrics."""
        if self.metrics is not None:
            self.metrics.increment_query_request(
                status=status, database=database or "unknown"
            )

    def _observe_total_duration(self, start_time: float) -> None:
        """Record the total end-to-end query duration in Prometheus metrics."""
        if self.metrics is not None:
            self.metrics.query_duration.observe(time.monotonic() - start_time)

    def _record_sql_rejected(self, reason: str) -> None:
        """Record a security/parsing rejection in Prometheus metrics."""
        if self.metrics is not None:
            self.metrics.increment_sql_rejected(reason=reason)
