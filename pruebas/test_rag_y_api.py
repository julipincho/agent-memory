"""RAG (indexación incremental, versiones, permisos, híbrido), triggers con el rol dueño y API HTTP real."""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import DBAPIError

from conftest import contexto, ejecutar, principal, sql, token, unico
from fragmentar import fragmentar
from servidor import rag
from servidor.config import url_db
from servidor.db import lectura

DOC_V1 = """# Combate
Reglas de D&D 5e adaptadas.
## Ventaja
Con ventaja se tiran dos d20 y se usa el mayor. La desventaja usa el menor.
## Iniciativa
Cada criatura tira iniciativa al empezar el combate; los empates los decide la destreza.
"""


def _indexar(agente: str, ruta: str, cuerpo: str, proyecto: str = "yarael_quest") -> dict:
    datos = cuerpo.encode()
    return ejecutar(agente, "documento_indexar", {
        "proyecto": proyecto, "ruta": ruta, "sha256": hashlib.sha256(datos).hexdigest(), "bytes": len(datos),
        "fragmentos": fragmentar(cuerpo)})


def _buscar(agente: str, consulta: str, proyectos=("yarael_quest",), **kw) -> list[dict]:
    with lectura() as con:
        return rag.buscar(con, principal(agente), list(proyectos), consulta, **kw)


def test_fragmentar_conserva_encabezados_y_lineas():
    fr = fragmentar(DOC_V1)
    assert [f["encabezado"] for f in fr] == ["Combate", "Combate › Ventaja", "Combate › Iniciativa"]
    assert fr[1]["linea_ini"] == 4 and "dos d20" in fr[1]["texto"]


def test_rag_incremental_versionado_y_citas():
    ruta = f"docs/{unico('reglas')}.md"
    r1 = _indexar("orquestador", ruta, DOC_V1)
    assert r1["fragmentos"] == 3
    assert _indexar("orquestador", ruta, DOC_V1)["sin_cambios"]
    res = _buscar("tortu", "cómo funciona la ventaja con dos dados")
    propio = [r for r in res if r["ruta"] == ruta]
    assert propio and propio[0]["encabezado"] == "Combate › Ventaja" and propio[0]["ref"].startswith(f"{ruta}:4-4@")
    r2 = _indexar("orquestador", ruta, DOC_V1.replace("el mayor", "el más alto (regla de la casa)"))
    assert r2["reemplaza"] == r1["documento"]
    vigentes = sql("SELECT count(*) n FROM documentos WHERE ruta = :r AND vigente", r=ruta)[0].n
    total = sql("SELECT count(*) n FROM documentos WHERE ruta = :r", r=ruta)[0].n
    assert (vigentes, total) == (1, 2)    # la versión vieja queda, pero no se recupera
    textos = " ".join(r["extracto"] for r in _buscar("claude", "ventaja regla de la casa") if r["ruta"] == ruta)
    assert "regla de la casa" in textos


def test_rag_respeta_permisos_y_entra_en_el_contexto():
    _indexar("claude", f"docs/{unico('agf')}.md", "# AGF\nEl gateway S2 sigue bloqueado por acceso.\n" * 2,
             proyecto="ai_game_factory")
    assert _buscar("codex", "gateway S2 bloqueado", proyectos=("ai_game_factory",)) == []
    assert _buscar("claude", "gateway S2 bloqueado", proyectos=("ai_game_factory",))
    pk = contexto("claude", proyecto="yarael_quest", consulta="iniciativa empates destreza", presupuesto=2000)
    assert "Documentación relevante (RAG)" in pk["texto"] and pk["extractos_rag"] >= 1


