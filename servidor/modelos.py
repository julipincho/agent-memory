"""Contrato de comandos dfmem-v1 (pydantic). Cada comando es una escritura atómica e idempotente."""
from __future__ import annotations

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

Clave = Field(pattern=r"^[a-z0-9][a-z0-9_.\-/]{0,119}$")
Proyecto = Field(pattern=r"^[a-z][a-z0-9_\-]{1,40}$")
Agente = Field(default=None, pattern=r"^[a-z][a-z0-9_\-]{1,40}$")
TipoMemoria = Literal["decision", "regla", "convencion", "arquitectura", "objetivo", "herramienta", "restriccion",
                      "leccion", "error_conocido", "hecho", "preferencia", "referencia", "resumen", "proximo_paso", "rol"]


class Base(BaseModel):
    model_config = ConfigDict(extra="forbid", str_max_length=4000)


class Ref(Base):
    """Referencia compacta a un archivo/artefacto: nunca el contenido, solo cómo encontrarlo y verificarlo."""
    ruta: str = Field(max_length=400)
    sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    commit: str | None = Field(default=None, pattern=r"^[0-9a-f]{7,40}$")
    nota: str = Field(default="", max_length=200)


class SesionAbrir(Base):
    id: UUID
    proyecto: str = Proyecto
    tarea: str | None = Field(default=None, max_length=120)
    canal: str = Field(max_length=40)
    sesion_runtime: str = Field(default="", max_length=120)
    agente: str | None = Agente


class SesionLatido(Base):
    id: UUID
    estado_mecanico: dict[str, Any] = Field(default_factory=dict)


class SesionCerrar(Base):
    id: UUID
    estado: Literal["cerrada", "interrumpida"] = "cerrada"
    resumen_final: str = Field(default="", max_length=2000)


class Checkpoint(Base):
    sesion_id: UUID
    proyecto: str = Proyecto
    tarea: str | None = Field(default=None, max_length=120)
    haciendo: str = Field(default="", max_length=600)
    completado: list[str] = Field(default_factory=list, max_length=40)
    pendiente: list[str] = Field(default_factory=list, max_length=40)
    archivos: list[str | Ref] = Field(default_factory=list, max_length=80)
    pruebas: list[dict[str, Any]] = Field(default_factory=list, max_length=40)
    efectos_confirmados: list[str] = Field(default_factory=list, max_length=40)
    reanudar_desde: str = Field(default="", max_length=800)
    git: dict[str, Any] = Field(default_factory=dict)
    origen: Literal["modelo", "automatico", "orquestador", "humano"] = "modelo"
    agente: str | None = Agente


class MemoriaRecordar(Base):
    proyecto: str = Proyecto
    alcance: Literal["global", "proyecto", "tarea", "agente"]
    clave: str = Clave
    tipo: TipoMemoria
    prioridad: int = Field(default=3, ge=1, le=5)
    titulo: str = Field(min_length=1, max_length=200)
    contenido: str = Field(default="", max_length=4000)
    refs: list[Ref] = Field(default_factory=list, max_length=20)
    etiquetas: list[str] = Field(default_factory=list, max_length=12)
    tarea: str | None = Field(default=None, max_length=120)
    revision_esperada: int | None = Field(default=None, ge=0)
    reemplaza: list[int] = Field(default_factory=list, max_length=10)
    compartida_con: list[str] = Field(default_factory=list, max_length=10)
    vence: datetime | None = None
    origen: Literal["modelo", "humano", "orquestador", "importado", "automatico"] = "modelo"
    fuente: str = Field(default="", max_length=300)
    motivo: str = Field(default="", max_length=300)


class MemoriaEstado(Base):
    id: int
    estado: Literal["vigente", "obsoleta", "retirada"]
    motivo: str = Field(min_length=3, max_length=300)


