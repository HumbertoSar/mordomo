#!/usr/bin/env bash
# Backup do Postgres do Mordomo na VPS (equivalente Linux do backup.ps1).
#
#   ./scripts/backup.sh                  # grava em ./backups, retém 30 dias
#
# Agendar diário às 3h (crontab -e):
#   0 3 * * * cd /opt/mordomo && ./scripts/backup.sh >> backups/backup.log 2>&1
#
# Dado de família não tem segunda chance: aqui moram lembretes, agenda, o
# histórico de conversa (checkpointer) e a série de product_events inteira.

set -euo pipefail
cd "$(dirname "$0")/.."

MANTER_DIAS="${MANTER_DIAS:-30}"
mkdir -p backups

arquivo="backups/mordomo_$(date +%F_%H%M).sql"
docker compose exec -T postgres pg_dump -U mordomo -d mordomo > "$arquivo"

echo "Backup gravado: $arquivo ($(du -h "$arquivo" | cut -f1))"
find backups -name 'mordomo_*.sql' -mtime "+$MANTER_DIAS" -delete
