"""API HTTP de la memoria (FastAPI). Escucha solo en 127.0.0.1 de WSL; la identidad sale del token Bearer.

Errores interpretables y estables: 401 sin credencial, 403 denegado, 404 inexistente, 409 conflicto de versión o
idempotencia, 422 contrato, 503 base de datos no disponible (nada se guardó).
"""
from __future__ import annotations

from typing import Any
from uuid import UUID

from fastapi import FastAPI, Header, Query, Request
from fastapi.responses import JSONResponse
from pydantic import ValidationError
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, OperationalError

from servidor import comandos, contexto, memorias, rag
from servidor.config import PROYECTO_GLOBAL, VERSION_CONTRATO
from servidor.db import lectura, transaccion
from servidor.modelos import Comando, Lote, PedidoContexto, PedidoRag
from servidor.permisos import Conflicto, Denegado, Invalido, NoAutenticado, NoEncontrado, autenticar

app = FastAPI(title="DreamFactory · memoria", version="1.0.0", docs_url=None, redoc_url=None)

CODIGOS = {NoAutenticado: 401, Denegado: 403, NoEncontrado: 404, Conflicto: 409, Invalido: 422}


def _error(e: Exception) -> tuple[int, dict]:
    for clase, codigo in CODIGOS.items():
        if isinstance(e, clase):
            return codigo, {"error": clase.__name__.lower(), "mensaje": str(e)}
    if isinstance(e, ValidationError):
        return 422, {"error": "invalido", "mensaje": str(e)[:800]}
    if isinstance(e, OperationalError):
        return 503, {"error": "base_no_disponible", "mensaje": "PostgreSQL no respondió; no se guardó nada"}
    if isinstance(e, DBAPIError):
        orig = getattr(e, "orig", None)
        estado = getattr(orig, "sqlstate", "") or ""
        if estado.startswith("23"):
            return 409, {"error": "integridad", "mensaje": str(orig).split("\n")[0][:300]}
        return 503, {"error": "base", "mensaje": str(orig or e).split("\n")[0][:300]}
    raise e


@app.exception_handler(Exception)
async def _manejar(_: Request, e: Exception) -> JSONResponse:
    codigo, cuerpo = _error(e)
    return JSONResponse(cuerpo, status_code=codigo)


def _token(authorization: str | None) -> str | None:
    if authorization and authorization.lower().startswith("bearer "):
        return authorization[7:].strip()
    return None


@app.get("/v1/salud")
def salud() -> dict:
    with lectura() as con:
        migracion = con.execute(text("SELECT version_num FROM alembic_version_memoria")).scalars().all()
    return {"ok": True, "contrato": VERSION_CONTRATO, "migracion": migracion, "proyecto_global": PROYECTO_GLOBAL}


@app.get("/v1/yo")
def yo(authorization: str | None = Header(default=None)) -> dict:
    with lectura() as con:
        p = autenticar(con, _token(authorization))
    return {"agente": p.agente, "tipo": p.tipo, "niveles": p.niveles}


@app.post("/v1/comando")
def comando(c: Comando, authorization: str | None = Header(default=None)) -> dict:
    with transaccion() as con:
        p = autenticar(con, _token(authorization))
        return comandos.ejecutar(con, p, c)


@app.post("/v1/lote")
def lote(l: Lote, authorization: str | None = Header(default=None)) -> dict:
    """Cada comando en su propia transacción: uno que falla no bloquea a los demás (la cola local reintenta)."""
    resultados: list[dict[str, Any]] = []
    for c in l.comandos:
        try:
            with transaccion() as con:
                p = autenticar(con, _token(authorization))
                resultados.append({"clave_idem": c.clave_idem, "ok": True, "respuesta": comandos.ejecutar(con, p, c)})
        except OperationalError:
            resultados.append({"clave_idem": c.clave_idem, "ok": False, "codigo": 503,
                               "error": {"error": "base_no_disponible", "mensaje": "no se guardó"}})
            break
        except Exception as e:  # noqa: BLE001  (se traduce a código estable)
            codigo, cuerpo = _error(e)
            resultados.append({"clave_idem": c.clave_idem, "ok": False, "codigo": codigo, "error": cuerpo})
    return {"resultados": resultados}


@app.post("/v1/contexto")
def paquete(pedido: PedidoContexto, authorization: str | None = Header(default=None)) -> dict:
    with transaccion() as con:
        p = autenticar(con, _token(authorization))
        return contexto.armar(con, p, pedido)


def _fila(f: Any) -> dict:
    return {k: (str(v) if isinstance(v, UUID) else v) for k, v in dict(f._mapping).items() if k != "tsv"}