def test_rag_hibrido_con_embeddings():
    ruta = f"docs/{unico('vec')}.md"
    r = _indexar("orquestador", ruta, "# Mapas\nEl editor de mapas guarda JSON.\n## Luces\nLas antorchas iluminan.\n")
    items = [{"documento_id": r["documento"], "n": 1, "vector": [1.0] + [0.0] * 15},
             {"documento_id": r["documento"], "n": 2, "vector": [0.0, 1.0] + [0.0] * 14}]
    assert ejecutar("orquestador", "fragmentos_embedding", {"proyecto": "yarael_quest", "modelo": "prueba-16",
                                                            "items": items})["actualizados"] == 2
    res = _buscar("claude", "", vector=[0.0, 0.9] + [0.1] * 14, modelo="prueba-16")
    assert res and res[0]["encabezado"] == "Mapas › Luces"
    with pytest.raises(DBAPIError):   # el texto del fragmento no se puede cambiar
        sql("UPDATE fragmentos SET texto = 'x' WHERE documento_id = :d", d=r["documento"])


def test_triggers_protegen_aun_al_dueno():
    motor = create_engine(url_db("migrador"))
    with motor.begin() as con:
        con.execute(text("SET ROLE df_memoria_owner"))
        with pytest.raises(DBAPIError):
            with con.begin_nested():
                con.execute(text("DELETE FROM memoria.checkpoints"))
        with pytest.raises(DBAPIError):
            with con.begin_nested():
                con.execute(text("TRUNCATE memoria.memorias CASCADE"))
    motor.dispose()


@pytest.fixture(scope="module")
def servidor_http():
    proc = subprocess.Popen([sys.executable, "-m", "uvicorn", "servidor.api:app", "--port", "8799"],
                            cwd="/app", stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=os.environ.copy())
    for _ in range(50):
        try:
            urllib.request.urlopen("http://127.0.0.1:8799/v1/salud", timeout=1)
            break
        except OSError:
            time.sleep(0.2)
    yield "http://127.0.0.1:8799"
    proc.terminate()
    proc.wait(10)


def _http(base: str, ruta: str, cuerpo: dict | None = None, agente: str | None = "claude") -> tuple[int, dict]:
    pedido = urllib.request.Request(base + ruta, data=json.dumps(cuerpo).encode() if cuerpo is not None else None,
                                    headers={"Content-Type": "application/json",
                                             **({"Authorization": f"Bearer {token(agente)}"} if agente else {})})
    try:
        with urllib.request.urlopen(pedido, timeout=10) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def test_api_http_de_punta_a_punta(servidor_http):
    base = servidor_http
    assert _http(base, "/v1/salud", agente=None)[1]["migracion"] == ["0002_rag_documentos"]
    assert _http(base, "/v1/yo", agente=None)[0] == 401
    assert _http(base, "/v1/yo")[1]["agente"] == "claude"
    sid = str(uuid.uuid4())
    lote = {"comandos": [
        {"tipo": "sesion_abrir", "clave_idem": f"a-{sid}", "datos": {"id": sid, "proyecto": "yarael_quest", "canal": "http"}},
        {"tipo": "checkpoint", "clave_idem": f"c-{sid}", "datos": {"sesion_id": sid, "proyecto": "yarael_quest",
                                                                   "haciendo": "probar la API"}},
        {"tipo": "memoria_recordar", "clave_idem": f"m-{sid}", "datos": {"proyecto": "dreamfactory", "alcance": "global",
                                                                        "clave": "x", "tipo": "regla", "titulo": "t"}}]}
    codigo, r = _http(base, "/v1/lote", lote, agente="codex")
    oks = [x["ok"] for x in r["resultados"]]
    assert codigo == 200 and oks == [True, True, False] and r["resultados"][2]["codigo"] == 403
    codigo, r2 = _http(base, "/v1/lote", lote, agente="codex")       # reintento de la cola local: idempotente
    assert [x.get("respuesta", {}).get("repetido") for x in r2["resultados"][:2]] == [True, True]
    codigo, pk = _http(base, "/v1/contexto", {"proyecto": "yarael_quest", "operacion": "inicio"}, agente="codex")
    assert codigo == 200 and "probar la API" in pk["texto"]
    assert _http(base, "/v1/eventos?proyecto=yarael_quest", agente="codex")[0] == 403
    assert _http(base, "/v1/eventos?proyecto=yarael_quest")[0] == 200
