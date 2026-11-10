"""Fixtures de integración: PostgreSQL real (el aislado de infra/probar_aislado.sh), sin mocks."""
from __future__ import annotations

import os
import uuid
from pathlib import Path

import pytest
from sqlalchemy import text

if not os.environ.get("DFMEM_PRUEBA"):
    pytest.skip("Pruebas de integración: correr con infra/probar_aislado.sh (DFMEM_PRUEBA=1)", allow_module_level=True)

from servidor import admin  # noqa: E402
from servidor.db import transaccion  # noqa: E402
from servidor.modelos import Comando  # noqa: E402
from servidor.permisos import autenticar  # noqa: E402

DIR_TOKENS = Path("/tmp/dfmem-tokens")


@pytest.fixture(scope="session", autouse=True)
def semilla() -> None:
    admin.semilla("/app/config/semilla.json")
    for agente, tipo in [("claude", "agente"), ("codex", "agente"), ("opencode", "agente"), ("tortu", "agente"),
                         ("orquestador", "orquestador")]:
        if not (DIR_TOKENS / f"{agente}.token").exists():
            admin.credencial(agente, tipo, str(DIR_TOKENS / f"{agente}.token"))


def token(agente: str) -> str:
    return (DIR_TOKENS / f"{agente}.token").read_text().strip()


def principal(agente: str):
    with transaccion() as con:
        return autenticar(con, token(agente))


def ejecutar(agente: str, tipo: str, datos: dict, clave: str | None = None) -> dict:
    from servidor import comandos
    with transaccion() as con:
        p = autenticar(con, token(agente))
        return comandos.ejecutar(con, p, Comando(tipo=tipo, clave_idem=clave or f"k-{uuid.uuid4()}", datos=datos))


def contexto(agente: str, **pedido) -> dict:
    from servidor import contexto as ctx
    from servidor.modelos import PedidoContexto
    with transaccion() as con:
        p = autenticar(con, token(agente))
        return ctx.armar(con, p, PedidoContexto(**pedido))


def sql(consulta: str, **params):
    with transaccion() as con:
        r = con.execute(text(consulta), params)
        return r.all() if r.returns_rows else r.rowcount


def nueva_sesion(agente: str, proyecto: str = "yarael_quest", tarea: str | None = None, por: str | None = None) -> str:
    sid = str(uuid.uuid4())
    datos = {"id": sid, "proyecto": proyecto, "canal": "prueba", "tarea": tarea}
    if por:
        datos["agente"] = agente
    ejecutar(por or agente, "sesion_abrir", datos)
    return sid


def unico(prefijo: str) -> str:
    return f"{prefijo}-{uuid.uuid4().hex[:8]}"
