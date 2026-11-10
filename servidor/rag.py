"""RAG sobre el corpus de DreamFactory: indexación incremental por sha256 y recuperación híbrida.

Léxico: full-text en español (tsvector ponderado encabezado/texto) con consulta en modo OR y ts_rank_cd.
Vectorial (opcional): si el cliente manda el embedding de la consulta y los fragmentos tienen embeddings del mismo
modelo, se calcula coseno y se fusiona con el ranking léxico por Reciprocal Rank Fusion (k=60).
Se devuelven extractos cortos con referencia exacta (ruta:líneas@sha), nunca documentos enteros.
"""
from __future__ import annotations

import math
from typing import Any

from sqlalchemy import Connection, text

import redaccion
from servidor.modelos import DocumentoIndexar, DocumentoRetirar, FragmentosEmbedding
from servidor.permisos import Invalido, Principal

TSQ = "CAST(replace(websearch_to_tsquery('spanish', :q)::text, ' & ', ' | ') AS tsquery)"
RRF_K = 60
POR_DOCUMENTO = 2


def indexar(con: Connection, p: Principal, d: DocumentoIndexar) -> dict:
    p.exigir(d.proyecto, "escritura")
    previo = con.execute(text("SELECT id, sha256 FROM documentos WHERE proyecto_id = :p AND ruta = :r AND vigente "
                              "FOR UPDATE"), {"p": d.proyecto, "r": d.ruta}).first()
    if previo and previo.sha256 == d.sha256:
        return {"documento": previo.id, "sin_cambios": True}
    ya = con.execute(text("SELECT id FROM documentos WHERE proyecto_id = :p AND ruta = :r AND sha256 = :s"),
                     {"p": d.proyecto, "r": d.ruta, "s": d.sha256}).first()
    if previo:
        con.execute(text("UPDATE documentos SET vigente = false, retirado = now() WHERE id = :i"), {"i": previo.id})
    if ya:   # vuelve una versión que ya estaba indexada: se reactiva tal cual
        con.execute(text("UPDATE documentos SET vigente = true, retirado = NULL WHERE id = :i"), {"i": ya.id})
        return {"documento": ya.id, "reactivado": True}
    did = con.execute(text(
        "INSERT INTO documentos (proyecto_id, ruta, sha256, commit, bytes, fragmentos, indexado_por) "
        "VALUES (:p, :r, :s, :c, :b, :n, :a) RETURNING id"),
        {"p": d.proyecto, "r": d.ruta, "s": d.sha256, "c": d.commit, "b": d.bytes, "n": len(d.fragmentos),
         "a": p.agente}).scalar_one()
    sospechosos = 0
    for i, f in enumerate(d.fragmentos, start=1):
        cuerpo, marcas = redaccion.limpiar(f.texto, 2400)
        encabezado, _ = redaccion.limpiar(f.encabezado, 300)
        sospechosos += marcas["sospechoso"]
        con.execute(text(
            "INSERT INTO fragmentos (documento_id, n, encabezado, linea_ini, linea_fin, texto, tokens, sospechoso) "
            "VALUES (:d, :n, :e, :li, :lf, :t, :k, :s)"),
            {"d": did, "n": i, "e": encabezado, "li": f.linea_ini, "lf": f.linea_fin, "t": cuerpo,
             "k": redaccion.estimar_tokens(cuerpo), "s": marcas["sospechoso"]})
    return {"documento": did, "fragmentos": len(d.fragmentos), "sospechosos": sospechosos,
            "reemplaza": previo.id if previo else None}


def retirar(con: Connection, p: Principal, d: DocumentoRetirar) -> dict:
    p.exigir(d.proyecto, "escritura")
    ids = con.execute(text("UPDATE documentos SET vigente = false, retirado = now() WHERE proyecto_id = :p "
                           "AND ruta = :r AND vigente RETURNING id"), {"p": d.proyecto, "r": d.ruta}).scalars().all()
    return {"retirados": ids}


def completar_embeddings(con: Connection, p: Principal, d: FragmentosEmbedding) -> dict:
    p.exigir(d.proyecto, "escritura")
    dimension = None
    hechos = 0
    for item in d.items:
        if dimension is None:
            dimension = len(item.vector)
        if len(item.vector) != dimension or not all(math.isfinite(x) for x in item.vector):
            raise Invalido("Vectores de dimensión distinta o con valores no finitos")
        hechos += con.execute(text(
            "UPDATE fragmentos f SET embedding = :v, modelo_embedding = :m FROM documentos d "
            "WHERE f.documento_id = d.id AND d.proyecto_id = :p AND f.documento_id = :di AND f.n = :n"),
            {"v": item.vector, "m": d.modelo, "p": d.proyecto, "di": item.documento_id, "n": item.n}).rowcount
    return {"actualizados": hechos, "modelo": d.modelo, "dimension": dimension}


