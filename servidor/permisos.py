"""Identidad y permisos. La identidad sale SIEMPRE de la credencial; ningún dato del pedido la cambia.

Niveles por proyecto: ninguno < lectura < escritura < curador. La memoria global vive en el proyecto de la fábrica
(PROYECTO_GLOBAL) y solo la escriben sus curadores. La memoria de alcance «agente» es privada del propietario
salvo que la comparta explícitamente (compartida_con). Una credencial «orquestador» (la cola, el panel) puede
registrar sesiones/checkpoints a nombre de los agentes que lanza, pero no leer su memoria privada.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field

from sqlalchemy import Connection, text

from servidor.config import PROYECTO_GLOBAL

NIVELES = {"ninguno": 0, "lectura": 1, "escritura": 2, "curador": 3}


class Denegado(Exception):
    """403: la credencial no alcanza. Nunca se confunde con «no existe»."""


class NoAutenticado(Exception):
    """401."""


class Conflicto(Exception):
    """409: versión esperada distinta, clave idempotente con otro contenido, operación ya resuelta."""


class Invalido(Exception):
    """422: datos que violan el contrato."""


class NoEncontrado(Exception):
    """404 (solo cuando el principal sí tendría permiso de verlo)."""


@dataclass
class Principal:
    agente: str
    tipo: str                      # agente | orquestador | admin
    credencial_id: int
    niveles: dict[str, int] = field(default_factory=dict)

    @property
    def nombre(self) -> str:
        return f"{self.tipo}:{self.agente}"

    def nivel(self, proyecto: str) -> int:
        if self.tipo == "admin":
            return NIVELES["curador"]
        return self.niveles.get(proyecto, 0)

    def exigir(self, proyecto: str, minimo: str) -> None:
        if self.nivel(proyecto) < NIVELES[minimo]:
            raise Denegado(f"{self.agente} no tiene nivel «{minimo}» en el proyecto «{proyecto}»")

    def actuar_como(self, agente: str | None) -> str:
        """Agente en cuyo nombre se escribe. Solo orquestador/admin pueden indicar otro."""
        if not agente or agente == self.agente:
            return self.agente
        if self.tipo in ("orquestador", "admin"):
            return agente
        raise Denegado(f"{self.agente} no puede escribir a nombre de {agente}")


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def autenticar(conexion: Connection, token: str | None) -> Principal:
    if not token or not token.startswith("dfm_") or len(token) > 200:
        raise NoAutenticado("Falta credencial válida (Authorization: Bearer dfm_...)")
    fila = conexion.execute(text(
        "SELECT c.id, c.agente_id, c.tipo, a.estado FROM credenciales c JOIN agentes a ON a.id = c.agente_id "
        "WHERE c.hash_token = :h AND c.revocada IS NULL"), {"h": hash_token(token)}).first()
    if not fila:
        raise NoAutenticado("Credencial desconocida o revocada")
    if fila.estado != "activo":
        raise Denegado(f"El agente {fila.agente_id} está {fila.estado}")
    niveles = {r.proyecto_id: NIVELES[r.nivel] for r in conexion.execute(text(
        "SELECT p.proyecto_id, p.nivel FROM permisos p JOIN proyectos pr ON pr.id = p.proyecto_id "
        "WHERE p.agente_id = :a AND pr.estado = 'activo'"), {"a": fila.agente_id})}
    return Principal(agente=fila.agente_id, tipo=fila.tipo, credencial_id=fila.id, niveles=niveles)


def puede_leer_memoria(p: Principal, proyecto: str, alcance: str, propietario: str | None,
                       compartida_con: list[str] | None) -> bool:
    if p.tipo == "admin":
        return True
    if alcance == "agente":
        return propietario == p.agente or p.agente in (compartida_con or [])
    return p.nivel(proyecto) >= NIVELES["lectura"]


def exigir_escritura_memoria(p: Principal, proyecto: str, alcance: str, propietario: str | None) -> None:
    if alcance == "global":
        if proyecto != PROYECTO_GLOBAL:
            raise Invalido(f"La memoria global pertenece al proyecto «{PROYECTO_GLOBAL}»")
        p.exigir(proyecto, "curador")
    elif alcance == "agente":
        if propietario != p.agente and p.tipo != "admin":
            raise Denegado("La memoria privada solo la escribe su propietario")
        p.exigir(proyecto, "lectura")
    else:
        p.exigir(proyecto, "escritura")
