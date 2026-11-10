"""Hooks de Claude Code → memoria persistente (ver README: «Claude Code hooks»).

  session-start  (startup|resume|clear|compact): abre/reabre la sesión y devuelve el paquete de contexto
                 como additionalContext. Tras una compactación vuelve a inyectar lo esencial.
  post-tool-use  (Edit|Write|MultiEdit|NotebookEdit): anota el archivo tocado en el estado local (sin red).
  stop           (fin de cada turno): latido con el estado mecánico (archivos, git) a la cola local + envío en
                 segundo plano. No bloquea.
  pre-compact    : checkpoint automático (mecánico) antes de perder contexto; intenta confirmarlo en el acto.
  session-end    : checkpoint automático si hubo cambios + cierre de la sesión.

Lo semántico (qué decidí, qué sigue) lo guarda el modelo con `dfmem checkpoint/recordar`; los hooks garantizan
lo mecánico aunque el modelo no lo haga. Nunca se guarda la conversación ni el razonamiento del modelo.
Un hook nunca rompe la sesión: ante cualquier error sale 0 y lo anota en hooks.log.
"""
from __future__ import annotations

import json
import sys
import time
import traceback
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import cliente  # noqa: E402
import dfmem  # noqa: E402

AGENTE = "claude"
NS = uuid.UUID("6f1f3c52-2c2b-4b8e-9a0e-df0000000001")
OPERACION = {"startup": "inicio", "resume": "reanudar", "clear": "inicio", "compact": "compactacion"}
ESPERA = {"startup": 45, "resume": 45, "clear": 20, "compact": 20}
GUIA = (f"Memoria (dfmem = `python {Path(__file__).resolve().parent.as_posix()}/dfmem.py`): al cerrar cada paso concreto "
        "`dfmem checkpoint --haciendo … --hecho … --pendiente … --reanudar …`; decisiones/lecciones "
        "`dfmem recordar --tipo decision|leccion|error_conocido --clave … --titulo … --contenido …`; al cambiar de "
        "tarea `dfmem tarea usar <clave>`; más contexto `dfmem buscar|rag \"…\"`. Antes de un efecto externo o "
        "destructivo: `dfmem operacion iniciar <clave> --destructiva` (código 3 = NO ejecutar).")


def _log(texto: str) -> None:
    ruta = cliente.DIR / "hooks.log"
    ruta.parent.mkdir(parents=True, exist_ok=True)
    with open(ruta, "a", encoding="utf-8") as f:
        f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {texto}\n")


def _estado_local(sesion: str) -> Path:
    return cliente.DIR / "sesiones" / f"claude-{sesion}.estado.json"


def _leer_estado(sesion: str) -> dict:
    try:
        return json.loads(_estado_local(sesion).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"archivos": [], "ultimo_auto": 0, "archivos_en_ultimo_auto": 0}


def _guardar_estado(sesion: str, estado: dict) -> None:
    cliente._escribir_atomico(_estado_local(sesion), json.dumps(estado, ensure_ascii=False))


def _ids(entrada: dict) -> tuple[str, str, str]:
    runtime = str(entrada.get("session_id") or "sin-id")
    proyecto = dfmem.proyecto_de(entrada.get("cwd"))
    return runtime, str(uuid.uuid5(NS, runtime)), proyecto


def session_start(entrada: dict) -> None:
    runtime, sid, proyecto = _ids(entrada)
    if not proyecto:   # carpeta fuera de los juegos/proyectos de la factory: no se registra nada
        return
    origen = entrada.get("source", "startup")
    previa = dfmem.sesion_actual(AGENTE)
    tarea = previa.get("tarea") if previa.get("id") == sid else None   # misma conversación: conserva la tarea
    dfmem.guardar_sesion(AGENTE, {"id": sid, "proyecto": proyecto, "tarea": tarea, "canal": "claude-code",
                                  "runtime": runtime})
    espera = ESPERA.get(origen, 30)
    abrir = cliente.comando(AGENTE, "sesion_abrir", {"id": sid, "proyecto": proyecto, "canal": "claude-code",
                                                     "sesion_runtime": runtime, "tarea": tarea},
                            clave=f"abrir-{sid}-{origen}-{int(time.time() // 60)}", espera_s=espera)
    paquete = cliente.contexto(AGENTE, proyecto, espera_s=5 if abrir.get("confirmado") else 0,
                               operacion=OPERACION.get(origen, "inicio"), sesion_id=sid, tarea=tarea)
    texto = paquete["texto"] + "\n" + GUIA
    if not paquete.get("confirmado"):
        cliente.vaciar_en_segundo_plano(AGENTE)
    print(json.dumps({"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": texto}},
                     ensure_ascii=True))
    _log(f"session-start {origen} {sid} proyecto={proyecto} confirmado={paquete.get('confirmado')} "
         f"tokens={paquete.get('tokens')}")


