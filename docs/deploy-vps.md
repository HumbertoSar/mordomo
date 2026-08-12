# Deploy na VPS (Ubuntu 24.04)

No Telegram o bot usa **long polling**: só tráfego de saída, sem domínio, HTTPS
ou porta aberta. O WhatsApp (fase 3) muda isso — ele empurra os eventos por
webhook e exige HTTPS público: veja a **seção 8** no fim deste documento.

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

### Senha do banco (obrigatório em VPS compartilhada)

O bind em `127.0.0.1` barra a internet, mas **não outros processos e usuários
da mesma VPS** — e este banco guarda o cofre da família em claro. Gere uma
senha forte e ponha no `.env` ANTES do primeiro `docker compose up`:

```bash
echo "POSTGRES_PASSWORD=$(openssl rand -base64 24)" >> .env
```

**Se o banco já existe** (volume `mordomo_pgdata` inicializado), a variável
sozinha NÃO troca a senha — a imagem só a aplica na criação do volume. Troque
por dentro e depois recrie os containers para os dois lados combinarem:

```bash
docker compose exec postgres psql -U mordomo -d mordomo \
  -c "ALTER USER mordomo WITH PASSWORD 'A_SENHA_DO_ENV';"
docker compose --profile bot up -d --force-recreate
```

Se errar, o sintoma é o bot não conectar no boot — rode o `ALTER USER` de novo
com a senha que está no `.env` e recrie.

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

O dump contém o **cofre da família em claro**. Defina `BACKUP_PASSPHRASE` no
`.env` (e guarde uma cópia da passphrase fora da VPS, num gerenciador de
senhas) para o script gravar `.sql.gpg` criptografado. Restaurar:

```bash
gpg -d backups/mordomo_X.sql.gpg | docker compose exec -T postgres psql -U mordomo -d mordomo
```

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

## 8. WhatsApp (fase 3): subdomínio + Caddy do host

Diferente do Telegram, o WhatsApp **empurra** os eventos: precisa de uma URL
HTTPS pública. A VPS já tem um **Caddy no host** ocupando 80/443 (storyrender e
outros) — o webhook entra como **mais um site nesse Caddy**, nunca como um
segundo proxy.

```
Meta ──HTTPS──> Caddy do host (443) ──reverse_proxy──> 127.0.0.1:8090 (container do bot)
```

### 8.1 DNS

No painel da Hostinger, registro **A**: `mordomo` → IP da VPS. Confira antes de
seguir (o Caddy só emite certificado depois que o DNS propaga):

```bash
dig +short mordomo.SEUDOMINIO.com.br
```

### 8.2 Bloco no Caddyfile

```bash
nano /etc/caddy/Caddyfile
```

```caddyfile
mordomo.SEUDOMINIO.com.br {
	# Só as duas rotas que existem — o resto do mundo leva 404 sem chegar
	# ao bot. A autenticação de verdade é a assinatura da Meta, validada
	# no app; isto aqui é só reduzir a superfície.
	@mordomo path /whatsapp/webhook /healthz
	handle @mordomo {
		reverse_proxy 127.0.0.1:8090
	}
	handle {
		respond 404
	}
}
```

```bash
caddy validate --config /etc/caddy/Caddyfile   # não recarregue sem validar
systemctl reload caddy
```

### 8.3 Variáveis e subida

Preencha no `.env` da VPS as variáveis `WHATSAPP_*` (de onde vem cada uma:
[docs/whatsapp-fase3.md](whatsapp-fase3.md)) e recrie o container:

```bash
cd /opt/mordomo && git pull && docker compose --profile bot up -d --build
docker compose logs -f bot     # espere "Mordomo a postos (canais: telegram, whatsapp)"
```

A migração `a17c3e90b4d2` (dedupe por wamid + janela de 24h) roda sozinha no
boot, como as outras.

### 8.4 Provar o caminho ANTES de mexer no painel da Meta

```bash
curl -s https://mordomo.SEUDOMINIO.com.br/healthz
```

Resposta `{"ok":true,"fila":0}` = DNS, certificado, Caddy e container estão de
pé. Só então vá ao painel configurar o webhook (etapa 6 do guia) — se a tela da
Meta recusar depois disso, o problema é o verify token, não a infra.

> ⚠️ O bot precisa estar **rodando** na hora de salvar o webhook: a Meta faz o
> GET de verificação no ato.

### 8.5 Canais em paralelo (canário)

Os dois canais rodam no **mesmo processo**, com o mesmo grafo e o mesmo
checkpointer — e como thread = membro (ADR-003), quem migrar do Telegram para o
WhatsApp **continua a mesma conversa**, com histórico e memória. O plano é
canário: só você no WhatsApp por uma semana, a família no Telegram, comparando
os dois no dashboard (o campo `canal` já viaja em todo evento).

## Diagnóstico rápido

```bash
docker compose ps                      # os dois serviços "Up"?
docker compose logs --tail 50 bot      # boot completo? Langfuse validou?
docker compose exec postgres psql -U mordomo -d mordomo \
  -c "select tipo, count(*) from product_events group by tipo order by 2 desc;"
```
