"""Memoria persistente de DreamFactory: identidades, permisos, tareas, sesiones, checkpoints, operaciones,
memorias versionadas, auditoría, idempotencia, presupuestos y medición de contexto.

Revision ID: 0001_memoria_inicial
Create Date: 2026-11-03
"""
from alembic import op

revision = "0001_memoria_inicial"
down_revision = None
branch_labels = ("memoria",)
depends_on = None

ESQUEMA = "memoria"

DDL = r"""
CREATE TABLE memoria.agentes (
    id            text PRIMARY KEY CHECK (id ~ '^[a-z][a-z0-9_\-]{1,40}$'),
    nombre        text NOT NULL,
    rol           text NOT NULL,
    especialidad  text NOT NULL DEFAULT '',
    runtime       text NOT NULL DEFAULT '',
    estado        text NOT NULL DEFAULT 'activo' CHECK (estado IN ('activo','suspendido','retirado')),
    creado        timestamptz NOT NULL DEFAULT now(),
    actualizado   timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE memoria.credenciales (
    id          bigserial PRIMARY KEY,
    agente_id   text NOT NULL REFERENCES memoria.agentes(id) ON DELETE RESTRICT,
    tipo        text NOT NULL CHECK (tipo IN ('agente','orquestador','admin')),
    hash_token  char(64) NOT NULL UNIQUE,
    etiqueta    text NOT NULL DEFAULT '',
    creada      timestamptz NOT NULL DEFAULT now(),
    revocada    timestamptz
);

CREATE TABLE memoria.proyectos (
    id      text PRIMARY KEY CHECK (id ~ '^[a-z][a-z0-9_\-]{1,40}$'),
    nombre  text NOT NULL,
    raiz    text NOT NULL DEFAULT '',
    estado  text NOT NULL DEFAULT 'activo' CHECK (estado IN ('activo','archivado')),
    creado  timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE memoria.permisos (
    agente_id    text NOT NULL REFERENCES memoria.agentes(id) ON DELETE RESTRICT,
    proyecto_id  text NOT NULL REFERENCES memoria.proyectos(id) ON DELETE RESTRICT,
    nivel        text NOT NULL CHECK (nivel IN ('ninguno','lectura','escritura','curador')),
    otorgado_por text NOT NULL,
    actualizado  timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (agente_id, proyecto_id)
);

CREATE TABLE memoria.tareas (
    id              bigserial PRIMARY KEY,
    proyecto_id     text NOT NULL REFERENCES memoria.proyectos(id) ON DELETE RESTRICT,
    clave           text NOT NULL CHECK (length(clave) BETWEEN 1 AND 120),
    titulo          text NOT NULL,
    objetivo        text NOT NULL DEFAULT '',
    criterios       jsonb NOT NULL DEFAULT '[]' CHECK (jsonb_typeof(criterios) = 'array'),
    estado          text NOT NULL DEFAULT 'pendiente'
                    CHECK (estado IN ('pendiente','en_curso','bloqueada','revision','rehacer','hecha','cancelada')),
    responsable     text REFERENCES memoria.agentes(id) ON DELETE RESTRICT,
    dependencias    text[] NOT NULL DEFAULT '{}',
    proxima_accion  text NOT NULL DEFAULT '',
    bloqueos        text NOT NULL DEFAULT '',
    artefactos      jsonb NOT NULL DEFAULT '[]' CHECK (jsonb_typeof(artefactos) = 'array'),
    version         integer NOT NULL DEFAULT 1 CHECK (version >= 1),
    creado          timestamptz NOT NULL DEFAULT now(),
    actualizado     timestamptz NOT NULL DEFAULT now(),
    UNIQUE (proyecto_id, clave),
    UNIQUE (id, proyecto_id)
);

CREATE TABLE memoria.sesiones (
    id               uuid PRIMARY KEY,
    agente_id        text NOT NULL REFERENCES memoria.agentes(id) ON DELETE RESTRICT,
    proyecto_id      text NOT NULL REFERENCES memoria.proyectos(id) ON DELETE RESTRICT,
    tarea_id         bigint,
    canal            text NOT NULL,
    sesion_runtime   text NOT NULL DEFAULT '',
    estado           text NOT NULL DEFAULT 'activa' CHECK (estado IN ('activa','cerrada','interrumpida')),
    inicio           timestamptz NOT NULL DEFAULT now(),
    ultimo_latido    timestamptz NOT NULL DEFAULT now(),
    fin              timestamptz,
    estado_mecanico  jsonb NOT NULL DEFAULT '{}',
    resumen_final    text NOT NULL DEFAULT '',
    FOREIGN KEY (tarea_id, proyecto_id) REFERENCES memoria.tareas(id, proyecto_id) ON DELETE RESTRICT,
    UNIQUE (id, agente_id, proyecto_id)
);
CREATE INDEX sesiones_agente_idx ON memoria.sesiones (agente_id, inicio DESC);
CREATE INDEX sesiones_activas_idx ON memoria.sesiones (estado, ultimo_latido) WHERE estado = 'activa';

CREATE TABLE memoria.checkpoints (
    id                   bigserial PRIMARY KEY,
    sesion_id            uuid NOT NULL,
    agente_id            text NOT NULL,
    proyecto_id          text NOT NULL,
    tarea_id             bigint,
    secuencia            integer NOT NULL CHECK (secuencia >= 1),
    haciendo             text NOT NULL DEFAULT '',
    completado           jsonb NOT NULL DEFAULT '[]' CHECK (jsonb_typeof(completado) = 'array'),
    pendiente            jsonb NOT NULL DEFAULT '[]' CHECK (jsonb_typeof(pendiente) = 'array'),
    archivos             jsonb NOT NULL DEFAULT '[]' CHECK (jsonb_typeof(archivos) = 'array'),
    pruebas              jsonb NOT NULL DEFAULT '[]' CHECK (jsonb_typeof(pruebas) = 'array'),
    efectos_confirmados  jsonb NOT NULL DEFAULT '[]' CHECK (jsonb_typeof(efectos_confirmados) = 'array'),
    reanudar_desde       text NOT NULL DEFAULT '',
    git                  jsonb NOT NULL DEFAULT '{}',
    origen               text NOT NULL CHECK (origen IN ('modelo','automatico','orquestador','humano')),
    creado               timestamptz NOT NULL DEFAULT now(),
    FOREIGN KEY (sesion_id, agente_id, proyecto_id) REFERENCES memoria.sesiones(id, agente_id, proyecto_id)
        ON DELETE RESTRICT,
    FOREIGN KEY (tarea_id, proyecto_id) REFERENCES memoria.tareas(id, proyecto_id) ON DELETE RESTRICT,
    UNIQUE (sesion_id, secuencia)
);
CREATE INDEX checkpoints_tarea_idx ON memoria.checkpoints (proyecto_id, tarea_id, id DESC);
CREATE INDEX checkpoints_agente_idx ON memoria.checkpoints (agente_id, proyecto_id, id DESC);

CREATE TABLE memoria.operaciones (
    id           bigserial PRIMARY KEY,
    proyecto_id  text NOT NULL REFERENCES memoria.proyectos(id) ON DELETE RESTRICT,
    clave        text NOT NULL CHECK (length(clave) BETWEEN 1 AND 200),
    agente_id    text NOT NULL REFERENCES memoria.agentes(id) ON DELETE RESTRICT,
    sesion_id    uuid REFERENCES memoria.sesiones(id) ON DELETE RESTRICT,
    tarea_id     bigint,
    tipo         text NOT NULL,
    descripcion  text NOT NULL,
    destructiva  boolean NOT NULL DEFAULT false,
    estado       text NOT NULL DEFAULT 'intencion'
                 CHECK (estado IN ('intencion','confirmada','fallida','incierta','reconciliada')),
    resultado    jsonb NOT NULL DEFAULT '{}',
    version      integer NOT NULL DEFAULT 1,
    creado       timestamptz NOT NULL DEFAULT now(),
    actualizado  timestamptz NOT NULL DEFAULT now(),
    FOREIGN KEY (tarea_id, proyecto_id) REFERENCES memoria.tareas(id, proyecto_id) ON DELETE RESTRICT,
    UNIQUE (proyecto_id, clave)
);
CREATE INDEX operaciones_abiertas_idx ON memoria.operaciones (proyecto_id, estado)
    WHERE estado IN ('intencion','incierta');

CREATE TABLE memoria.memorias (
    id               bigserial PRIMARY KEY,
    proyecto_id      text NOT NULL REFERENCES memoria.proyectos(id) ON DELETE RESTRICT,
    alcance          text NOT NULL CHECK (alcance IN ('global','proyecto','tarea','agente')),
    propietario      text REFERENCES memoria.agentes(id) ON DELETE RESTRICT,
    tarea_id         bigint,
    clave            text NOT NULL CHECK (clave ~ '^[a-z0-9][a-z0-9_.\-/]{0,119}$'),
    tipo             text NOT NULL CHECK (tipo IN ('decision','regla','convencion','arquitectura','objetivo',
                         'herramienta','restriccion','leccion','error_conocido','hecho','preferencia',
                         'referencia','resumen','proximo_paso','rol')),
    prioridad        smallint NOT NULL DEFAULT 3 CHECK (prioridad BETWEEN 1 AND 5),
    estado           text NOT NULL DEFAULT 'vigente' CHECK (estado IN ('vigente','obsoleta','reemplazada','retirada')),
    revision_actual  integer NOT NULL DEFAULT 1,
    reemplazada_por  bigint REFERENCES memoria.memorias(id) ON DELETE RESTRICT,
    compartida_con   text[] NOT NULL DEFAULT '{}',
    vence            timestamptz,
    creado           timestamptz NOT NULL DEFAULT now(),
    actualizado      timestamptz NOT NULL DEFAULT now(),
    FOREIGN KEY (tarea_id, proyecto_id) REFERENCES memoria.tareas(id, proyecto_id) ON DELETE RESTRICT,
    CHECK ((alcance = 'agente') = (propietario IS NOT NULL)),
    CHECK ((alcance = 'tarea') = (tarea_id IS NOT NULL))
);
CREATE UNIQUE INDEX memorias_clave_uidx ON memoria.memorias
    (proyecto_id, alcance, coalesce(propietario, ''), coalesce(tarea_id, 0), clave);
CREATE INDEX memorias_vigentes_idx ON memoria.memorias (proyecto_id, estado, prioridad DESC);

CREATE TABLE memoria.memoria_revisiones (
    memoria_id  bigint NOT NULL REFERENCES memoria.memorias(id) ON DELETE RESTRICT,
    revision    integer NOT NULL CHECK (revision >= 1),
    titulo      text NOT NULL CHECK (length(titulo) BETWEEN 1 AND 200),
    contenido   text NOT NULL CHECK (length(contenido) <= 4000),
    refs        jsonb NOT NULL DEFAULT '[]' CHECK (jsonb_typeof(refs) = 'array'),
    etiquetas   text[] NOT NULL DEFAULT '{}',
    autor       text NOT NULL REFERENCES memoria.agentes(id) ON DELETE RESTRICT,
    origen      text NOT NULL CHECK (origen IN ('modelo','humano','orquestador','importado','automatico')),
    fuente      text NOT NULL DEFAULT '',
    motivo      text NOT NULL DEFAULT '',
    hash        char(64) NOT NULL,
    sospechoso  boolean NOT NULL DEFAULT false,
    creado      timestamptz NOT NULL DEFAULT now(),
    tsv         tsvector GENERATED ALWAYS AS (
                    setweight(to_tsvector('spanish'::regconfig, titulo), 'A') ||
                    setweight(to_tsvector('spanish'::regconfig, contenido), 'B')) STORED,
    PRIMARY KEY (memoria_id, revision)
);
CREATE INDEX memoria_revisiones_tsv_idx ON memoria.memoria_revisiones USING gin (tsv);
CREATE INDEX memoria_revisiones_hash_idx ON memoria.memoria_revisiones (hash);

CREATE TABLE memoria.idempotencia (
    principal       text NOT NULL,
    clave           text NOT NULL CHECK (length(clave) BETWEEN 8 AND 200),
    comando         text NOT NULL,
    hash_peticion   char(64) NOT NULL,
    respuesta       jsonb NOT NULL,
    creado          timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (principal, clave)
);

CREATE TABLE memoria.eventos (
    id               bigserial PRIMARY KEY,
    cuando           timestamptz NOT NULL DEFAULT now(),
    principal        text NOT NULL,
    tipo_credencial  text NOT NULL,
    agente_id        text NOT NULL,
    accion           text NOT NULL,
    entidad          text NOT NULL,
    entidad_id       text NOT NULL DEFAULT '',
    proyecto_id      text,
    clave_idem       text,
    detalle          jsonb NOT NULL DEFAULT '{}'
);
CREATE INDEX eventos_proyecto_idx ON memoria.eventos (proyecto_id, id DESC);
CREATE INDEX eventos_agente_idx ON memoria.eventos (agente_id, id DESC);

CREATE TABLE memoria.presupuestos (
    agente_id  text NOT NULL DEFAULT '*',
    operacion  text NOT NULL CHECK (operacion IN ('inicio','reanudar','compactacion','cambio_tarea','despacho','busqueda')),
    tokens     integer NOT NULL CHECK (tokens BETWEEN 50 AND 20000),
    PRIMARY KEY (agente_id, operacion)
);

CREATE TABLE memoria.consumo_contexto (
    id                        bigserial PRIMARY KEY,
    agente_id                 text NOT NULL REFERENCES memoria.agentes(id) ON DELETE RESTRICT,
    sesion_id                 uuid,
    proyecto_id               text NOT NULL,
    tarea_id                  bigint,
    operacion                 text NOT NULL,
    presupuesto               integer NOT NULL,
    tokens_entregados         integer NOT NULL,
    tokens_contexto_completo  integer NOT NULL,
    items_incluidos           integer NOT NULL,
    items_omitidos            integer NOT NULL,
    criticos                  integer NOT NULL,
    excedido_por_criticos     boolean NOT NULL DEFAULT false,
    creado                    timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX consumo_agente_idx ON memoria.consumo_contexto (agente_id, id DESC);

-- La historia no se reescribe: revisiones, checkpoints, eventos e idempotencia son de solo agregar.
CREATE FUNCTION memoria.prohibir_cambios() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'memoria.%: la historia es inmutable (% bloqueado)', TG_TABLE_NAME, TG_OP
        USING ERRCODE = 'restrict_violation';
END $$;
CREATE TRIGGER memoria_revisiones_inmutable BEFORE UPDATE OR DELETE ON memoria.memoria_revisiones
    FOR EACH ROW EXECUTE FUNCTION memoria.prohibir_cambios();
CREATE TRIGGER checkpoints_inmutable BEFORE UPDATE OR DELETE ON memoria.checkpoints
    FOR EACH ROW EXECUTE FUNCTION memoria.prohibir_cambios();
CREATE TRIGGER eventos_inmutable BEFORE UPDATE OR DELETE ON memoria.eventos
    FOR EACH ROW EXECUTE FUNCTION memoria.prohibir_cambios();
CREATE TRIGGER idempotencia_inmutable BEFORE UPDATE OR DELETE ON memoria.idempotencia
    FOR EACH ROW EXECUTE FUNCTION memoria.prohibir_cambios();

-- Ni borrado ni truncado en ninguna tabla: retirar = cambiar estado.
CREATE FUNCTION memoria.prohibir_borrado() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'memoria.%: no se borra; se cambia el estado', TG_TABLE_NAME USING ERRCODE = 'restrict_violation';
END $$;
"""

