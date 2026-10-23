#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
umask 077
mkdir -p backups
chmod 700 backups
backup_file="backups/postgres-$(date -u +%Y%m%dT%H%M%SZ).dump"
trap 'rm -f "${backup_file}.partial"' EXIT
docker compose --env-file .env.server exec -T db pg_dump -U trainer -d trainer -Fc > "${backup_file}.partial"
docker compose --env-file .env.server exec -T db pg_restore --list < "${backup_file}.partial" > /dev/null
mv "${backup_file}.partial" "$backup_file"
find backups -maxdepth 1 -type f -name 'postgres-*.dump' -mtime +14 -delete
printf 'Backup created: %s\n' "$backup_file"
