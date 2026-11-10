"""Indexación incremental del corpus para el RAG: solo manda lo que cambió (sha256) y retira lo que desapareció.

Corpus por proyecto en config/cliente.json. Corre con la credencial del orquestador (o la que se indique).
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import cliente  # noqa: E402
from fragmentar import fragmentar, sha256_bytes  # noqa: E402

from dfmem import CONFIG  # noqa: E402  (juegos.json + config/cliente.json)
MAXIMO_BYTES = 600_000


def _commit(raiz: Path) -> str | None:
    try:
        r = subprocess.run(["git", "-C", str(raiz), "rev-parse", "HEAD"], capture_output=True, text=True, timeout=10,
                           stdin=subprocess.DEVNULL, creationflags=cliente.OCULTO)
        return r.stdout.strip() or None if r.returncode == 0 else None
    except (OSError, subprocess.SubprocessError):
        return None


def archivos(proyecto: str) -> tuple[Path, list[Path]]:
    datos = CONFIG["proyectos"][proyecto]
    raiz = Path(datos.get("corpus_base") or datos["raiz"])
    vistos: dict[str, Path] = {}
    for patron in datos["corpus"]:
        for ruta in sorted(raiz.glob(patron)):
            if ruta.is_file() and not {"_archivo", "node_modules", ".git", "dist"} & set(ruta.parts):
                vistos[ruta.relative_to(raiz).as_posix()] = ruta
    return raiz, list(vistos.values())


def indexar_proyecto(agente: str, proyecto: str) -> dict:
    raiz, rutas = archivos(proyecto)
    if not raiz.exists():
        return {"proyecto": proyecto, "error": f"no se encuentra {raiz}"}
    estado = cliente.http("GET", f"/v1/rag/estado?proyecto={proyecto}", agente=agente)
    indexados: dict[str, str] = estado["documentos"]
    commit = _commit(raiz)
    nuevos = sin_cambios = fragmentos = 0
    presentes = set()
    for ruta in rutas:
        rel = ruta.relative_to(raiz).as_posix()
        presentes.add(rel)
        datos = ruta.read_bytes()
        if len(datos) > MAXIMO_BYTES:
            continue
        sha = sha256_bytes(datos)
        if indexados.get(rel) == sha:
            sin_cambios += 1
            continue
        fr = fragmentar(datos.decode("utf-8", errors="replace"))
        if not fr:
            continue
        r = cliente.comando(agente, "documento_indexar", {
            "proyecto": proyecto, "ruta": rel, "sha256": sha, "commit": commit, "bytes": len(datos), "fragmentos": fr},
            clave=f"doc-{proyecto}-{sha[:24]}-{sha256_bytes(rel.encode())[:12]}", encolar_si_falla=False)
        nuevos += 1
        fragmentos += r.get("fragmentos", 0)
    retirados = 0
    for rel in set(indexados) - presentes:
        cliente.comando(agente, "documento_retirar", {"proyecto": proyecto, "ruta": rel}, encolar_si_falla=False)
        retirados += 1
    return {"proyecto": proyecto, "archivos": len(rutas), "indexados": nuevos, "sin_cambios": sin_cambios,
            "fragmentos_nuevos": fragmentos, "retirados": retirados}


def main(agente: str, proyectos: list[str]) -> int:
    if agente == "claude" and (cliente.DIR / "credenciales" / "orquestador.token").exists():
        agente = "orquestador"   # la indexación es tarea de la fábrica, no de un agente
    if not cliente.disponible(45):
        print("ERROR: la memoria no responde; no se indexó nada", file=sys.stderr)
        return 2
    total = 0
    for p in proyectos:
        if p not in CONFIG["proyectos"]:
            continue
        r = indexar_proyecto(agente, p)
        print(json.dumps(r, ensure_ascii=False))
        total += bool(r.get("error"))
    return 1 if total else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1] if len(sys.argv) > 1 else "orquestador", sys.argv[2:] or list(CONFIG["proyectos"])))
