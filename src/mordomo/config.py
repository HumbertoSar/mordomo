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

    # Transcrição de áudio (Groq/Whisper). Vazio = áudio recusado com simpatia;
    # o resto do bot funciona igual (mesmo padrão do Langfuse).
    groq_api_key: str = ""
    groq_modelo_transcricao: str = "whisper-large-v3-turbo"

    # Banco
    database_url: str = "postgresql+psycopg://mordomo:mordomo@localhost:5432/mordomo"

    # Observabilidade (vazio = desligado; o bot roda igual)
    langfuse_public_key: str = ""
    langfuse_secret_key: str = ""
    langfuse_host: str = "https://us.cloud.langfuse.com"

    # Latência do LLM. A mediana do supervisor é ~1,3s, mas medimos uma chamada de
    # 10,6s no MESMO modelo e prompt (trace do Langfuse): o OpenRouter escolhe entre
    # hosts para o mesmo modelo e às vezes cai num lento. Esperar não ajuda —
    # aborta e tenta de novo. Ver docs/adr/006-latencia-e-provedores.md.
    llm_timeout_segundos: float = 8.0
    llm_max_retries: int = 2
    openrouter_provider_sort: str = "latency"  # "latency" | "throughput" | "price" | "" (sem preferência)

    # Contexto por chamada (ADR-007). O histórico completo fica no checkpointer;
    # ao LLM vão só as últimas N mensagens. Medido no primeiro dia de uso real:
    # sem janela, a entrada do supervisor crescia ~72 tokens/turno, sem teto.
    # 0 = desligado (comportamento antigo).
    contexto_janela_mensagens: int = 8

    # Ambiente: separa os traces do Langfuse (tag) entre a máquina de dev e a
    # VPS — os DADOS já são separados por banco; isto separa a observabilidade.
    # Default "dev" de propósito: produção é quem declara AMBIENTE=prod.
    ambiente: str = "dev"

    # Comportamento
    tz_familia: str = "America/Sao_Paulo"
    debounce_segundos: float = 1.5
    briefing_hora: str = "07:30"  # briefing matinal proativo; vazio desliga

    @property
    def checkpointer_conn_string(self) -> str:
        """URL para o AsyncPostgresSaver (psycopg puro, sem o dialeto SQLAlchemy)."""
        return self.database_url.replace("postgresql+psycopg://", "postgresql://")


settings = Settings()
