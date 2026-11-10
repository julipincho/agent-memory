"""Banco de evaluación del RAG: consultas reales en lenguaje natural con el documento que debería aparecer.

Mide recall@1/@3/@5 y MRR del modo léxico; con --embeddings ollama:<modelo> también el híbrido (si ese modelo está
instalado y la GPU libre; este banco NO arranca Ollama: lo usa solo si ya responde). Guarda el resultado en
evaluacion/resultados/<fecha>.json para comparar estrategias con números, no con impresiones.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

AQUI = Path(__file__).resolve().parent
sys.path.insert(0, str(AQUI.parent))
import cliente  # noqa: E402

# Demo bench over examples/demo_project. The numbers in the README come from a private bench of 20 real questions
# over 1,910 chunks of my own projects; bring your own with --banco bench.json:
#   {"proyectos": ["my_project"], "banco": [["question", ["expected_doc.md", ...]], ...]}
BANCO = [
    ("why do we use full text search before embeddings", ["decisions.md"]),
    ("can an agent rewrite the history", ["decisions.md"]),
    ("who approves a finished task", ["README.md"]),
    ("only one heavy job on the graphics card", ["decisions.md"]),
]
PROYECTOS = ["demo"]


def evaluar(k: int = 5, agente: str = "claude") -> dict:
    filas, rr, aciertos = [], [], {1: 0, 3: 0, 5: 0}
    t0 = time.time()
    for consulta, esperados in BANCO:
        res = cliente.http("POST", "/v1/rag/buscar", {"proyectos": PROYECTOS, "consulta": consulta, "k": k},
                           agente=agente)["resultados"]
        rutas = [r["ruta"] for r in res]
        pos = next((i + 1 for i, ruta in enumerate(rutas) if any(ruta.endswith(e) for e in esperados)), None)
        rr.append(1 / pos if pos else 0)
        for n in aciertos:
            aciertos[n] += bool(pos and pos <= n)
        filas.append({"consulta": consulta, "posicion": pos, "top3": rutas[:3],
                      "tokens_extractos": sum(len(r["extracto"]) for r in res) // 3.5})
    n = len(BANCO)
    return {"modo": "lexico", "consultas": n, "recall@1": round(aciertos[1] / n, 3), "recall@3": round(aciertos[3] / n, 3),
            "recall@5": round(aciertos[5] / n, 3), "mrr": round(sum(rr) / n, 3),
            "ms_por_consulta": round((time.time() - t0) * 1000 / n, 1),
            "tokens_extractos_promedio": round(sum(f["tokens_extractos"] for f in filas) / n), "detalle": filas}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--banco", help="JSON file with {proyectos, banco} (see the top of this file)")
    ap.add_argument("--embeddings", help="ollama:<modelo> (solo si Ollama ya responde y no hay gpu.lock)")
    a = ap.parse_args()
    if a.banco:
        global BANCO, PROYECTOS
        datos = json.loads(Path(a.banco).read_text(encoding="utf-8"))
        BANCO, PROYECTOS = [tuple(x) for x in datos["banco"]], datos["proyectos"]
    if not cliente.disponible(45):
        print("la memoria no responde", file=sys.stderr)
        return 2
    resultado = {"fecha": time.strftime("%Y-%m-%d %H:%M"), "lexico": evaluar()}
    if a.embeddings:
        resultado["hibrido"] = {"estado": "no evaluado: falta un modelo de embeddings instalado (ver README §RAG)"}
    destino = AQUI / "resultados" / f"rag_{time.strftime('%Y%m%d_%H%M')}.json"
    destino.parent.mkdir(parents=True, exist_ok=True)
    destino.write_text(json.dumps(resultado, ensure_ascii=False, indent=1), encoding="utf-8")
    lx = resultado["lexico"]
    print(f"léxico: recall@1={lx['recall@1']} recall@3={lx['recall@3']} recall@5={lx['recall@5']} MRR={lx['mrr']} "
          f"({lx['ms_por_consulta']} ms/consulta, ~{lx['tokens_extractos_promedio']} tokens de extractos) → {destino.name}")
    for f in lx["detalle"]:
        if not f["posicion"] or f["posicion"] > 3:
            print(f"  ✗ {f['consulta']} → {f['posicion']} {f['top3']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
