"""Alembic de la memoria: cadena propia, tabla de versiones propia (memoria.alembic_version_memoria).

No comparte historial con la cadena de AGF (public.alembic_version). Corre como df_memoria_migrador y hace
SET ROLE df_memoria_owner para que todos los objetos queden a nombre del dueño sin login.
"""
from alembic import context
from sqlalchemy import create_engine, pool, text

from servidor.config import url_db

config = context.config


def run_migrations_online() -> None:
    engine = create_engine(url_db("migrador"), poolclass=pool.NullPool, connect_args={"connect_timeout": 5})
    try:
        with engine.connect() as conexion:
            conexion.execute(text("SET ROLE df_memoria_owner"))
            conexion.execute(text("SET search_path TO memoria"))
            context.configure(connection=conexion, target_metadata=None, transactional_ddl=True,
                              version_table="alembic_version_memoria", version_table_schema="memoria")
            with context.begin_transaction():
                context.run_migrations()
            conexion.commit()
    finally:
        engine.dispose()


if context.is_offline_mode():
    raise RuntimeError("Migraciones offline deshabilitadas: se aplican solo contra una base verificada.")
run_migrations_online()
