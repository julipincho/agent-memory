"""Troceado de documentos para el RAG (solo biblioteca estándar; lo usan el cliente y las pruebas del servidor).

Markdown: se corta por encabezados y, dentro de cada sección, por párrafos hasta ~TAMANO caracteres. Cada fragmento
conserva la ruta de encabezados («A › B») y sus líneas exactas, para citar `ruta:ini-fin@sha` sin copiar el archivo.
"""
from __future__ import annotations

import hashlib
import re

TAMANO = 1400
ENCABEZADO = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$")


def sha256_bytes(datos: bytes) -> str:
    return hashlib.sha256(datos).hexdigest()


def fragmentar(texto: str, tamano: int = TAMANO) -> list[dict]:
    lineas = texto.replace("\r\n", "\n").split("\n")
    pila: list[tuple[int, str]] = []
    fragmentos: list[dict] = []
    actual: list[tuple[int, str]] = []
    en_codigo = False

    def ruta() -> str:
        return " › ".join(t for _, t in pila)[:300]

    def volcar() -> None:
        nonlocal actual
        while actual and not actual[-1][1].strip():
            actual.pop()
        while actual and not actual[0][1].strip():
            actual.pop(0)
        if actual:
            cuerpo = "\n".join(l for _, l in actual)
            fragmentos.append({"encabezado": ruta(), "linea_ini": actual[0][0], "linea_fin": actual[-1][0],
                               "texto": cuerpo[:2400]})
        actual = []

    for numero, linea in enumerate(lineas, start=1):
        if linea.strip().startswith("```"):
            en_codigo = not en_codigo
        m = None if en_codigo else ENCABEZADO.match(linea)
        if m:
            volcar()
            nivel = len(m.group(1))
            while pila and pila[-1][0] >= nivel:
                pila.pop()
            pila.append((nivel, m.group(2).strip()))
            continue
        tam = sum(len(l) + 1 for _, l in actual)
        if actual and tam + len(linea) > tamano and (not linea.strip() or tam > tamano * 1.4):
            volcar()
        actual.append((numero, linea))
    volcar()
    return [f for f in fragmentos if len(f["texto"].strip()) >= 20]