class TareaGuardar(Base):
    proyecto: str = Proyecto
    clave: str = Field(min_length=1, max_length=120)
    titulo: str | None = Field(default=None, max_length=200)
    objetivo: str | None = Field(default=None, max_length=2000)
    criterios: list[str] | None = Field(default=None, max_length=30)
    estado: Literal["pendiente", "en_curso", "bloqueada", "revision", "rehacer", "hecha", "cancelada"] | None = None
    responsable: str | None = Agente
    dependencias: list[str] | None = Field(default=None, max_length=30)
    proxima_accion: str | None = Field(default=None, max_length=600)
    bloqueos: str | None = Field(default=None, max_length=1000)
    artefactos: list[Ref] | None = Field(default=None, max_length=40)
    version_esperada: int | None = Field(default=None, ge=1)


class OperacionIniciar(Base):
    proyecto: str = Proyecto
    clave: str = Field(min_length=1, max_length=200)
    tipo: str = Field(max_length=60)
    descripcion: str = Field(max_length=600)
    destructiva: bool = False
    tarea: str | None = Field(default=None, max_length=120)
    sesion_id: UUID | None = None
    agente: str | None = Agente


class OperacionResolver(Base):
    proyecto: str = Proyecto
    clave: str = Field(min_length=1, max_length=200)
    estado: Literal["confirmada", "fallida", "incierta", "reconciliada"]
    resultado: dict[str, Any] = Field(default_factory=dict)
    version_esperada: int | None = Field(default=None, ge=1)


class Fragmento(Base):
    encabezado: str = Field(default="", max_length=300)
    linea_ini: int = Field(ge=1)
    linea_fin: int = Field(ge=1)
    texto: str = Field(max_length=2400)


class DocumentoIndexar(Base):
    proyecto: str = Proyecto
    ruta: str = Field(min_length=1, max_length=400)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    commit: str | None = Field(default=None, pattern=r"^[0-9a-f]{7,40}$")
    bytes: int = Field(ge=0)
    fragmentos: list[Fragmento] = Field(max_length=800)


class DocumentoRetirar(Base):
    proyecto: str = Proyecto
    ruta: str = Field(min_length=1, max_length=400)


class VectorFragmento(Base):
    documento_id: int
    n: int = Field(ge=1)
    vector: list[float] = Field(min_length=8, max_length=4096)


class FragmentosEmbedding(Base):
    proyecto: str = Proyecto
    modelo: str = Field(min_length=1, max_length=120)
    items: list[VectorFragmento] = Field(max_length=200)


class Comando(Base):
    tipo: Literal["sesion_abrir", "sesion_latido", "sesion_cerrar", "checkpoint", "memoria_recordar",
                  "memoria_estado", "tarea_guardar", "operacion_iniciar", "operacion_resolver",
                  "documento_indexar", "documento_retirar", "fragmentos_embedding"]
    clave_idem: str = Field(min_length=8, max_length=200)
    datos: dict[str, Any]


class Lote(Base):
    comandos: list[Comando] = Field(max_length=200)


class PedidoRag(Base):
    proyectos: list[str] = Field(min_length=1, max_length=5)
    consulta: str = Field(default="", max_length=300)
    k: int = Field(default=5, ge=1, le=20)
    vector: list[float] | None = Field(default=None, max_length=4096)
    modelo: str | None = Field(default=None, max_length=120)


class PedidoContexto(Base):
    proyecto: str = Proyecto
    tarea: str | None = Field(default=None, max_length=120)
    operacion: Literal["inicio", "reanudar", "compactacion", "cambio_tarea", "despacho", "busqueda"] = "inicio"
    sesion_id: UUID | None = None
    consulta: str = Field(default="", max_length=300)
    presupuesto: int | None = Field(default=None, ge=50, le=8000)
    agente: str | None = Agente        # solo orquestador/admin: paquete para el agente que va a lanzar


MODELOS = {"sesion_abrir": SesionAbrir, "sesion_latido": SesionLatido, "sesion_cerrar": SesionCerrar,
           "checkpoint": Checkpoint, "memoria_recordar": MemoriaRecordar, "memoria_estado": MemoriaEstado,
           "tarea_guardar": TareaGuardar, "operacion_iniciar": OperacionIniciar,
           "operacion_resolver": OperacionResolver, "documento_indexar": DocumentoIndexar,
           "documento_retirar": DocumentoRetirar, "fragmentos_embedding": FragmentosEmbedding}
