#!/usr/bin/env bash
# First-time setup (idempotent): secrets -> build -> migrate -> seed -> one token per agent in ./.dfmem/credenciales.
# Never overwrites existing secrets or tokens. Requires Docker with the compose plugin and python3.
set -Eeuo pipefail
cd "$(dirname "$0")/.."
mkdir -p secrets .dfmem/credenciales
for f in pg_super app migrador; do
  [ -s "secrets/$f.txt" ] || python3 -c 'import secrets; print(secrets.token_urlsafe(24))' > "secrets/$f.txt"
done
# Local development: the API container runs as uid 10001 and must be able to read its secrets.
# In production use chown 10001 + chmod 400 instead.
chmod 644 secrets/*.txt

docker compose up -d --build --wait memoria
docker compose exec -T memoria python -m servidor.admin semilla /app/config/semilla.json

for spec in claude:agente codex:agente opencode:agente tortu:agente orquestador:orquestador; do
  agente="${spec%%:*}"
  tipo="${spec##*:}"
  destino=".dfmem/credenciales/$agente.token"
  if [ -s "$destino" ]; then continue; fi
  docker compose exec -T memoria sh -c \
    "python -m servidor.admin credencial $agente $tipo /tmp/t/$agente.token >/dev/null && cat /tmp/t/$agente.token && rm /tmp/t/$agente.token" \
    > "$destino"
  chmod 600 "$destino"
  echo "token created for $agente -> $destino"
done
echo
echo "Ready. Run:  export DFMEM_DIR=\"$PWD/.dfmem\"  and then  python3 dfmem.py estado"
