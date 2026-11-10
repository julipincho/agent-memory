"""Administración (solo dentro del contenedor, nunca por HTTP): semilla, credenciales, mantenimiento, verificación.

  python -m servidor.admin semilla /app/config/semilla.json
  python -m servidor.admin credencial <agente> <agente|orquestador|admin> <archivo_salida>   # no imprime el token
  python -m servidor.admin revocar <credencial_id>
  python -m servidor.admin mantenimiento
  python -m servidor.admin verificar
"""
from __future__ import annotations

import json
import os
import secrets
import sys
from pathlib import Path

from sqlalchemy import text

from servidor.comandos import _json, marcar_interrumpidas
from servidor.db import transaccion
from servidor.permisos import hash_token


def _evento(con, accion: str, entidad: str, entidad_id: str, detalle: dict) -> None:
    con.execute(text("INSERT INTO eventos (principal, tipo_credencial, agente_id, accion, entidad, entidad_id, detalle) "
                     "VALUES ('admin:consola', 'admin', 'sistema', :a, :e, :i, CAST(:d AS jsonb))"),
                {"a": accion, "e": entidad, "i": entidad_id, "d": _json(detalle)})


def semilla(ruta: str) -> dict:
    datos = json.loads(Path(ruta).read_text(encoding="utf-8"))
    with transaccion() as con:
        for a in datos["agentes"]:
            con.execute(text(
                "INSERT INTO agentes (id, nombre, rol, especialidad, runtime) VALUES (:id, :nombre, :rol, :especialidad, "
                ":runtime) ON CONFLICT (id) DO UPDATE SET nombre = EXCLUDED.nombre, rol = EXCLUDED.rol, "
                "especialidad = EXCLUDED.especialidad, runtime = EXCLUDED.runtime, actualizado = now()"), a)
        for py in datos["proyectos"]:
            con.execute(text("INSERT INTO proyectos (id, nombre, raiz) VALUES (:id, :nombre, :raiz) ON CONFLICT (id) "
                             "DO UPDATE SET nombre = EXCLUDED.nombre, raiz = EXCLUDED.raiz"), py)
        for pe in datos["permisos"]:
            con.execute(text("INSERT INTO permisos (agente_id, proyecto_id, nivel, otorgado_por) VALUES (:agente, "
                             ":proyecto, :nivel, 'semilla') ON CONFLICT (agente_id, proyecto_id) DO UPDATE SET "
                             "nivel = EXCLUDED.nivel, otorgado_por = 'semilla', actualizado = now()"), pe)
        for pr in datos["presupuestos"]:
            con.execute(text("INSERT INTO presupuestos (agente_id, operacion, tokens) VALUES (:agente, :operacion, "
                             ":tokens) ON CONFLICT (agente_id, operacion) DO UPDATE SET tokens = EXCLUDED.tokens"), pr)
        _evento(con, "semilla", "configuracion", ruta, {k: len(v) for k, v in datos.items() if isinstance(v, list)})
    return {k: len(v) for k, v in datos.items() if isinstance(v, list)}


def credencial(agente: str, tipo: str, salida: str) -> dict:
    token = "dfm_" + secrets.token_urlsafe(32)
    destino = Path(salida)
    destino.parent.mkdir(parents=True, exist_ok=True)
    with transaccion() as con:
        cid = con.execute(text("INSERT INTO credenciales (agente_id, tipo, hash_token, etiqueta) VALUES (:a, :t, :h, "
                               ":e) RETURNING id"), {"a": agente, "t": tipo, "h": hash_token(token),
                                                      "e": destino.name}).scalar_one()
        _evento(con, "credencial_creada", "credencial", str(cid), {"agente": agente, "tipo": tipo})
        tmp = destino.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(token)
        os.replace(tmp, destino)
    return {"credencial": cid, "agente": agente, "tipo": tipo, "archivo": str(destino)}


def revocar(cid: int) -> dict:
    with transaccion() as con:
        n = con.execute(text("UPDATE credenciales SET revocada = now() WHERE id = :i AND revocada IS NULL"),
                        {"i": cid}).rowcount
        _evento(con, "credencial_revocada", "credencial", str(cid), {"filas": n})
    return {"revocadas": n}


def mantenimiento() -> dict:
    with transaccion() as con:
        inciertas = marcar_interrumpidas(con)
        vencidas = con.execute(text("UPDATE memorias SET estado = 'obsoleta', actualizado = now() WHERE estado = "
                                    "'vigente' AND vence IS NOT NULL AND vence <= now()")).rowcount
        _evento(con, "mantenimiento", "memoria", "", {"operaciones_inciertas": inciertas, "vencidas": vencidas})
    return {"operaciones_pasadas_a_inciertas": inciertas, "memorias_vencidas": vencidas}


CHEQUEOS = {
    "memorias_sin_revision_actual": "SELECT count(*) FROM memorias m WHERE NOT EXISTS (SELECT 1 FROM memoria_revisiones "
                                    "r WHERE r.memoria_id = m.id AND r.revision = m.revision_actual)",
    "revisiones_con_huecos": "SELECT count(*) FROM (SELECT memoria_id, max(revision) mx, count(*) n FROM "
                             "memoria_revisiones GROUP BY 1) s WHERE mx <> n",
    "reemplazadas_sin_destino": "SELECT count(*) FROM memorias WHERE estado = 'reemplazada' AND reemplazada_por IS NULL",
    "checkpoints_secuencia_rota": "SELECT count(*) FROM (SELECT sesion_id, max(secuencia) mx, count(*) n FROM "
                                  "checkpoints GROUP BY 1) s WHERE mx <> n",
    "documentos_vigentes_duplicados": "SELECT count(*) FROM (SELECT proyecto_id, ruta FROM documentos WHERE vigente "
                                      "GROUP BY 1, 2 HAVING count(*) > 1) s",
    "fragmentos_faltantes": "SELECT count(*) FROM documentos d WHERE d.fragmentos <> (SELECT count(*) FROM fragmentos "
                            "f WHERE f.documento_id = d.id)",
}


def verificar() -> dict:
    with transaccion() as con:
        problemas = {k: con.execute(text(sql)).scalar_one() for k, sql in CHEQUEOS.items()}
        totales = {t: con.execute(text(f"SELECT count(*) FROM {t}")).scalar_one() for t in (
            "agentes", "credenciales", "proyectos", "tareas", "sesiones", "checkpoints", "operaciones", "memorias",
            "memoria_revisiones", "documentos", "fragmentos", "eventos", "consumo_contexto")}
    return {"integro": not any(problemas.values()), "problemas": problemas, "totales": totales}


def main(args: list[str]) -> int:
    ordenes = {"semilla": lambda: semilla(args[1]), "credencial": lambda: credencial(args[1], args[2], args[3]),
               "revocar": lambda: revocar(int(args[1])), "mantenimiento": mantenimiento, "verificar": verificar}
    if not args or args[0] not in ordenes:
        print(__doc__)
        return 2
    resultado = ordenes[args[0]]()
    print(json.dumps(resultado, ensure_ascii=False, default=str))
    return 0 if resultado.get("integro", True) else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
