from functools import lru_cache

from pgvector.psycopg import register_vector
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from app.settings import settings


@lru_cache
def pool() -> ConnectionPool:
    # check= pre-pings connections so a restarted DB doesn't hand out dead sockets.
    return ConnectionPool(
        settings().database_url,
        min_size=1,
        max_size=10,
        kwargs={"row_factory": dict_row, "autocommit": True},
        check=ConnectionPool.check_connection,
        configure=register_vector,
        open=True,
    )


def fetch(sql: str, *params) -> list[dict]:
    with pool().connection() as c:
        return c.execute(sql, params).fetchall()


def fetch_one(sql: str, *params) -> dict | None:
    rows = fetch(sql, *params)
    return rows[0] if rows else None


def execute(sql: str, *params) -> None:
    with pool().connection() as c:
        c.execute(sql, params)
