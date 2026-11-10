"""Conexión a PostgreSQL. Cada comando corre en una sola transacción: o se confirma todo o nada."""
from __future__ import annotations

from contextlib import contextmanager
from functools import lru_cache
from typing import Iterator

from sqlalchemy import Connection, create_engine, text
from sqlalchemy.engine import Engine

from servidor.config import url_db


@lru_cache(maxsize=1)
def motor() -> Engine:
    return create_engine(
        url_db("app"), pool_pre_ping=True, pool_size=5, max_overflow=5, pool_timeout=10,
        connect_args={"connect_timeout": 5, "options": "-c search_path=memoria -c statement_timeout=8000"},
    )


@contextmanager
def transaccion() -> Iterator[Connection]:
    with motor().begin() as conexion:
        yield conexion


@contextmanager
def lectura() -> Iterator[Connection]:
    with motor().connect() as conexion:
        conexion.execute(text("SET TRANSACTION READ ONLY"))
        try:
            yield conexion
        finally:
            conexion.rollback()
