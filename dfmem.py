"""dfmem — memoria persistente de DreamFactory desde la línea de comandos (cualquier agente; solo stdlib).

  python dfmem.py [--agente claude] [--proyecto <id>] <orden> ...

Órdenes: estado · contexto · tarea usar|guardar|ver · checkpoint · recordar · olvidar · buscar · ver · rag ·
         operacion iniciar|resolver|abiertas · indexar · vaciar · consumo
Salida: texto breve (o --json). Código 0 = confirmado en PostgreSQL; 2 = PENDIENTE en cola local (no confirmado);
3 = operación que NO debe ejecutarse (ya hecha o incierta); 1 = error/denegado.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import cliente  # noqa: E402

for _flujo in (sys.stdout, sys.stderr):   # Windows: las tuberías no son UTF-8 por defecto
    try:
        _flujo.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

try:   # optional: DreamFactory's game catalog (juegos.py next to this folder). Standalone = config/cliente.json only.
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    import juegos  # noqa: E402
except ImportError:
    juegos = None


def _config() -> dict:
    """Proyectos de memoria = juegos del catálogo + los de config/cliente.json (fábrica, AGF, pruebas)."""
    base = json.loads((Path(__file__).resolve().parent / "config" / "cliente.json").read_text(encoding="utf-8"))
    for j in (juegos.catalogo().values() if juegos else []):
        base["proyectos"][j["memoria"]] = {"raiz": j["raiz"], "raices": j["raices"], "corpus_base": j["corpus_base"],
                                           "corpus": j["corpus"]}
    aqui = Path(__file__).resolve().parent
    for datos in base["proyectos"].values():   # relative roots are relative to this repository
        for k in ("raiz", "corpus_base"):
            if datos.get(k) and not Path(datos[k]).is_absolute() and not datos[k].startswith("//"):
                datos[k] = (aqui / datos[k]).as_posix()
    return base


CONFIG = _config()


def proyecto_de(ruta: str | None) -> str | None:
    """Proyecto de memoria de una carpeta (la raíz más específica gana); None si no pertenece a ninguno."""
    ruta_n = str(Path(ruta or os.getcwd()).resolve()).replace("\\", "/").lower().rstrip("/") + "/"
    mejor, largo = None, 0
    for clave, datos in CONFIG["proyectos"].items():
        for raiz in datos.get("raices") or [datos["raiz"]]:
            r = raiz.replace("\\", "/").lower().rstrip("/") + "/"
            if ruta_n.startswith(r) and len(r) > largo:
                mejor, largo = clave, len(r)
    return mejor


def raiz_de(proyecto: str) -> Path:
    return Path(CONFIG["proyectos"].get(proyecto, {}).get("raiz") or os.getcwd())


def git_info(raiz: Path) -> dict:
    def git(*a: str) -> str:
        try:
            return subprocess.run(["git", "-C", str(raiz), *a], capture_output=True, text=True, timeout=10,
                                  encoding="utf-8", errors="replace", stdin=subprocess.DEVNULL,
                                  creationflags=cliente.OCULTO).stdout.strip()
        except (OSError, subprocess.SubprocessError):
            return ""
    estado = git("status", "--porcelain")
    return {"head": git("rev-parse", "--short", "HEAD"), "rama": git("rev-parse", "--abbrev-ref", "HEAD"),
            "cambios_sin_commit": len([l for l in estado.splitlines() if l.strip()])}


def _sesion_ruta(agente: str) -> Path:
    return cliente.DIR / "sesiones" / f"{agente}.json"


def sesion_actual(agente: str) -> dict:
    try:
        return json.loads(_sesion_ruta(agente).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def guardar_sesion(agente: str, datos: dict) -> None:
    cliente._escribir_atomico(_sesion_ruta(agente), json.dumps(datos, ensure_ascii=False, indent=1))


def asegurar_sesion(agente: str, proyecto: str, canal: str = "cli") -> dict:
    s = sesion_actual(agente)
    if s.get("id") and s.get("proyecto") == proyecto:
        return s
    import uuid
    s = {"id": str(uuid.uuid4()), "proyecto": proyecto, "tarea": None, "canal": canal}
    cliente.comando(agente, "sesion_abrir", {"id": s["id"], "proyecto": proyecto, "canal": canal})
    guardar_sesion(agente, s)
    return s


def _salida(args, r: dict, texto: str) -> int:
    print(json.dumps(r, ensure_ascii=False, default=str, indent=1) if args.json else texto)
    if r.get("pendiente"):
        print("⚠ PENDIENTE: PostgreSQL no confirmó; quedó en la cola local (`dfmem vaciar`).", file=sys.stderr)
        return 2
    return 0


def _clave_valor(items: list[str] | None) -> list[dict]:
    salida = []
    for it in items or []:
        nombre, _, resultado = it.partition("=")
        salida.append({"nombre": nombre.strip(), "resultado": resultado.strip() or "?"})
    return salida


def orden_contexto(a) -> int:
    s = sesion_actual(a.agente)
    r = cliente.contexto(a.agente, a.proyecto, espera_s=a.esperar, tarea=a.tarea or s.get("tarea"),
                         operacion=a.operacion, consulta=a.consulta, presupuesto=a.presupuesto,
                         sesion_id=s.get("id") if s.get("proyecto") == a.proyecto else None)
    print(r["texto"] if not a.json else json.dumps(r, ensure_ascii=False, indent=1))
    if r.get("confirmado"):
        print(f"[{r['tokens']} tokens de ~{r['presupuesto']} · contexto completo ≈ {r['tokens_contexto_completo']}]",
              file=sys.stderr)
    return 0 if r.get("confirmado") else 2


def orden_tarea(a) -> int:
    if a.accion == "usar":
        s = asegurar_sesion(a.agente, a.proyecto)
        s["tarea"] = a.clave
        guardar_sesion(a.agente, s)
        cliente.comando(a.agente, "sesion_abrir", {"id": s["id"], "proyecto": a.proyecto, "canal": s.get("canal", "cli"),
                                                   "tarea": a.clave})
        a.tarea, a.operacion, a.consulta, a.presupuesto = a.clave, "cambio_tarea", "", None
        return orden_contexto(a)
    if a.accion == "ver":
        print(json.dumps(cliente.http("GET", f"/v1/tareas/{a.proyecto}/{a.clave}", agente=a.agente),
                         ensure_ascii=False, indent=1, default=str))
        return 0
    datos = {"proyecto": a.proyecto, "clave": a.clave, "titulo": a.titulo, "objetivo": a.objetivo, "estado": a.estado,
             "proxima_accion": a.proxima, "bloqueos": a.bloqueos, "responsable": a.responsable,
             "criterios": a.criterio, "version_esperada": a.version}
    r = cliente.comando(a.agente, "tarea_guardar", {k: v for k, v in datos.items() if v is not None})
    return _salida(a, r, f"tarea {a.clave} → versión {r.get('version', '?')}")


def orden_checkpoint(a) -> int:
    s = asegurar_sesion(a.agente, a.proyecto)
    datos = {"sesion_id": s["id"], "proyecto": a.proyecto, "tarea": a.tarea or s.get("tarea"),
             "haciendo": a.haciendo or "", "completado": a.hecho or [], "pendiente": a.pendiente or [],
             "archivos": a.archivo or [], "pruebas": _clave_valor(a.prueba), "efectos_confirmados": a.efecto or [],
             "reanudar_desde": a.reanudar or "", "git": git_info(raiz_de(a.proyecto)), "origen": "modelo"}
    r = cliente.comando(a.agente, "checkpoint", {k: v for k, v in datos.items() if v is not None})
    return _salida(a, r, f"checkpoint #{r.get('checkpoint', '?')} (secuencia {r.get('secuencia', '?')}) confirmado")


def orden_recordar(a) -> int:
    refs = [{"ruta": r} for r in a.ref or []]
    datos = {"proyecto": a.proyecto, "alcance": a.alcance, "clave": a.clave, "tipo": a.tipo, "prioridad": a.prioridad,
             "titulo": a.titulo, "contenido": a.contenido or "", "refs": refs, "etiquetas": a.etiqueta or [],
             "tarea": a.tarea, "reemplaza": [int(x) for x in (a.reemplaza or "").split(",") if x.strip()],
             "revision_esperada": a.revision, "fuente": a.fuente or "", "motivo": a.motivo or ""}
    r = cliente.comando(a.agente, "memoria_recordar", {k: v for k, v in datos.items() if v is not None})
    extra = " (sin cambios)" if r.get("sin_cambios") else ""
    avisos = (" · ⚠ contenido sospechoso neutralizado" if r.get("sospechoso") else "") + \
             (f" · secretos redactados: {', '.join(r['redactado'])}" if r.get("redactado") else "")
    return _salida(a, r, f"memoria m{r.get('memoria', '?')} rev {r.get('revision', '?')}{extra}{avisos}")


def orden_operacion(a) -> int:
    if a.accion == "abiertas":
        print(json.dumps(cliente.http("GET", f"/v1/operaciones?proyecto={a.proyecto}", agente=a.agente)["operaciones"],
                         ensure_ascii=False, indent=1, default=str))
        return 0
    if a.accion == "iniciar":
        s = asegurar_sesion(a.agente, a.proyecto)
        r = cliente.comando(a.agente, "operacion_iniciar", {
            "proyecto": a.proyecto, "clave": a.clave, "tipo": a.tipo or "externa", "descripcion": a.descripcion or a.clave,
            "destructiva": a.destructiva, "sesion_id": s["id"], "tarea": s.get("tarea")}, encolar_si_falla=False)
        if not r.get("puede_ejecutar"):
            print(f"NO EJECUTAR «{a.clave}»: {r.get('aviso', r.get('estado'))}")
            return 3
        print(f"intención registrada («{a.clave}»): ejecutá y después `dfmem operacion resolver {a.clave} --estado ...`")
        return 0
    r = cliente.comando(a.agente, "operacion_resolver", {"proyecto": a.proyecto, "clave": a.clave, "estado": a.estado,
                                                         "resultado": json.loads(a.resultado or "{}")})
    return _salida(a, r, f"operación {a.clave} → {r.get('estado', '?')}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="dfmem", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--agente", default=os.environ.get("DFMEM_AGENTE", "claude"))
    ap.add_argument("--proyecto", default=None)
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--esperar", type=float, default=30, help="segundos para despertar WSL/PostgreSQL")
    sub = ap.add_subparsers(dest="orden", required=True)
    sub.add_parser("estado")
    c = sub.add_parser("contexto")
    c.add_argument("--tarea"), c.add_argument("--consulta", default=""), c.add_argument("--presupuesto", type=int)
    c.add_argument("--operacion", default="inicio",
                   choices=["inicio", "reanudar", "compactacion", "cambio_tarea", "despacho", "busqueda"])
    t = sub.add_parser("tarea")
    t.add_argument("accion", choices=["usar", "guardar", "ver"]), t.add_argument("clave")
    for o in ("titulo", "objetivo", "estado", "proxima", "bloqueos", "responsable"):
        t.add_argument(f"--{o}")
    t.add_argument("--criterio", action="append"), t.add_argument("--version", type=int)
    k = sub.add_parser("checkpoint")
    k.add_argument("--haciendo"), k.add_argument("--reanudar"), k.add_argument("--tarea")
    for o in ("hecho", "pendiente", "archivo", "prueba", "efecto"):
        k.add_argument(f"--{o}", action="append")
    r = sub.add_parser("recordar")
    r.add_argument("--alcance", default="proyecto", choices=["global", "proyecto", "tarea", "agente"])
    r.add_argument("--clave", required=True), r.add_argument("--tipo", required=True)
    r.add_argument("--prioridad", type=int, default=3), r.add_argument("--titulo", required=True)
    r.add_argument("--contenido"), r.add_argument("--tarea"), r.add_argument("--reemplaza"), r.add_argument("--fuente")
    r.add_argument("--motivo"), r.add_argument("--revision", type=int)
    r.add_argument("--ref", action="append"), r.add_argument("--etiqueta", action="append")
    o = sub.add_parser("olvidar")
    o.add_argument("id", type=int), o.add_argument("--estado", default="obsoleta", choices=["obsoleta", "retirada", "vigente"])
    o.add_argument("--motivo", required=True)
    b = sub.add_parser("buscar")
    b.add_argument("consulta"), b.add_argument("--limite", type=int, default=8)
    v = sub.add_parser("ver")
    v.add_argument("id", type=int), v.add_argument("--historia", action="store_true")
    g = sub.add_parser("rag")
    g.add_argument("consulta"), g.add_argument("--k", type=int, default=5), g.add_argument("--en", action="append")
    p = sub.add_parser("operacion")
    p.add_argument("accion", choices=["iniciar", "resolver", "abiertas"]), p.add_argument("clave", nargs="?", default="")
    p.add_argument("--tipo"), p.add_argument("--descripcion"), p.add_argument("--destructiva", action="store_true")
    p.add_argument("--estado", choices=["confirmada", "fallida", "incierta", "reconciliada"]), p.add_argument("--resultado")
    i = sub.add_parser("indexar")
    i.add_argument("--todos", action="store_true", help="todos los proyectos de config/cliente.json")
    va = sub.add_parser("vaciar")
    va.add_argument("--silencioso", action="store_true")
    sub.add_parser("consumo")
    a = ap.parse_args(argv)
    # sin --proyecto: el de la carpeta actual; fuera de un proyecto, el juego activo del panel
    a.proyecto = (a.proyecto or proyecto_de(None) or (juegos.activo()["memoria"] if juegos else None)
                  or os.environ.get("DFMEM_PROYECTO") or CONFIG.get("proyecto_defecto"))
    try:
        return _despachar(a)
    except cliente.ErrorMemoria as e:
        print(f"ERROR memoria: {e}", file=sys.stderr)
        return 2 if e.sin_conexion else 1


def _despachar(a) -> int:
    if a.orden == "estado":
        ok = cliente.disponible(a.esperar)
        info = {"api": cliente.URL, "disponible": ok, "pendientes_cola_local": cliente.pendientes(a.agente),
                "sesion_local": sesion_actual(a.agente), "proyecto": a.proyecto}
        if ok:
            info.update(salud=cliente.http("GET", "/v1/salud"), yo=cliente.http("GET", "/v1/yo", agente=a.agente))
        print(json.dumps(info, ensure_ascii=False, indent=1, default=str))
        return 0 if ok else 2
    if a.orden == "vaciar":
        r = cliente.vaciar(a.agente, a.esperar)
        if not a.silencioso:
            print(json.dumps(r, ensure_ascii=False))
        return 0 if not r.get("pendientes") else 2
    cliente.disponible(a.esperar)
    if a.orden == "contexto":
        return orden_contexto(a)
    if a.orden == "tarea":
        return orden_tarea(a)
    if a.orden == "checkpoint":
        return orden_checkpoint(a)
    if a.orden == "recordar":
        return orden_recordar(a)
    if a.orden == "operacion":
        return orden_operacion(a)
    if a.orden == "olvidar":
        r = cliente.comando(a.agente, "memoria_estado", {"id": a.id, "estado": a.estado, "motivo": a.motivo})
        return _salida(a, r, f"m{a.id}: {r.get('anterior', '?')} → {a.estado}")
    if a.orden == "buscar":
        from urllib.parse import quote
        r = cliente.http("GET", f"/v1/memorias/buscar?q={quote(a.consulta)}&proyecto={a.proyecto}&limite={a.limite}",
                         agente=a.agente)["resultados"]
        for m in r:
            print(f"m{m['id']} [{m['tipo']}·{m['alcance']}·p{m['prioridad']}] {m['titulo']}: {m['contenido'][:200]}")
        return 0
    if a.orden == "ver":
        print(json.dumps(cliente.http("GET", f"/v1/memorias/{a.id}?historia={str(a.historia).lower()}", agente=a.agente),
                         ensure_ascii=False, indent=1, default=str))
        return 0
    if a.orden == "rag":
        proyectos = a.en or list(dict.fromkeys([a.proyecto, "dreamfactory"]))
        r = cliente.http("POST", "/v1/rag/buscar", {"proyectos": proyectos, "consulta": a.consulta, "k": a.k},
                         agente=a.agente)["resultados"]
        for x in r:
            print(f"{x['ref']} · {x['encabezado']}\n   {x['extracto']}")
        return 0
    if a.orden == "indexar":
        import indexar
        return indexar.main(a.agente, list(CONFIG["proyectos"]) if a.todos else [a.proyecto])
    if a.orden == "consumo":
        for f in cliente.http("GET", "/v1/consumo", agente=a.agente)["consumo"]:
            print(f"{f['creado'][:16]} {f['agente_id']} {f['operacion']}: {f['tokens_entregados']}/{f['presupuesto']} "
                  f"tokens (completo ≈ {f['tokens_contexto_completo']}) · {f['items_incluidos']} incl. / "
                  f"{f['items_omitidos']} omit.")
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
