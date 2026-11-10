"""Memorias versionadas: crear/revisar, reemplazar, invalidar y leer respetando alcance y permisos.

Una memoria es una identidad estable (proyecto, alcance, propietario, tarea, clave) con revisiones inmutables.
Escribir la misma clave crea una revisión nueva (la anterior queda en la historia, no se pisa). «Reemplaza» marca
otras memorias como reemplazadas por esta: así una decisión nueva desplaza a la vieja en la recuperación.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any

from sqlalchemy import Connection, text

import redaccion
from servidor.modelos import MemoriaEstado, MemoriaRecordar
from servidor.permisos import (Conflicto, Denegado, Invalido, NoEncontrado, Principal, exigir_escritura_memoria,
                               puede_leer_memoria)

COLUMNAS = ("m.id, m.proyecto_id, m.alcance, m.propietario, m.tarea_id, m.clave, m.tipo, m.prioridad, m.estado, "
            "m.revision_actual, m.reemplazada_por, m.compartida_con, m.vence, m.actualizado, r.titulo, r.contenido, "
            "r.refs, r.etiquetas, r.autor, r.origen, r.fuente, r.hash, r.sospechoso")


def _huella(titulo: str, contenido: str, refs: list) -> str:
    return hashlib.sha256(json.dumps([titulo, contenido, refs], ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def recordar(con: Connection, p: Principal, d: MemoriaRecordar) -> dict:
    from servidor.comandos import tarea_id   # evita import circular
    propietario = p.agente if d.alcance == "agente" else None
    exigir_escritura_memoria(p, d.proyecto, d.alcance, propietario)
    if d.alcance == "tarea" and not d.tarea:
        raise Invalido("alcance «tarea» requiere tarea")
    if d.compartida_con and d.alcance != "agente":
        raise Invalido("compartida_con solo aplica a memoria privada (alcance «agente»)")
    tid = tarea_id(con, d.proyecto, d.tarea, crear=True) if d.alcance == "tarea" else None
    titulo, m1 = redaccion.limpiar(d.titulo, 200)
    contenido, m2 = redaccion.limpiar(d.contenido, 4000)
    fuente, _ = redaccion.limpiar(d.fuente, 300)
    motivo, _ = redaccion.limpiar(d.motivo, 300)
    refs = redaccion.limpiar_json([r.model_dump(exclude_none=True) for r in d.refs], 400)
    etiquetas = [redaccion.limpiar(e, 40)[0] for e in d.etiquetas]
    sospechoso = m1["sospechoso"] or m2["sospechoso"]
    redactado = sorted(set(m1["redactado"] + m2["redactado"]))
    huella = _huella(titulo, contenido, refs)
    actual = con.execute(text(
        "SELECT m.id, m.revision_actual, m.estado, m.tipo, m.prioridad, r.hash FROM memorias m "
        "JOIN memoria_revisiones r ON r.memoria_id = m.id AND r.revision = m.revision_actual "
        "WHERE m.proyecto_id = :p AND m.alcance = :al AND coalesce(m.propietario, '') = coalesce(:pr, '') "
        "AND coalesce(m.tarea_id, 0) = coalesce(:t, 0) AND m.clave = :c FOR UPDATE OF m"),
        {"p": d.proyecto, "al": d.alcance, "pr": propietario, "t": tid, "c": d.clave}).first()
    if actual is None:
        if d.revision_esperada not in (None, 0):
            raise Conflicto(f"La memoria {d.clave} no existe (se esperaba revisión {d.revision_esperada})")
        mid = con.execute(text(
            "INSERT INTO memorias (proyecto_id, alcance, propietario, tarea_id, clave, tipo, prioridad, compartida_con, "
            "vence) VALUES (:p, :al, :pr, :t, :c, :ti, :pi, :co, :v) RETURNING id"),
            {"p": d.proyecto, "al": d.alcance, "pr": propietario, "t": tid, "c": d.clave, "ti": d.tipo,
             "pi": d.prioridad, "co": d.compartida_con, "v": d.vence}).scalar_one()
        revision = 1
    else:
        if d.revision_esperada is not None and d.revision_esperada != actual.revision_actual:
            raise Conflicto(f"La memoria {d.clave} está en la revisión {actual.revision_actual}, no "
                            f"{d.revision_esperada}: releerla y reintentar")
        mid = actual.id
        if (actual.hash == huella and actual.estado == "vigente" and actual.tipo == d.tipo
                and actual.prioridad == d.prioridad and not d.reemplaza):
            return {"memoria": mid, "revision": actual.revision_actual, "sin_cambios": True}
        revision = actual.revision_actual + 1
        con.execute(text("UPDATE memorias SET tipo = :ti, prioridad = :pi, estado = 'vigente', revision_actual = :r, "
                         "reemplazada_por = NULL, compartida_con = :co, vence = :v, actualizado = now() WHERE id = :i"),
                    {"ti": d.tipo, "pi": d.prioridad, "r": revision, "co": d.compartida_con, "v": d.vence, "i": mid})
    con.execute(text(
        "INSERT INTO memoria_revisiones (memoria_id, revision, titulo, contenido, refs, etiquetas, autor, origen, "
        "fuente, motivo, hash, sospechoso) VALUES (:m, :r, :ti, :co, CAST(:re AS jsonb), :et, :au, :o, :f, :mo, :h, :s)"),
        {"m": mid, "r": revision, "ti": titulo, "co": contenido, "re": json.dumps(refs, ensure_ascii=False),
         "et": etiquetas, "au": p.agente, "o": d.origen, "f": fuente, "mo": motivo, "h": huella, "s": sospechoso})
    reemplazadas = []
    for vieja in d.reemplaza:
        if vieja == mid:
            raise Invalido("Una memoria no puede reemplazarse a sí misma")
        fila = con.execute(text("SELECT id, proyecto_id, alcance, propietario, estado FROM memorias "
                                "WHERE id = :i FOR UPDATE"), {"i": vieja}).first()
        if not fila:
            raise NoEncontrado(f"No existe la memoria {vieja}")
        exigir_escritura_memoria(p, fila.proyecto_id, fila.alcance, fila.propietario)
        con.execute(text("UPDATE memorias SET estado = 'reemplazada', reemplazada_por = :n, actualizado = now() "
                         "WHERE id = :i"), {"n": mid, "i": vieja})
        reemplazadas.append(vieja)
    return {"memoria": mid, "revision": revision, "clave": d.clave, "sospechoso": sospechoso, "redactado": redactado,
            "reemplazadas": reemplazadas}


def cambiar_estado(con: Connection, p: Principal, d: MemoriaEstado) -> dict:
    fila = con.execute(text("SELECT id, proyecto_id, alcance, propietario, compartida_con, estado FROM memorias "
                            "WHERE id = :i FOR UPDATE"), {"i": d.id}).first()
    if not fila or not puede_leer_memoria(p, fila.proyecto_id, fila.alcance, fila.propietario, fila.compartida_con):
        raise NoEncontrado(f"No existe la memoria {d.id}")
    exigir_escritura_memoria(p, fila.proyecto_id, fila.alcance, fila.propietario)
    con.execute(text("UPDATE memorias SET estado = :e, actualizado = now(), "
                     "reemplazada_por = CASE WHEN :e = 'vigente' THEN NULL ELSE reemplazada_por END WHERE id = :i"),
                {"e": d.estado, "i": d.id})
    return {"memoria": d.id, "estado": d.estado, "anterior": fila.estado}


# ---------------------------------------------------------------- lectura

def _visibles_sql() -> str:
    """Filtro SQL de visibilidad equivalente a puede_leer_memoria (se vuelve a comprobar en Python)."""
    return ("(:admin OR (m.alcance <> 'agente' AND m.proyecto_id = ANY(:legibles)) "
            "OR (m.alcance = 'agente' AND (m.propietario = :yo OR :yo = ANY(m.compartida_con))))")


def parametros_visibilidad(p: Principal) -> dict:
    return {"admin": p.tipo == "admin", "legibles": [k for k, v in p.niveles.items() if v >= 1], "yo": p.agente}


def candidatas(con: Connection, p: Principal, proyectos: list[str], consulta: str = "",
               solo_vigentes: bool = True, limite: int = 300) -> list[Any]:
    filtro_estado = ("AND m.estado = 'vigente' AND (m.vence IS NULL OR m.vence > now())" if solo_vigentes else "")
    rango = ("ts_rank_cd(r.tsv, CAST(replace(websearch_to_tsquery('spanish', :q)::text, ' & ', ' | ') AS tsquery))" if consulta else "0")
    filas = con.execute(text(
        f"SELECT {COLUMNAS}, {rango} AS rango FROM memorias m JOIN memoria_revisiones r "
        f"ON r.memoria_id = m.id AND r.revision = m.revision_actual "
        f"WHERE m.proyecto_id = ANY(:proyectos) {filtro_estado} AND {_visibles_sql()} "
        f"ORDER BY m.prioridad DESC, rango DESC, m.actualizado DESC LIMIT :lim"),
        {"proyectos": proyectos, "q": consulta, "lim": limite, **parametros_visibilidad(p)}).all()
    return [f for f in filas if puede_leer_memoria(p, f.proyecto_id, f.alcance, f.propietario, f.compartida_con)]


def buscar(con: Connection, p: Principal, proyectos: list[str], consulta: str, limite: int = 10) -> list[Any]:
    filas = con.execute(text(
        f"SELECT {COLUMNAS}, ts_rank_cd(r.tsv, CAST(replace(websearch_to_tsquery('spanish', :q)::text, ' & ', ' | ') AS tsquery)) AS rango FROM memorias m "
        f"JOIN memoria_revisiones r ON r.memoria_id = m.id AND r.revision = m.revision_actual "
        f"WHERE m.proyecto_id = ANY(:proyectos) AND m.estado = 'vigente' AND {_visibles_sql()} "
        f"AND r.tsv @@ CAST(replace(websearch_to_tsquery('spanish', :q)::text, ' & ', ' | ') AS tsquery) ORDER BY rango DESC, m.prioridad DESC LIMIT :lim"),
        {"proyectos": proyectos, "q": consulta, "lim": limite, **parametros_visibilidad(p)}).all()
    return [f for f in filas if puede_leer_memoria(p, f.proyecto_id, f.alcance, f.propietario, f.compartida_con)]


def ver(con: Connection, p: Principal, mid: int, con_historia: bool = False) -> dict:
    fila = con.execute(text(f"SELECT {COLUMNAS} FROM memorias m JOIN memoria_revisiones r ON r.memoria_id = m.id "
                            f"AND r.revision = m.revision_actual WHERE m.id = :i"), {"i": mid}).first()
    if not fila or not puede_leer_memoria(p, fila.proyecto_id, fila.alcance, fila.propietario, fila.compartida_con):
        if fila and p.nivel(fila.proyecto_id) >= 1:
            raise Denegado("Memoria privada de otro agente")
        raise NoEncontrado(f"No existe la memoria {mid}")
    datos = dict(fila._mapping)
    if con_historia:
        datos["historia"] = [dict(r._mapping) for r in con.execute(text(
            "SELECT revision, titulo, autor, origen, motivo, creado, hash FROM memoria_revisiones "
            "WHERE memoria_id = :i ORDER BY revision"), {"i": mid})]
    return datos
