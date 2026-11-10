"""Redacción de secretos, neutralización de contenido inyectado y estimación de tokens.

Solo biblioteca estándar: lo usan el servidor (en Docker) y el cliente (Windows, hooks de Claude, cola).
La memoria recuperada es DATO, nunca instrucciones: antes de guardarla se quitan secretos y se desarman las
marcas que un modelo podría confundir con mensajes del sistema.
"""
from __future__ import annotations

import math
import re

LIMITE_TEXTO = 4000          # caracteres máximos de un campo de texto libre
LIMITE_TITULO = 200

_SECRETOS: list[tuple[str, re.Pattern[str]]] = [
    ("clave_privada", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?(-----END [A-Z ]*PRIVATE KEY-----|$)", re.S)),
    ("anthropic", re.compile(r"\bsk-ant-[A-Za-z0-9_\-]{10,}")),
    ("openai", re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9_\-]{20,}")),
    ("github", re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{20,}|\bgithub_pat_[A-Za-z0-9_]{20,}")),
    ("aws", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("google", re.compile(r"\bAIza[0-9A-Za-z_\-]{30,}")),
    ("slack", re.compile(r"\bxox[abposr]-[A-Za-z0-9\-]{10,}")),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}")),
    ("url_con_clave", re.compile(r"(?i)\b([a-z][a-z0-9+.\-]*://[^\s:/@]+):[^\s@/]+@")),
    ("token_memoria", re.compile(r"\bdfm_[A-Za-z0-9_\-]{20,}")),
    ("asignacion", re.compile(
        r"(?i)\b(password|passwd|contrase(?:ñ|n)a|secret|secreto|api[_\-]?key|token|access[_\-]?key|private[_\-]?key)"
        r"(\s*[:=]\s*|\s+es\s+)(['\"]?)[^\s'\"]{6,}\3")),
]

# Marcas que imitan la estructura de la conversación o de las herramientas.
_MARCAS = re.compile(
    r"(?i)</?\s*(system|system-reminder|assistant|user|human|tool_use|tool_result|function_calls|invoke|"
    r"antml:[a-z_]+|instructions?|command-name|pasted_content)\b[^>]*>")
_ORDENES = re.compile(
    r"(?i)\b(ignore|ignorá|ignora|olvidá|olvida|disregard)\b[^.\n]{0,40}\b(previous|anteriores|above|prior|instrucciones|instructions)\b")


def redactar(texto: str) -> tuple[str, list[str]]:
    """Devuelve (texto sin secretos, tipos encontrados)."""
    encontrados: list[str] = []
    for nombre, patron in _SECRETOS:
        if nombre == "url_con_clave":
            texto, n = patron.subn(lambda m: f"{m.group(1)}:[REDACTADO]@", texto)
        elif nombre == "asignacion":
            texto, n = patron.subn(lambda m: f"{m.group(1)}{m.group(2)}[REDACTADO]", texto)
        else:
            texto, n = patron.subn(f"[REDACTADO:{nombre}]", texto)
        if n:
            encontrados.append(nombre)
    return texto, encontrados


def neutralizar(texto: str) -> tuple[str, bool]:
    """Desarma marcas tipo <system> y frases de secuestro. Devuelve (texto, sospechoso)."""
    sospechoso = bool(_MARCAS.search(texto) or _ORDENES.search(texto))
    texto = _MARCAS.sub(lambda m: m.group(0).replace("<", "‹").replace(">", "›"), texto)
    return texto, sospechoso


def limpiar(texto: str | None, limite: int = LIMITE_TEXTO) -> tuple[str, dict]:
    """Redacta, neutraliza y recorta. Devuelve (texto, marcas) con marcas = {redactado: [...], sospechoso: bool}."""
    if not texto:
        return "", {"redactado": [], "sospechoso": False}
    texto = texto.replace("\x00", "")
    texto, tipos = redactar(texto)
    texto, sospechoso = neutralizar(texto)
    if len(texto) > limite:
        texto = texto[: limite - 1] + "…"
    return texto, {"redactado": tipos, "sospechoso": sospechoso}


def limpiar_json(valor, limite: int = LIMITE_TEXTO, profundidad: int = 0):
    """Aplica limpiar() a todas las cadenas de una estructura JSON (listas/dicts), con límites de tamaño."""
    if profundidad > 6:
        return None
    if isinstance(valor, str):
        return limpiar(valor, limite)[0]
    if isinstance(valor, list):
        return [limpiar_json(v, limite, profundidad + 1) for v in valor[:200]]
    if isinstance(valor, dict):
        claves_prohibidas = {"thinking", "razonamiento", "reasoning", "chain_of_thought", "password", "token", "secret"}
        return {str(k)[:80]: limpiar_json(v, limite, profundidad + 1)
                for k, v in list(valor.items())[:100] if str(k).lower() not in claves_prohibidas}
    return valor


def estimar_tokens(texto: str) -> int:
    """Estimación conservadora (sin tokenizador local): ~3.5 caracteres por token en español/código."""
    return math.ceil(len(texto) / 3.5) if texto else 0