@app.get("/v1/memorias/buscar")
def buscar(q: str = Query(min_length=2, max_length=300), proyecto: str = Query(...), limite: int = Query(10, le=50),
           authorization: str | None = Header(default=None)) -> dict:
    with lectura() as con:
        p = autenticar(con, _token(authorization))
        p.exigir(proyecto, "lectura")
        filas = memorias.buscar(con, p, list(dict.fromkeys([proyecto, PROYECTO_GLOBAL])), q, limite)
    return {"resultados": [{"id": f.id, "tipo": f.tipo, "alcance": f.alcance, "prioridad": f.prioridad,
                            "titulo": f.titulo, "contenido": f.contenido[:500], "rango": round(float(f.rango), 4),
                            "revision": f.revision_actual} for f in filas]}


@app.get("/v1/memorias/{mid}")
def ver_memoria(mid: int, historia: bool = False, authorization: str | None = Header(default=None)) -> dict:
    with lectura() as con:
        p = autenticar(con, _token(authorization))
        return memorias.ver(con, p, mid, historia)


@app.get("/v1/checkpoints/ultimo")
def ultimo_checkpoint(proyecto: str, tarea: str | None = None, agente: str | None = None,
                      authorization: str | None = Header(default=None)) -> dict:
    with lectura() as con:
        p = autenticar(con, _token(authorization))
        p.exigir(proyecto, "lectura")
        fila = con.execute(text(
            "SELECT c.* FROM checkpoints c LEFT JOIN tareas t ON t.id = c.tarea_id WHERE c.proyecto_id = :p "
            "AND (CAST(:t AS text) IS NULL OR t.clave = :t) AND (CAST(:a AS text) IS NULL OR c.agente_id = :a) "
            "ORDER BY c.id DESC LIMIT 1"), {"p": proyecto, "t": tarea, "a": agente}).first()
    if not fila:
        raise NoEncontrado("Sin checkpoints para ese filtro")
    return _fila(fila)


@app.get("/v1/tareas/{proyecto}/{clave}")
def ver_tarea(proyecto: str, clave: str, authorization: str | None = Header(default=None)) -> dict:
    with lectura() as con:
        p = autenticar(con, _token(authorization))
        p.exigir(proyecto, "lectura")
        fila = con.execute(text("SELECT * FROM tareas WHERE proyecto_id = :p AND clave = :c"),
                           {"p": proyecto, "c": clave}).first()
    if not fila:
        raise NoEncontrado(f"No existe la tarea {proyecto}/{clave}")
    return _fila(fila)


@app.get("/v1/operaciones")
def operaciones(proyecto: str, estado: str = "abiertas", authorization: str | None = Header(default=None)) -> dict:
    estados = ["intencion", "incierta"] if estado == "abiertas" else [estado]
    with lectura() as con:
        p = autenticar(con, _token(authorization))
        p.exigir(proyecto, "lectura")
        filas = con.execute(text("SELECT * FROM operaciones WHERE proyecto_id = :p AND estado = ANY(:e) "
                                 "ORDER BY id DESC LIMIT 50"), {"p": proyecto, "e": estados}).all()
    return {"operaciones": [_fila(f) for f in filas]}


@app.post("/v1/rag/buscar")
def rag_buscar(pedido: PedidoRag, authorization: str | None = Header(default=None)) -> dict:
    with lectura() as con:
        p = autenticar(con, _token(authorization))
        return {"resultados": rag.buscar(con, p, pedido.proyectos, pedido.consulta, pedido.k, pedido.vector,
                                         pedido.modelo)}


@app.get("/v1/rag/estado")
def rag_estado(proyecto: str, authorization: str | None = Header(default=None)) -> dict:
    with lectura() as con:
        p = autenticar(con, _token(authorization))
        return rag.estado(con, p, proyecto)


@app.get("/v1/consumo")
def consumo(limite: int = Query(20, le=200), authorization: str | None = Header(default=None)) -> dict:
    with lectura() as con:
        p = autenticar(con, _token(authorization))
        filtro = "" if p.tipo == "admin" else "WHERE agente_id = :a"
        filas = con.execute(text(f"SELECT * FROM consumo_contexto {filtro} ORDER BY id DESC LIMIT :l"),
                            {"a": p.agente, "l": limite}).all()
    return {"consumo": [_fila(f) for f in filas]}


@app.get("/v1/eventos")
def eventos(proyecto: str, limite: int = Query(50, le=500), authorization: str | None = Header(default=None)) -> dict:
    with lectura() as con:
        p = autenticar(con, _token(authorization))
        p.exigir(proyecto, "curador")
        filas = con.execute(text("SELECT * FROM eventos WHERE proyecto_id = :p ORDER BY id DESC LIMIT :l"),
                            {"p": proyecto, "l": limite}).all()
    return {"eventos": [_fila(f) for f in filas]}
