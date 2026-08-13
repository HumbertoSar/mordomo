# Imagem do bot. O Telegram é long polling (só tráfego de SAÍDA); o WhatsApp
# (fase 3) expõe o webhook numa porta INTERNA — quem fala HTTPS com a Meta é o
# Caddy do host (docs/deploy-vps.md). Migrações rodam no boot.

FROM python:3.12-slim

# uv copiado da imagem oficial — sem curl|sh dentro do build
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

WORKDIR /app

# Camada de dependências separada: mexer no código não reinstala o mundo
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --extra whatsapp --no-install-project

# O projeto em si (uv sync instala o mordomo em modo editable — necessário para
# db/migracoes.py achar o alembic.ini na raiz via caminho relativo ao fonte)
COPY . .
RUN uv sync --frozen --no-dev --extra whatsapp

# Release dos traces do Langfuse: o SHA entra por build-arg porque o .git fica
# fora da imagem (.dockerignore). Vazio = traces sem release (dev/local).
ARG GIT_SHA=""
ENV TZ=America/Sao_Paulo \
    PYTHONUNBUFFERED=1 \
    MORDOMO_RELEASE=$GIT_SHA

CMD ["uv", "run", "--no-sync", "python", "-m", "mordomo.main"]
