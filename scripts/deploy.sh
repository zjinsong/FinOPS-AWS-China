#!/usr/bin/env bash
set -euo pipefail

repo_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${repo_dir}"

if [[ ! -f .env ]]; then
  echo "Missing .env. Copy .env.example and replace placeholders." >&2
  exit 1
fi

if grep -Eq '<[A-Z0-9_]+>' .env; then
  echo "Unresolved placeholders remain in .env." >&2
  exit 1
fi

for secret in deepseek_api_key session_secret admin_password_hash pseudonym_secret; do
  if [[ ! -s "/etc/finops-ai/${secret}" ]]; then
    echo "Missing /etc/finops-ai/${secret}" >&2
    exit 1
  fi
done

docker compose --env-file .env -f deploy/docker-compose.yml config >/dev/null
docker compose --env-file .env -f deploy/docker-compose.yml up -d --build
docker compose --env-file .env -f deploy/docker-compose.yml ps
