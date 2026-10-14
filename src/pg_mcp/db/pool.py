"""Database connection pool management.

This module handles creating and managing PostgreSQL connection pools
using asyncpg.
"""

import asyncio
import logging

import asyncpg
from asyncpg import Pool

logger = logging.getLogger(__name__)


async def create_pool(
    dsn: str,
    min_size: int = 5,
    max_size: int = 20,
    command_timeout: float = 30,
) -> Pool:
    """Create a new asyncpg connection pool.

    Args:
        dsn: Data Source Name string for the PostgreSQL connection.
        min_size: Minimum number of connections in the pool.
        max_size: Maximum number of connections in the pool.
        command_timeout: Command execution timeout in seconds.

    Returns:
        An asyncpg Pool instance.
    """
    pool: Pool = await asyncpg.create_pool(
        dsn=dsn,
        min_size=min_size,
        max_size=max_size,
        command_timeout=command_timeout,
    )
    logger.info(f"Created connection pool with dsn={dsn[:50]}...")
    return pool


async def close_pools(pools: dict[str, Pool], close_timeout: float = 10.0) -> None:
    """Close all connection pools gracefully.

    This function closes all pools and waits for all connections to be
    released properly. If graceful shutdown takes too long, it will
    forcefully terminate the pools.

    Args:
        pools: Dictionary mapping database names to their pools.
        close_timeout: Maximum time in seconds to wait for graceful shutdown
            before forcing termination. Default: 10.0 seconds.
    """
    for db_name, pool in pools.items():
        try:
            # Try graceful close with timeout
            await asyncio.wait_for(pool.close(), timeout=close_timeout)
            logger.info(f"Connection pool for '{db_name}' closed gracefully")
        except TimeoutError:
            # Force termination if graceful close times out
            logger.warning(
                f"Graceful close timed out for '{db_name}', forcing termination"
            )
            pool.terminate()
            logger.info(f"Connection pool for '{db_name}' terminated")
        except Exception as e:
            # Log error but continue closing other pools
            logger.error(f"Error closing pool for '{db_name}': {e!s}")
            # Force terminate on error
            pool.terminate()