def estado(con: Connection, p: Principal, proyecto: str) -> dict:
    p.exigir(proyecto, "lectura")
    filas = con.execute(text("SELECT ruta, sha256, id, fragmentos FROM documentos WHERE proyecto_id = :p AND vigente"),
                        {"p": proyecto}).all()
    con_emb = con.execute(text(
        "SELECT f.modelo_embedding, count(*) FROM fragmentos f JOIN documentos d ON d.id = f.documento_id "
        "WHERE d.proyecto_id = :p AND d.vigente GROUP BY 1"), {"p": proyecto}).all()
    return {"documentos": {f.ruta: f.sha256 for f in filas},
            "fragmentos": sum(f.fragmentos for f in filas),
            "embeddings": {(m or "ninguno"): n for m, n in con_emb}}


def _coseno(a: list[float], b: list[float]) -> float:
    num = sum(x * y for x, y in zip(a, b))
    den = math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(y * y for y in b))
    return num / den if den else 0.0


def buscar(con: Connection, p: Principal, proyectos: list[str], consulta: str, k: int = 5,
           vector: list[float] | None = None, modelo: str | None = None) -> list[dict[str, Any]]:
    legibles = [py for py in proyectos if p.nivel(py) >= 1]
    if not legibles or not (consulta.strip() or vector):
        return []
    lexico = con.execute(text(
        f"SELECT d.id AS doc, d.ruta, d.sha256, d.commit, d.proyecto_id, f.n, f.encabezado, f.linea_ini, f.linea_fin, "
        f"f.tokens, f.sospechoso, ts_rank_cd(f.tsv, c.q, 32) AS rango, "
        f"ts_headline('spanish', f.texto, c.q, 'MaxWords=40,MinWords=12,MaxFragments=2,FragmentDelimiter= … ,"
        f"StartSel=⟦,StopSel=⟧') AS extracto "
        f"FROM fragmentos f JOIN documentos d ON d.id = f.documento_id, (SELECT {TSQ} AS q) AS c "
        f"WHERE d.vigente AND d.proyecto_id = ANY(:pr) AND f.tsv @@ c.q ORDER BY rango DESC LIMIT :lim"),
        {"q": consulta, "pr": legibles, "lim": k * 6}).all() if consulta.strip() else []
    puntajes: dict[tuple[int, int], float] = {}
    filas: dict[tuple[int, int], dict] = {}
    for pos, f in enumerate(lexico):
        clave = (f.doc, f.n)
        puntajes[clave] = puntajes.get(clave, 0) + 1 / (RRF_K + pos + 1)
        filas[clave] = dict(f._mapping)
    if vector and modelo:
        cand = con.execute(text(
            "SELECT d.id AS doc, d.ruta, d.sha256, d.commit, d.proyecto_id, f.n, f.encabezado, f.linea_ini, "
            "f.linea_fin, f.tokens, f.sospechoso, f.embedding, left(f.texto, 300) AS extracto FROM fragmentos f "
            "JOIN documentos d ON d.id = f.documento_id WHERE d.vigente AND d.proyecto_id = ANY(:pr) "
            "AND f.modelo_embedding = :m LIMIT 5000"), {"pr": legibles, "m": modelo}).all()
        ordenados = sorted(cand, key=lambda f: _coseno(vector, f.embedding), reverse=True)[: k * 6]
        for pos, f in enumerate(ordenados):
            clave = (f.doc, f.n)
            puntajes[clave] = puntajes.get(clave, 0) + 1 / (RRF_K + pos + 1)
            filas.setdefault(clave, {c: v for c, v in f._mapping.items() if c != "embedding"})
    resultado, por_doc = [], {}
    for clave in sorted(puntajes, key=puntajes.get, reverse=True):
        f = filas[clave]
        if por_doc.get(f["doc"], 0) >= POR_DOCUMENTO:
            continue
        por_doc[f["doc"]] = por_doc.get(f["doc"], 0) + 1
        extracto = " ".join((f.get("extracto") or "").replace("⟦", "").replace("⟧", "").split())
        resultado.append({"ref": f"{f['ruta']}:{f['linea_ini']}-{f['linea_fin']}@{f['sha256'][:12]}",
                          "proyecto": f["proyecto_id"], "ruta": f["ruta"], "encabezado": f["encabezado"],
                          "extracto": extracto, "puntaje": round(puntajes[clave], 5), "sospechoso": f["sospechoso"],
                          "tokens_fragmento": f["tokens"]})
        if len(resultado) >= k:
            break
    return resultado
