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
#
# Criptografia: com BACKUP_PASSPHRASE no .env, o dump sai .sql.gpg (AES256) —
# o arquivo carrega o COFRE da família em claro; criptografado, um vazamento
# da pasta de backups não vira vazamento do cofre. Restaurar:
#   gpg -d backups/mordomo_X.sql.gpg | docker compose exec -T postgres psql -U mordomo -d mordomo

set -euo pipefail
# Dumps contêm Cofre, documentos e histórico: pasta nasce 700 e arquivos 600.
umask 077
cd "$(dirname "$0")/.."

MANTER_DIAS="${MANTER_DIAS:-30}"
mkdir -p backups

passphrase="${BACKUP_PASSPHRASE:-$(grep '^BACKUP_PASSPHRASE=' .env 2>/dev/null | tail -1 | cut -d= -f2- || true)}"

arquivo="backups/mordomo_$(date +%F_%H%M).sql"
if [ -n "$passphrase" ]; then
    arquivo="$arquivo.gpg"
    docker compose exec -T postgres pg_dump -U mordomo -d mordomo \
        | gpg --batch --yes --symmetric --cipher-algo AES256 \
              --passphrase "$passphrase" -o "$arquivo"
else
    echo "AVISO: sem BACKUP_PASSPHRASE no .env — dump em TEXTO PURO (contém o cofre)"
    docker compose exec -T postgres pg_dump -U mordomo -d mordomo > "$arquivo"
fi

echo "Backup gravado: $arquivo ($(du -h "$arquivo" | cut -f1))"
find backups -name 'mordomo_*.sql*' -mtime "+$MANTER_DIAS" -delete
