"""Configuración centralizada del servidor de memoria (variables de entorno + archivos de secreto de Docker)."""
from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

from sqlalchemy.engine import URL

PROYECTO_GLOBAL = os.environ.get("DFMEM_PROYECTO_GLOBAL", "dreamfactory")
VERSION_CONTRATO = "dfmem-v1"
PRESUPUESTO_DEFECTO = 1200
PRESUPUESTO_MAXIMO = 8000

_ROLES = {"app": ("DFMEM_DB_USER_APP", "DFMEM_DB_PASSWORD_FILE_APP"),
          "migrador": ("DFMEM_DB_USER_MIGRADOR", "DFMEM_DB_PASSWORD_FILE_MIGRADOR")}


def _secreto(variable: str) -> str:
    ruta = os.environ.get(variable)
    if not ruta:
        raise RuntimeError(f"Falta la variable {variable} (ruta al archivo de secreto).")
    return Path(ruta).read_text(encoding="utf-8").strip()


@lru_cache(maxsize=4)
def url_db(rol: str = "app") -> URL:
    usuario, clave = _ROLES[rol]
    return URL.create(
        drivername="postgresql+psycopg",
        username=os.environ[usuario],
        password=_secreto(clave),
        host=os.environ.get("DFMEM_DB_HOST", "postgres"),
        port=int(os.environ.get("DFMEM_DB_PORT", "5432")),
        database=os.environ.get("DFMEM_DB_NAME", "ai_game_factory"),
    )
