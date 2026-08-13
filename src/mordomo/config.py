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

    # ── WhatsApp Cloud API (fase 3) ──────────────────────────────────────
    # Tudo vazio = canal DESLIGADO e o bot sobe só com Telegram (mesmo padrão
    # do Langfuse e do Groq). Como obter cada valor: docs/whatsapp-fase3.md.
    whatsapp_token: str = ""             # System User, permanente (etapa 3)
    whatsapp_phone_number_id: str = ""   # id do número remetente (etapa 2)
    whatsapp_waba_id: str = ""           # conta WhatsApp Business (templates)
    whatsapp_app_secret: str = ""        # valida X-Hub-Signature-256 (etapa 4)
    whatsapp_verify_token: str = ""      # segredo do GET de verificação (etapa 5)
    # A Meta descontinua versão da Graph API a cada ~2 anos — por isso é config,
    # não constante. Changelog: developers.facebook.com/docs/graph-api/changelog
    whatsapp_api_version: str = "v25.0"
    whatsapp_porta: int = 8090           # porta INTERNA; quem fala HTTPS é o Caddy
    # Fora da janela de 24h só sai template aprovado (imutável após aprovação —
    # daí o sufixo de versão). Vazio = proativo fora da janela é ENGOLIDO com
    # aviso no log, nunca vira erro para a família.
    whatsapp_template_lembrete: str = "lembrete_v1"
    whatsapp_template_idioma: str = "pt_BR"
    whatsapp_janela_horas: int = 24      # regra da Meta; existe para o teste fixar

    # Proatividade multi-canal: quem tem as duas identidades recebe por este
    # canal. Durante o canário a família segue no Telegram — o que decide é
    # existir identidade no canal, não esta variável.
    canal_preferido: str = "whatsapp"

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

    # Pedidos da família → issues (vazio = só registra em product_events)
    github_token: str = ""
    github_repo: str = "HumbertoSar/mordomo"
    # O repo acima é PÚBLICO: por padrão a issue leva só o título curto — o
    # texto original e o autor ficam em product_events. True só se o repo das
    # issues for privado (ou você aceitar publicar conversa da família).
    github_issues_detalhadas: bool = False

    # Comportamento
    tz_familia: str = "America/Sao_Paulo"
    debounce_segundos: float = 1.5
    briefing_hora: str = "07:30"  # briefing matinal proativo; vazio desliga
    curadoria_hora: str = "18:30"  # curadoria de evals aos domingos; vazio desliga

    @property
    def whatsapp_habilitado(self) -> bool:
        """Sem as 4 credenciais o canal nem sobe — meia configuração é pior que
        nenhuma: o webhook responderia 200 sem conseguir devolver mensagem."""
        return bool(
            self.whatsapp_token
            and self.whatsapp_phone_number_id
            and self.whatsapp_app_secret
            and self.whatsapp_verify_token
        )

    @property
    def checkpointer_conn_string(self) -> str:
        """URL para o AsyncPostgresSaver (psycopg puro, sem o dialeto SQLAlchemy)."""
        return self.database_url.replace("postgresql+psycopg://", "postgresql://")


settings = Settings()
