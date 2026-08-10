# Imagem do bot (long polling: só tráfego de SAÍDA — nenhuma porta exposta).
# Migrações rodam no boot (main.py chama alembic upgrade head).

FROM python:3.12-slim

# uv copiado da imagem oficial — sem curl|sh dentro do build
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

WORKDIR /app

# Camada de dependências separada: mexer no código não reinstala o mundo
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

# O projeto em si (uv sync instala o mordomo em modo editable — necessário para
# db/migracoes.py achar o alembic.ini na raiz via caminho relativo ao fonte)
COPY . .
RUN uv sync --frozen --no-dev

ENV TZ=America/Sao_Paulo \
    PYTHONUNBUFFERED=1

CMD ["uv", "run", "--no-sync", "python", "-m", "mordomo.main"]
