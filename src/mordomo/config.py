"""Configuração central via variáveis de ambiente (.env)."""

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # protected_namespaces=(): os campos model_supervisor/model_agente colidem com o
    # namespace "model_" reservado do Pydantic e gerariam warning a cada boot.
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore", protected_namespaces=()
    )

    # Canal
    telegram_bot_token: str = ""

    # LLM via OpenRouter (API compatível com OpenAI)
    openrouter_api_key: str = ""
    openrouter_base_url: str = "https://openrouter.ai/api/v1"
    model_supervisor: str = "anthropic/claude-haiku-4.5"
    model_agente: str = "anthropic/claude-sonnet-4.5"

    # Banco
    database_url: str = "postgresql+psycopg://mordomo:mordomo@localhost:5432/mordomo"

    # Observabilidade (vazio = desligado; o bot roda igual)
    langfuse_public_key: str = ""
    langfuse_secret_key: str = ""
    langfuse_host: str = "https://us.cloud.langfuse.com"

    # Comportamento
    tz_familia: str = "America/Sao_Paulo"
    debounce_segundos: float = 1.5

    @property
    def checkpointer_conn_string(self) -> str:
        """URL para o AsyncPostgresSaver (psycopg puro, sem o dialeto SQLAlchemy)."""
        return self.database_url.replace("postgresql+psycopg://", "postgresql://")


settings = Settings()
