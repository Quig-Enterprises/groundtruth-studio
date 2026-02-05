"""
Shared PostgreSQL Database Connection Module

Provides centralized connection management for all database operations.
Replaces all direct sqlite3 usage throughout the application.
"""

import os
import logging
from contextlib import contextmanager
from typing import Optional, List, Dict, Any
from urllib.parse import urlparse

import psycopg2
from psycopg2 import pool, extras

logger = logging.getLogger(__name__)

# Global connection pool
_connection_pool: Optional[pool.ThreadedConnectionPool] = None


def get_database_url() -> str:
    """Get database URL from environment"""
    url = os.environ.get('DATABASE_URL')
    if not url:
        raise RuntimeError(
            "DATABASE_URL environment variable not set. "
            "Expected format: postgresql://user:pass@host:port/dbname"
        )
    return url


def init_connection_pool(min_conn: int = 2, max_conn: int = 10) -> None:
    """Initialize the connection pool. Call once at app startup."""
    global _connection_pool

    if _connection_pool is not None:
        return  # Already initialized

    url = get_database_url()
    parsed = urlparse(url)

    _connection_pool = pool.ThreadedConnectionPool(
        min_conn,
        max_conn,
        host=parsed.hostname,
        port=parsed.port or 5432,
        database=parsed.path[1:],  # Remove leading /
        user=parsed.username,
        password=parsed.password,
    )
    logger.info(f"Database connection pool initialized (min={min_conn}, max={max_conn})")


def close_connection_pool() -> None:
    """Close all connections in the pool. Call at app shutdown."""
    global _connection_pool
    if _connection_pool:
        _connection_pool.closeall()
        _connection_pool = None
        logger.info("Database connection pool closed")


@contextmanager
def get_connection():
    """
    Get a database connection from the pool.

    Usage:
        with get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM videos")
            rows = cursor.fetchall()

    Connection is automatically returned to pool when context exits.
    """
    global _connection_pool

    if _connection_pool is None:
        init_connection_pool()

    conn = _connection_pool.getconn()
    try:
        yield conn
    finally:
        _connection_pool.putconn(conn)


@contextmanager
def get_cursor(commit: bool = True):
    """
    Get a cursor with automatic commit/rollback and connection management.

    Usage:
        with get_cursor() as cursor:
            cursor.execute("INSERT INTO videos ...")
            # Auto-commits on success, rolls back on exception

    Args:
        commit: If True, commit transaction on success. Default True.
    """
    with get_connection() as conn:
        cursor = conn.cursor(cursor_factory=extras.RealDictCursor)
        try:
            yield cursor
            if commit:
                conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            cursor.close()


def execute_query(query: str, params: tuple = None, fetch: str = 'all') -> Any:
    """
    Execute a query and return results.

    Args:
        query: SQL query string (use %s for parameters)
        params: Query parameters as tuple
        fetch: 'all', 'one', 'none' (for INSERT/UPDATE/DELETE)

    Returns:
        List of dicts (fetch='all'), single dict (fetch='one'), or None
    """
    with get_cursor(commit=(fetch == 'none')) as cursor:
        cursor.execute(query, params)

        if fetch == 'all':
            return cursor.fetchall()
        elif fetch == 'one':
            return cursor.fetchone()
        elif fetch == 'none':
            return None
        else:
            raise ValueError(f"Invalid fetch mode: {fetch}")


def execute_returning(query: str, params: tuple = None, returning_col: str = 'id') -> Any:
    """
    Execute INSERT/UPDATE with RETURNING clause.

    Args:
        query: SQL query with RETURNING clause
        params: Query parameters
        returning_col: Column name to return (default 'id')

    Returns:
        Value of the returning column
    """
    with get_cursor() as cursor:
        cursor.execute(query, params)
        result = cursor.fetchone()
        return result[returning_col] if result else None