def post_tool_use(entrada: dict) -> None:
    _, sid, proyecto = _ids(entrada)
    if not proyecto:   # carpeta fuera de los juegos/proyectos de la factory: no se registra nada
        return
    ruta = (entrada.get("tool_input") or {}).get("file_path") or (entrada.get("tool_input") or {}).get("notebook_path")
    if not ruta:
        return
    raiz = str(dfmem.raiz_de(proyecto)).replace("\\", "/").lower()
    rel = str(ruta).replace("\\", "/")
    if rel.lower().startswith(raiz + "/"):
        rel = rel[len(raiz) + 1:]
    estado = _leer_estado(sid)
    if rel not in estado["archivos"]:
        estado["archivos"] = (estado["archivos"] + [rel])[-200:]
        _guardar_estado(sid, estado)


def _mecanico(sid: str, proyecto: str) -> dict:
    estado = _leer_estado(sid)
    return {"archivos_tocados": estado["archivos"][-40:], "git": dfmem.git_info(dfmem.raiz_de(proyecto)),
            "ultimo_turno": time.strftime("%Y-%m-%d %H:%M:%S")}


def stop(entrada: dict) -> None:
    if entrada.get("stop_hook_active"):
        return
    _, sid, proyecto = _ids(entrada)
    if not proyecto:
        return
    cliente.encolar(AGENTE, "sesion_latido", {"id": sid, "estado_mecanico": _mecanico(sid, proyecto)})
    cliente.vaciar_en_segundo_plano(AGENTE, 60)


def _checkpoint_automatico(sid: str, proyecto: str, motivo: str, forzar: bool) -> bool:
    estado = _leer_estado(sid)
    if not forzar and len(estado["archivos"]) == estado.get("archivos_en_ultimo_auto", 0):
        return False
    tarea = dfmem.sesion_actual(AGENTE).get("tarea") if dfmem.sesion_actual(AGENTE).get("id") == sid else None
    nuevos = estado["archivos"][estado.get("archivos_en_ultimo_auto", 0):]
    cliente.encolar(AGENTE, "checkpoint", {
        "sesion_id": sid, "proyecto": proyecto, "tarea": tarea, "origen": "automatico",
        "haciendo": f"[automático: {motivo}] estado mecánico; lo semántico está en el último checkpoint del modelo",
        "archivos": estado["archivos"][-80:], "git": dfmem.git_info(dfmem.raiz_de(proyecto)),
        "reanudar_desde": ("Revisar el último checkpoint del modelo y estos archivos tocados"
                           + (f" (nuevos desde el anterior automático: {len(nuevos)})" if nuevos else ""))})
    estado.update(ultimo_auto=time.time(), archivos_en_ultimo_auto=len(estado["archivos"]))
    _guardar_estado(sid, estado)
    return True


def pre_compact(entrada: dict) -> None:
    _, sid, proyecto = _ids(entrada)
    if not proyecto:   # carpeta fuera de los juegos/proyectos de la factory: no se registra nada
        return
    _checkpoint_automatico(sid, proyecto, f"antes de compactar ({entrada.get('trigger', '?')})", forzar=True)
    r = cliente.vaciar(AGENTE, 15)
    if r.get("pendientes"):
        cliente.vaciar_en_segundo_plano(AGENTE)
    _log(f"pre-compact {sid} {r}")


def session_end(entrada: dict) -> None:
    _, sid, proyecto = _ids(entrada)
    if not proyecto:   # carpeta fuera de los juegos/proyectos de la factory: no se registra nada
        return
    _checkpoint_automatico(sid, proyecto, f"fin de sesión ({entrada.get('reason', '?')})", forzar=False)
    estado = _leer_estado(sid)
    cliente.encolar(AGENTE, "sesion_cerrar", {
        "id": sid, "estado": "cerrada",
        "resumen_final": f"Cierre ({entrada.get('reason', '?')}); {len(estado['archivos'])} archivos tocados."})
    r = cliente.vaciar(AGENTE, 10)
    if r.get("pendientes"):
        cliente.vaciar_en_segundo_plano(AGENTE)
    _log(f"session-end {sid} {r}")


EVENTOS = {"session-start": session_start, "post-tool-use": post_tool_use, "stop": stop, "pre-compact": pre_compact,
           "session-end": session_end}


def main() -> int:
    evento = sys.argv[1] if len(sys.argv) > 1 else ""
    try:
        entrada = json.loads(sys.stdin.buffer.read().decode("utf-8-sig") or "{}")
        EVENTOS[evento](entrada)
    except Exception:  # noqa: BLE001  (un hook no debe romper la sesión)
        _log(f"ERROR {evento}: {traceback.format_exc()[-1500:]}")
        if evento == "session-start":
            print(json.dumps({"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext":
                              "⚠ La memoria persistente falló al iniciar (ver "
                              f"{cliente.DIR / 'hooks.log'}); no hay contexto recuperado. {GUIA}"}},
                             ensure_ascii=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
