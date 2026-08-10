# Deploy na VPS (Ubuntu 24.04)

O bot usa **long polling**: só tráfego de saída. Não precisa de domínio, HTTPS,
webhook nem porta aberta — isso muda apenas na fase 3 (WhatsApp Cloud API).

Modelo de operação a partir do deploy:

| | Onde | Bot (token) | Banco |
|---|---|---|---|
| **Produção** | VPS | o bot da família | Postgres do compose na VPS |
| **Dev** | sua máquina | um SEGUNDO bot (@BotFather) | Postgres local |

> ⚠️ **Um token = um processo.** Dois processos em long polling com o mesmo
> token disputam o `getUpdates` (erro 409 do Telegram). Antes de subir na VPS,
> pare o bot local — e crie um bot de dev para continuar desenvolvendo.

## 1. Preparar a VPS (uma vez)

```bash
ssh root@SEU_IP
curl -fsSL https://get.docker.com | sh
git clone https://github.com/HumbertoSar/mordomo.git /opt/mordomo
cd /opt/mordomo
```

## 2. Segredos

```bash
cp .env.example .env
nano .env
```

Preencha `TELEGRAM_BOT_TOKEN`, `OPENROUTER_API_KEY`, `GROQ_API_KEY` e as chaves
do Langfuse. **`DATABASE_URL` pode ficar como está**: o compose injeta a URL
certa (host `postgres`) por cima no container.

Se a VPS já tiver outro Postgres publicado em `127.0.0.1:5432` (é o caso da
VPS do Humberto — o storyrender usa), acrescente ao `.env`:

```
POSTGRES_HOST_PORT=5433
```

O bot não é afetado (fala com o banco pela rede interna do compose); a porta do
host só serve para diagnósticos com `psql` de fora.

## 3. Migrar os dados de casa (antes do primeiro boot!)

Na máquina Windows, gere o dump e envie:

```powershell
powershell -ExecutionPolicy Bypass -File scripts/backup.ps1
scp backups/mordomo_MAIS_RECENTE.sql root@SEU_IP:/opt/mordomo/
```

Na VPS, suba só o banco e restaure:

```bash
docker compose up -d postgres
sleep 5
docker compose exec -T postgres psql -U mordomo -d mordomo < mordomo_MAIS_RECENTE.sql
rm mordomo_MAIS_RECENTE.sql
```

Pular esta etapa = família perde lembretes, agenda e histórico de conversa.

## 4. Subir o bot

```bash
docker compose --profile bot up -d --build
docker compose logs -f bot     # espere "🤵 Mordomo a postos."
```

As migrações (Alembic) rodam sozinhas no boot. `restart: unless-stopped`
segura reboot da VPS e crash do processo.

## 5. Backup diário

```bash
chmod +x scripts/backup.sh
crontab -e
# adicionar:
# 0 3 * * * cd /opt/mordomo && ./scripts/backup.sh >> backups/backup.log 2>&1
```

Snapshots da Hostinger protegem o disco inteiro, mas um `pg_dump` diário é o
que permite restaurar SÓ o banco, e inspecionar o conteúdo.

## 6. Atualizar (a cada mudança de código)

```bash
cd /opt/mordomo && git pull && docker compose --profile bot up -d --build
```

## 7. Dev na sua máquina, sem conflito

1. Crie um segundo bot no @BotFather (ex.: `@Alfred_Dev_bot`).
2. No `.env` LOCAL, troque `TELEGRAM_BOT_TOKEN` pelo token de dev.
3. `make run` local conversa só com o bot de dev; a família nem percebe.

No Langfuse, se quiser separar os traces, mude a tag em
`observability.py::config_invocacao` (ex.: `"dev"` vs `"prod"`) — hoje tudo
sai como `fase1`.

## Diagnóstico rápido

```bash
docker compose ps                      # os dois serviços "Up"?
docker compose logs --tail 50 bot      # boot completo? Langfuse validou?
docker compose exec postgres psql -U mordomo -d mordomo \
  -c "select tipo, count(*) from product_events group by tipo order by 2 desc;"
```
