#!/usr/bin/env bash
# Prueba aislada y efímera: PostgreSQL 18 (misma imagen fijada que producción) en una red interna propia,
# bootstrap de roles, migraciones (dos veces: deben ser idempotentes) y la suite de integración completa.
# No toca la base productiva. Todo lo creado se desmonta al salir (también si falla).
# Usage: bash scripts/test_isolated.sh   (needs Docker; uses its own throwaway network and database)
set -Eeuo pipefail
AQUI="$(cd "$(dirname "$0")/.." && pwd)"
ID="dfmem-prueba-$$"
RED="$ID-red"
PG="$ID-pg"
IMAGEN_PG="postgres:18@sha256:74935e72241653ca55e0414067e6d8763aceb8a810eb51b452253ec3dcfc4336"
TMP="$(mktemp -d /tmp/dfmem-prueba.XXXXXX)"
chmod 755 "$TMP"

limpiar() {
  docker rm -f "$PG" >/dev/null 2>&1 || true
  docker network rm "$RED" >/dev/null 2>&1 || true
  rm -rf "$TMP"
}
trap limpiar EXIT

clave() { python3 -c 'import secrets; print(secrets.token_urlsafe(24))'; }
clave > "$TMP/super.txt"; clave > "$TMP/app.txt"; clave > "$TMP/migrador.txt"
chown 10001:10001 "$TMP/app.txt" "$TMP/migrador.txt"; chmod 400 "$TMP/app.txt" "$TMP/migrador.txt"
chmod 444 "$TMP/super.txt"

echo "[prueba] construyendo imagen"
docker compose -f "$AQUI/compose.yaml" build -q migrate

docker network create --internal "$RED" >/dev/null
docker run -d --name "$PG" --network "$RED" --network-alias postgres --tmpfs /var/lib/postgresql \
  -e POSTGRES_USER=agf -e POSTGRES_DB=agent_memory -e POSTGRES_PASSWORD_FILE=/run/s/super.txt \
  -v "$TMP/super.txt:/run/s/super.txt:ro" "$IMAGEN_PG" >/dev/null
for i in $(seq 1 60); do
  docker exec "$PG" pg_isready -U agf -d agent_memory >/dev/null 2>&1 && break
  sleep 1
done
sleep 2
docker exec "$PG" pg_isready -U agf -d agent_memory

echo "[prueba] bootstrap de roles y esquema"
{ printf "\\set pw_app '%s'\n\\set pw_mig '%s'\n\\set db 'agent_memory'\n" "$(cat "$TMP/app.txt")" "$(cat "$TMP/migrador.txt")"
  cat "$AQUI/infra/bootstrap.sql"; } | docker exec -i "$PG" psql -q -U agf -d agent_memory
# segunda vez: idempotente
{ printf "\\set pw_app '%s'\n\\set pw_mig '%s'\n\\set db 'agent_memory'\n" "$(cat "$TMP/app.txt")" "$(cat "$TMP/migrador.txt")"
  cat "$AQUI/infra/bootstrap.sql"; } | docker exec -i "$PG" psql -q -U agf -d agent_memory

correr() {
  docker run --rm --network "$RED" \
    -e DFMEM_DB_HOST=postgres -e DFMEM_DB_NAME=agent_memory \
    -e DFMEM_DB_USER_APP=df_memoria_app -e DFMEM_DB_PASSWORD_FILE_APP=/run/s/app.txt \
    -e DFMEM_DB_USER_MIGRADOR=df_memoria_migrador -e DFMEM_DB_PASSWORD_FILE_MIGRADOR=/run/s/migrador.txt \
    -e DFMEM_PRUEBA=1 -v "$TMP/app.txt:/run/s/app.txt:ro" -v "$TMP/migrador.txt:/run/s/migrador.txt:ro" \
    --tmpfs /tmp:uid=10001 agent-memory:dev "$@"
}

echo "[prueba] migraciones (dos veces)"
correr alembic -c servidor/alembic.ini upgrade head
correr alembic -c servidor/alembic.ini upgrade head
correr alembic -c servidor/alembic.ini current

echo "[prueba] propietario de los objetos y privilegios del rol app"
docker exec -i "$PG" psql -At -U agf -d agent_memory <<'SQL'
SELECT 'tablas_no_del_dueno=' || count(*) FROM pg_tables WHERE schemaname = 'memoria' AND tableowner <> 'df_memoria_owner';
SELECT 'app_puede_borrar=' || bool_or(has_table_privilege('df_memoria_app', 'memoria.' || tablename, 'DELETE')) FROM pg_tables WHERE schemaname = 'memoria';
SQL

echo "[prueba] suite de integración"
set +e
correr python -m pytest -q -p no:cacheprovider pruebas "$@" 2>&1 | tail -40
estado=${PIPESTATUS[0]}
correr python -m servidor.admin verificar || estado=1
exit "$estado"