TABLAS = ["agentes", "credenciales", "proyectos", "permisos", "tareas", "sesiones", "checkpoints", "operaciones",
          "memorias", "memoria_revisiones", "idempotencia", "eventos", "presupuestos", "consumo_contexto"]
ACTUALIZABLES = ["agentes", "credenciales", "proyectos", "permisos", "tareas", "sesiones", "operaciones",
                 "memorias", "presupuestos"]


def upgrade() -> None:
    op.execute(DDL)
    for t in TABLAS:
        op.execute(f"CREATE TRIGGER {t}_sin_truncar BEFORE TRUNCATE ON memoria.{t} "
                   f"FOR EACH STATEMENT EXECUTE FUNCTION memoria.prohibir_borrado()")
    for t in ACTUALIZABLES:
        op.execute(f"CREATE TRIGGER {t}_sin_borrar BEFORE DELETE ON memoria.{t} "
                   f"FOR EACH ROW EXECUTE FUNCTION memoria.prohibir_borrado()")
    # Rol de la aplicación: lee y agrega; actualiza solo el estado mutable; nunca DDL ni DELETE.
    op.execute("GRANT USAGE ON SCHEMA memoria TO df_memoria_app")
    op.execute(f"GRANT SELECT, INSERT ON {', '.join('memoria.' + t for t in TABLAS)} TO df_memoria_app")
    op.execute(f"GRANT UPDATE ON {', '.join('memoria.' + t for t in ACTUALIZABLES)} TO df_memoria_app")
    op.execute("GRANT USAGE ON ALL SEQUENCES IN SCHEMA memoria TO df_memoria_app")
    op.execute("GRANT SELECT ON memoria.alembic_version_memoria TO df_memoria_app")


def downgrade() -> None:
    raise RuntimeError("La memoria no se baja con downgrade: restaurar desde backup (ver memoria/README.md).")
