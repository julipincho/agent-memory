"""RAG: corpus de documentos versionados por contenido (sha256) y fragmentos con búsqueda full-text en español.

El embedding es opcional (real[] + modelo) y se completa después; los fragmentos son inmutables salvo esas dos
columnas. Un documento cambiado crea otra versión; la anterior queda con vigente = false (no se borra).

Revision ID: 0002_rag_documentos
Revises: 0001_memoria_inicial
"""
from alembic import op

revision = "0002_rag_documentos"
down_revision = "0001_memoria_inicial"
branch_labels = None
depends_on = None

DDL = r"""
CREATE TABLE memoria.documentos (
    id            bigserial PRIMARY KEY,
    proyecto_id   text NOT NULL REFERENCES memoria.proyectos(id) ON DELETE RESTRICT,
    ruta          text NOT NULL CHECK (length(ruta) BETWEEN 1 AND 400),
    sha256        char(64) NOT NULL,
    commit        text,
    bytes         integer NOT NULL,
    fragmentos    integer NOT NULL,
    vigente       boolean NOT NULL DEFAULT true,
    indexado      timestamptz NOT NULL DEFAULT now(),
    indexado_por  text NOT NULL REFERENCES memoria.agentes(id) ON DELETE RESTRICT,
    retirado      timestamptz,
    UNIQUE (proyecto_id, ruta, sha256)
);
CREATE UNIQUE INDEX documentos_vigente_uidx ON memoria.documentos (proyecto_id, ruta) WHERE vigente;

CREATE TABLE memoria.fragmentos (
    documento_id      bigint NOT NULL REFERENCES memoria.documentos(id) ON DELETE RESTRICT,
    n                 integer NOT NULL CHECK (n >= 1),
    encabezado        text NOT NULL DEFAULT '',
    linea_ini         integer NOT NULL,
    linea_fin         integer NOT NULL,
    texto             text NOT NULL CHECK (length(texto) <= 2400),
    tokens            integer NOT NULL,
    sospechoso        boolean NOT NULL DEFAULT false,
    embedding         real[],
    modelo_embedding  text,
    tsv               tsvector GENERATED ALWAYS AS (
                          setweight(to_tsvector('spanish'::regconfig, encabezado), 'A') ||
                          setweight(to_tsvector('spanish'::regconfig, texto), 'B')) STORED,
    PRIMARY KEY (documento_id, n),
    CHECK (linea_fin >= linea_ini)
);
CREATE INDEX fragmentos_tsv_idx ON memoria.fragmentos USING gin (tsv);

CREATE FUNCTION memoria.fragmento_solo_embedding() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'DELETE' OR (NEW.documento_id, NEW.n, NEW.encabezado, NEW.linea_ini, NEW.linea_fin, NEW.texto,
                            NEW.tokens, NEW.sospechoso)
        IS DISTINCT FROM (OLD.documento_id, OLD.n, OLD.encabezado, OLD.linea_ini, OLD.linea_fin, OLD.texto,
                          OLD.tokens, OLD.sospechoso) THEN
        RAISE EXCEPTION 'memoria.fragmentos: solo se completan embedding/modelo_embedding' USING ERRCODE = 'restrict_violation';
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER fragmentos_inmutable BEFORE UPDATE OR DELETE ON memoria.fragmentos
    FOR EACH ROW EXECUTE FUNCTION memoria.fragmento_solo_embedding();
CREATE TRIGGER documentos_sin_borrar BEFORE DELETE ON memoria.documentos
    FOR EACH ROW EXECUTE FUNCTION memoria.prohibir_borrado();
CREATE TRIGGER documentos_sin_truncar BEFORE TRUNCATE ON memoria.documentos
    FOR EACH STATEMENT EXECUTE FUNCTION memoria.prohibir_borrado();
CREATE TRIGGER fragmentos_sin_truncar BEFORE TRUNCATE ON memoria.fragmentos
    FOR EACH STATEMENT EXECUTE FUNCTION memoria.prohibir_borrado();

GRANT SELECT, INSERT ON memoria.documentos, memoria.fragmentos TO df_memoria_app;
GRANT UPDATE (vigente, retirado) ON memoria.documentos TO df_memoria_app;
GRANT UPDATE (embedding, modelo_embedding) ON memoria.fragmentos TO df_memoria_app;
GRANT USAGE ON ALL SEQUENCES IN SCHEMA memoria TO df_memoria_app;
"""


def upgrade() -> None:
    op.execute(DDL)


def downgrade() -> None:
    raise RuntimeError("Sin downgrade: restaurar desde backup.")
