"""Observabilidade: Langfuse (traces) via callback do LangChain.

Convenções (docs do projeto):
  - user  = membro da família (langfuse_user_id = member_id)
  - session = conversa do dia (langfuse_session_id = "<member_id>:<data>")
  - trace: gateway → supervisor → subagente → tool, tudo aninhado

Sem chaves no .env → lista vazia de callbacks e o bot roda igual.
Nota OpenRouter: o cálculo automático de custo do Langfuse pode não reconhecer
todos os ids de modelo do OpenRouter — dá para cadastrar preços por modelo no
próprio Langfuse (Settings → Models)."""

import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from .config import settings

log = logging.getLogger(__name__)
_handler = None


def langfuse_callbacks() -> list:
    global _handler
    if not (settings.langfuse_public_key and settings.langfuse_secret_key):
        return []
    if _handler is None:
        try:
            from langfuse.langchain import CallbackHandler

            _handler = CallbackHandler()
            log.info("Langfuse ligado (%s)", settings.langfuse_host)
        except Exception:
            log.exception("Langfuse indisponível — seguindo sem traces")
            return []
    return [_handler]


def session_id_de(member_id: int) -> str:
    """Sessão diária por membro (no fuso da família) — replay no Langfuse."""
    hoje = datetime.now(ZoneInfo(settings.tz_familia)).date()
    return f"{member_id}:{hoje.isoformat()}"


def config_invocacao(member_id: int, member_nome: str, member_papel: str) -> dict:
    """Config do LangGraph: thread por MEMBRO (ADR-003) + metadados Langfuse."""
    return {
        "configurable": {
            "thread_id": f"membro-{member_id}",
            "member_id": member_id,
            "member_nome": member_nome,
            "member_papel": member_papel,
        },
        "callbacks": langfuse_callbacks(),
        "metadata": {
            "langfuse_user_id": str(member_id),
            "langfuse_session_id": session_id_de(member_id),
            "langfuse_tags": ["mordomo", "fase1"],
        },
    }
