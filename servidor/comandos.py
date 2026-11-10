"""Comandos de escritura: cada uno en UNA transacción con idempotencia y auditoría.

Contrato: ejecutar(conexion, principal, comando) → respuesta. Si la misma clave_idem llega otra vez con los mismos
datos, devuelve la respuesta original (repetido=True) sin repetir el efecto; con otros datos, Conflicto.
Nada confirma progreso antes del COMMIT: el cliente solo da por guardado lo que recibió con respuesta OK.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any

from sqlalchemy import Connection, text

import redaccion
from servidor import memorias, rag
from servidor.modelos import (MODELOS, Checkpoint, Comando, OperacionIniciar, OperacionResolver, SesionAbrir,
                              SesionCerrar, SesionLatido, TareaGuardar)
from servidor.permisos import Conflicto, Denegado, Invalido, NoEncontrado, Principal

SESION_CALLADA_MIN = 45


def _json(valor: Any) -> str:
    return json.dumps(valor, ensure_ascii=False, sort_keys=True, default=str)


def auditar(con: Connection, p: Principal, accion: str, entidad: str, entidad_id: Any, proyecto: str | None,
            clave: str | None = None, detalle: dict | None = None) -> None:
    con.execute(text(
        "INSERT INTO eventos (principal, tipo_credencial, agente_id, accion, entidad, entidad_id, proyecto_id, "
        "clave_idem, detalle) VALUES (:pr, :tc, :ag, :ac, :en, :id, :py, :cl, CAST(:de AS jsonb))"),
        {"pr": p.nombre, "tc": p.tipo, "ag": p.agente, "ac": accion, "en": entidad, "id": str(entidad_id),
         "py": proyecto, "cl": clave, "de": _json(detalle or {})})


def tarea_id(con: Connection, proyecto: str, clave: str | None, crear: bool = False) -> int | None:
    if not clave:
        return None
    fila = con.execute(text("SELECT id FROM tareas WHERE proyecto_id = :p AND clave = :c"),
                       {"p": proyecto, "c": clave}).first()
    if fila:
        return fila.id
    if not crear:
        raise NoEncontrado(f"No existe la tarea {proyecto}/{clave}")
    return con.execute(text("INSERT INTO tareas (proyecto_id, clave, titulo) VALUES (:p, :c, :c) RETURNING id"),
                       {"p": proyecto, "c": clave}).scalar_one()


def marcar_interrumpidas(con: Connection, agente: str | None = None) -> int:
    """Sesiones activas sin latido hace rato → interrumpidas; sus intenciones abiertas → inciertas (no se reejecutan)."""
    filas = con.execute(text(
        "WITH s AS (UPDATE sesiones SET estado = 'interrumpida' WHERE estado = 'activa' "
        f"AND ultimo_latido < now() - interval '{SESION_CALLADA_MIN} minutes' "
        "AND (CAST(:a AS text) IS NULL OR agente_id = :a) RETURNING id) "
        "UPDATE operaciones SET estado = 'incierta', version = version + 1, actualizado = now(), "
        "resultado = resultado || '{\"motivo\": \"la sesión se cortó con la operación sin resolver\"}' "
        "WHERE estado = 'intencion' AND sesion_id IN (SELECT id FROM s) RETURNING id"), {"a": agente})
    return len(filas.all())


# ---------------------------------------------------------------- sesiones y checkpoints

def sesion_abrir(con: Connection, p: Principal, d: SesionAbrir) -> dict:
    actor = p.actuar_como(d.agente)
    p.exigir(d.proyecto, "escritura" if actor != p.agente else "lectura")
    marcar_interrumpidas(con, actor)
    tid = tarea_id(con, d.proyecto, d.tarea, crear=p.nivel(d.proyecto) >= 2)
    fila = con.execute(text("SELECT agente_id, proyecto_id, estado FROM sesiones WHERE id = :i FOR UPDATE"),
                       {"i": d.id}).first()
    if fila:
        if (fila.agente_id, fila.proyecto_id) != (actor, d.proyecto):
            raise Conflicto("Ese id de sesión ya pertenece a otro agente o proyecto")
        con.execute(text("UPDATE sesiones SET estado = CASE WHEN estado = 'interrumpida' THEN 'activa' ELSE estado END, "
                         "ultimo_latido = now(), tarea_id = coalesce(CAST(:t AS bigint), tarea_id) WHERE id = :i"),
                    {"i": d.id, "t": tid})
        return {"sesion": str(d.id), "reabierta": True, "agente": actor}
    con.execute(text(
        "INSERT INTO sesiones (id, agente_id, proyecto_id, tarea_id, canal, sesion_runtime) "
        "VALUES (:i, :a, :p, :t, :c, :r)"),
        {"i": d.id, "a": actor, "p": d.proyecto, "t": tid, "c": d.canal, "r": d.sesion_runtime})
    return {"sesion": str(d.id), "reabierta": False, "agente": actor}


def _sesion_propia(con: Connection, p: Principal, sesion_id) -> Any:
    fila = con.execute(text("SELECT id, agente_id, proyecto_id, estado FROM sesiones WHERE id = :i FOR UPDATE"),
                       {"i": sesion_id}).first()
    if not fila:
        raise NoEncontrado("Sesión inexistente")
    if fila.agente_id != p.agente and p.tipo not in ("orquestador", "admin"):
        raise Denegado("La sesión es de otro agente")
    return fila


def sesion_latido(con: Connection, p: Principal, d: SesionLatido) -> dict:
    s = _sesion_propia(con, p, d.id)
    if s.estado == "cerrada":
        raise Conflicto("La sesión ya está cerrada")
    mecanico = redaccion.limpiar_json(d.estado_mecanico, 400)
    con.execute(text("UPDATE sesiones SET ultimo_latido = now(), estado = 'activa', "
                     "estado_mecanico = estado_mecanico || CAST(:m AS jsonb) WHERE id = :i"),
                {"i": d.id, "m": _json(mecanico)})
    return {"sesion": str(d.id), "reactivada": s.estado == "interrumpida"}


def sesion_cerrar(con: Connection, p: Principal, d: SesionCerrar) -> dict:
    s = _sesion_propia(con, p, d.id)
    if s.estado == "cerrada":
        return {"sesion": str(d.id), "ya_cerrada": True}
    resumen, _ = redaccion.limpiar(d.resumen_final, 2000)
    con.execute(text("UPDATE sesiones SET estado = :e, fin = now(), resumen_final = :r WHERE id = :i"),
                {"i": d.id, "e": d.estado, "r": resumen})
    inciertas = con.execute(text(
        "UPDATE operaciones SET estado = 'incierta', version = version + 1, actualizado = now() "
        "WHERE sesion_id = :i AND estado = 'intencion' RETURNING clave"), {"i": d.id}).scalars().all()
    return {"sesion": str(d.id), "estado": d.estado, "operaciones_inciertas": inciertas}


def checkpoint(con: Connection, p: Principal, d: Checkpoint) -> dict:
    actor = p.actuar_como(d.agente)
    p.exigir(d.proyecto, "escritura")
    s = _sesion_propia(con, p, d.sesion_id)
    if (s.agente_id, s.proyecto_id) != (actor, d.proyecto):
        raise Conflicto("La sesión no corresponde a ese agente/proyecto")
    tid = tarea_id(con, d.proyecto, d.tarea, crear=True)
    secuencia = con.execute(text("SELECT coalesce(max(secuencia), 0) + 1 FROM checkpoints WHERE sesion_id = :s"),
                            {"s": d.sesion_id}).scalar_one()
    datos = redaccion.limpiar_json(d.model_dump(mode="json", exclude={"sesion_id", "proyecto", "tarea", "agente",
                                                                       "origen"}), 800)
    nuevo = con.execute(text(
        "INSERT INTO checkpoints (sesion_id, agente_id, proyecto_id, tarea_id, secuencia, haciendo, completado, "
        "pendiente, archivos, pruebas, efectos_confirmados, reanudar_desde, git, origen) VALUES (:s, :a, :p, :t, "
        ":n, :h, CAST(:c AS jsonb), CAST(:pe AS jsonb), CAST(:ar AS jsonb), CAST(:pr AS jsonb), CAST(:ef AS jsonb), "
        ":re, CAST(:g AS jsonb), :o) RETURNING id"),
        {"s": d.sesion_id, "a": actor, "p": d.proyecto, "t": tid, "n": secuencia, "h": datos["haciendo"],
         "c": _json(datos["completado"]), "pe": _json(datos["pendiente"]), "ar": _json(datos["archivos"]),
         "pr": _json(datos["pruebas"]), "ef": _json(datos["efectos_confirmados"]), "re": datos["reanudar_desde"],
         "g": _json(datos["git"]), "o": d.origen}).scalar_one()
    con.execute(text("UPDATE sesiones SET ultimo_latido = now(), tarea_id = coalesce(CAST(:t AS bigint), tarea_id) "
                     "WHERE id = :s"), {"s": d.sesion_id, "t": tid})
    return {"checkpoint": nuevo, "secuencia": secuencia, "tarea_id": tid}


# ---------------------------------------------------------------- tareas

def tarea_guardar(con: Connection, p: Principal, d: TareaGuardar) -> dict:
    p.exigir(d.proyecto, "escritura")
    campos = {k: v for k, v in d.model_dump(mode="json", exclude={"proyecto", "clave", "version_esperada"}).items()
              if v is not None}
    campos = redaccion.limpiar_json(campos, 2000)
    fila = con.execute(text("SELECT id, version FROM tareas WHERE proyecto_id = :p AND clave = :c FOR UPDATE"),
                       {"p": d.proyecto, "c": d.clave}).first()
    if fila is None:
        if d.version_esperada not in (None,):
            raise Conflicto(f"La tarea {d.clave} no existe (se esperaba versión {d.version_esperada})")
        campos.setdefault("titulo", d.clave)
        cols = ["proyecto_id", "clave", *campos]
        valores = {"proyecto_id": d.proyecto, "clave": d.clave, **campos}
    elif d.version_esperada is not None and d.version_esperada != fila.version:
        raise Conflicto(f"La tarea {d.clave} está en la versión {fila.version}, no {d.version_esperada}: releer y reintentar")
    exprs = {"criterios": "CAST(:criterios AS jsonb)", "artefactos": "CAST(:artefactos AS jsonb)"}
    for k in ("criterios", "artefactos"):
        if k in campos:
            campos[k] = _json(campos[k])
    if fila is None:
        valores.update(campos)
        sql = (f"INSERT INTO tareas ({', '.join(cols)}) VALUES "
               f"({', '.join(exprs.get(c, ':' + c) for c in cols)}) RETURNING id, version")
        r = con.execute(text(sql), valores).first()
    else:
        asignaciones = ", ".join(f"{c} = {exprs.get(c, ':' + c)}" for c in campos)
        sql = (f"UPDATE tareas SET {asignaciones + ', ' if asignaciones else ''}version = version + 1, "
               f"actualizado = now() WHERE id = :id RETURNING id, version")
        r = con.execute(text(sql), {**campos, "id": fila.id}).first()
    return {"tarea_id": r.id, "clave": d.clave, "version": r.version}


# ---------------------------------------------------------------- operaciones con efectos externos

def operacion_iniciar(con: Connection, p: Principal, d: OperacionIniciar) -> dict:
    actor = p.actuar_como(d.agente)
    p.exigir(d.proyecto, "escritura")
    fila = con.execute(text("SELECT id, estado, agente_id, version, resultado FROM operaciones "
                            "WHERE proyecto_id = :p AND clave = :c FOR UPDATE"), {"p": d.proyecto, "c": d.clave}).first()
    if fila:
        aviso = {"intencion": "ya registrada y sin resolver: su resultado es desconocido → verificar, no reejecutar",
                 "incierta": "resultado incierto → reconciliar antes de cualquier reintento",
                 "confirmada": "ya se hizo", "fallida": "falló: para reintentar usá una clave nueva",
                 "reconciliada": "ya reconciliada"}[fila.estado]
        return {"operacion": fila.id, "estado": fila.estado, "puede_ejecutar": False, "aviso": aviso,
                "version": fila.version}
    tid = tarea_id(con, d.proyecto, d.tarea, crear=True)
    descripcion, _ = redaccion.limpiar(d.descripcion, 600)
    nuevo = con.execute(text(
        "INSERT INTO operaciones (proyecto_id, clave, agente_id, sesion_id, tarea_id, tipo, descripcion, destructiva) "
        "VALUES (:p, :c, :a, :s, :t, :ti, :de, :d) RETURNING id"),
        {"p": d.proyecto, "c": d.clave, "a": actor, "s": d.sesion_id, "t": tid, "ti": d.tipo, "de": descripcion,
         "d": d.destructiva}).scalar_one()
    return {"operacion": nuevo, "estado": "intencion", "puede_ejecutar": True, "version": 1}


TRANSICIONES = {"intencion": {"confirmada", "fallida", "incierta"},
                "incierta": {"confirmada", "fallida", "reconciliada"}}


def operacion_resolver(con: Connection, p: Principal, d: OperacionResolver) -> dict:
    fila = con.execute(text("SELECT id, estado, agente_id, version FROM operaciones "
                            "WHERE proyecto_id = :p AND clave = :c FOR UPDATE"), {"p": d.proyecto, "c": d.clave}).first()
    if not fila:
        p.exigir(d.proyecto, "lectura")
        raise NoEncontrado(f"No existe la operación {d.clave}")
    if fila.agente_id != p.agente and p.tipo not in ("orquestador", "admin"):
        p.exigir(d.proyecto, "curador")
    if d.version_esperada is not None and d.version_esperada != fila.version:
        raise Conflicto(f"La operación está en la versión {fila.version}")
    if d.estado not in TRANSICIONES.get(fila.estado, set()):
        raise Conflicto(f"Transición inválida {fila.estado} → {d.estado}")
    con.execute(text("UPDATE operaciones SET estado = :e, resultado = CAST(:r AS jsonb), version = version + 1, "
                     "actualizado = now() WHERE id = :i"),
                {"e": d.estado, "r": _json(redaccion.limpiar_json(d.resultado, 600)), "i": fila.id})
    return {"operacion": fila.id, "estado": d.estado, "version": fila.version + 1}


MANEJADORES = {"sesion_abrir": sesion_abrir, "sesion_latido": sesion_latido, "sesion_cerrar": sesion_cerrar,
               "checkpoint": checkpoint, "tarea_guardar": tarea_guardar, "operacion_iniciar": operacion_iniciar,
               "operacion_resolver": operacion_resolver, "memoria_recordar": memorias.recordar,
               "memoria_estado": memorias.cambiar_estado, "documento_indexar": rag.indexar,
               "documento_retirar": rag.retirar, "fragmentos_embedding": rag.completar_embeddings}


def ejecutar(con: Connection, p: Principal, comando: Comando) -> dict:
    """Valida, aplica idempotencia, ejecuta y audita. Debe llamarse dentro de una transacción."""
    huella = hashlib.sha256(_json({"tipo": comando.tipo, "datos": comando.datos}).encode()).hexdigest()
    con.execute(text("SELECT pg_advisory_xact_lock(hashtextextended(:k, 7))"), {"k": f"{p.nombre}|{comando.clave_idem}"})
    previa = con.execute(text("SELECT comando, hash_peticion, respuesta FROM idempotencia "
                              "WHERE principal = :p AND clave = :k"), {"p": p.nombre, "k": comando.clave_idem}).first()
    if previa:
        if previa.hash_peticion != huella:
            raise Conflicto("Esa clave_idem ya se usó con otro contenido")
        return {**previa.respuesta, "repetido": True}
    try:
        datos = MODELOS[comando.tipo].model_validate(comando.datos)
    except ValueError as e:
        raise Invalido(str(e)[:800]) from e
    respuesta = MANEJADORES[comando.tipo](con, p, datos)
    con.execute(text("INSERT INTO idempotencia (principal, clave, comando, hash_peticion, respuesta) "
                     "VALUES (:p, :k, :c, :h, CAST(:r AS jsonb))"),
                {"p": p.nombre, "k": comando.clave_idem, "c": comando.tipo, "h": huella, "r": _json(respuesta)})
    proyecto = comando.datos.get("proyecto") if isinstance(comando.datos, dict) else None
    entidad_id = next((respuesta.get(k) for k in ("memoria", "checkpoint", "tarea_id", "operacion", "sesion")
                       if respuesta.get(k) is not None), "")
    auditar(con, p, comando.tipo, comando.tipo.split("_")[0], entidad_id, proyecto, comando.clave_idem,
            {k: v for k, v in respuesta.items() if k in ("estado", "revision", "secuencia", "sospechoso", "redactado",
                                                          "version", "sin_cambios")})
    return {**respuesta, "repetido": False}
