"""Transcrição de áudio (Groq / Whisper) — responsabilidade de ADAPTER.

Pelo ADR-001, o núcleo nunca sabe se a mensagem nasceu de voz: o adapter
transcreve ANTES de montar o InboundMessage (que só carrega `veio_de_audio`
para o analytics). WhatsApp brasileiro é áudio — esta é a feature de adoção
mais importante da fase 2.

Groq expõe API compatível com OpenAI; o upload é multipart. Sem GROQ_API_KEY,
`transcrever` devolve None e o adapter recusa com simpatia — o bot inteiro
continua funcionando sem a chave (mesmo padrão do Langfuse)."""

import logging

import httpx

from ..config import settings

log = logging.getLogger(__name__)

_URL = "https://api.groq.com/openai/v1/audio/transcriptions"


def disponivel() -> bool:
    return bool(settings.groq_api_key)


async def transcrever(dados: bytes, nome_arquivo: str = "audio.ogg") -> str | None:
    """Áudio → texto pt-BR, ou None (sem chave, erro de rede, transcrição vazia).

    None nunca vira exceção para o usuário: quem chama decide a mensagem de
    fallback. Falha de transcrição não pode derrubar a conversa."""
    if not disponivel():
        return None
    try:
        async with httpx.AsyncClient(timeout=60) as cliente:
            resposta = await cliente.post(
                _URL,
                headers={"Authorization": f"Bearer {settings.groq_api_key}"},
                data={
                    "model": settings.groq_modelo_transcricao,
                    "language": "pt",
                    "response_format": "text",
                    "temperature": 0,
                },
                files={"file": (nome_arquivo, dados)},
            )
            resposta.raise_for_status()
        texto = resposta.text.strip()
        return texto or None
    except Exception:
        log.exception("Transcrição falhou (conversa segue; usuário recebe fallback)")
        return None
