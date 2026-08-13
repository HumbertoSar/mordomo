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
import pathlib
from datetime import datetime
from zoneinfo import ZoneInfo

from .config import settings

log = logging.getLogger(__name__)
_handler = None


def _release_do_git() -> str | None:
    """SHA curto lido direto de .git/ — sem binário git (a imagem slim não tem)
    e sem subprocess. No container o .git nem existe (.dockerignore): lá o SHA
    chega pronto em MORDOMO_RELEASE, injetado no build."""
    try:
        raiz = pathlib.Path(__file__).resolve().parents[2]
        head = (raiz / ".git" / "HEAD").read_text(encoding="ascii").strip()
        if not head.startswith("ref:"):
            return head[:12]  # HEAD solto (detached): já é o SHA
        ref = head.split(" ", 1)[1]
        arquivo_ref = raiz / ".git" / ref
        if arquivo_ref.exists():
            return arquivo_ref.read_text(encoding="ascii").strip()[:12]
        for linha in (raiz / ".git" / "packed-refs").read_text(encoding="ascii").splitlines():
            if linha.endswith(ref):
                return linha.split(" ", 1)[0][:12]
    except OSError:
        pass
    return None


def release_atual() -> str | None:
    """Carimbo de versão dos traces: env do build (produção) ou .git (dev)."""
    return settings.mordomo_release or _release_do_git()


def langfuse_callbacks() -> list:
    global _handler
    if not (settings.langfuse_public_key and settings.langfuse_secret_key):
        return []
    if _handler is None:
        try:
            from langfuse import Langfuse
            from langfuse.langchain import CallbackHandler

            # As chaves vivem no `settings` (o pydantic-settings lê o .env), NÃO no
            # os.environ — ninguém chama load_dotenv aqui. Se deixássemos o SDK se
            # autoconfigurar pelo ambiente, ele não acharia credencial nenhuma e os
            # traces sumiriam EM SILÊNCIO. Por isso a inicialização é explícita.
            from .privacidade import mascarar

            Langfuse(
                public_key=settings.langfuse_public_key,
                secret_key=settings.langfuse_secret_key,
                host=settings.langfuse_host,
                # ADR-005: valores do Cofre nunca chegam ao trace
                mask=mascarar,
                # "Caiu a partir de qual deploy?" — todo trace sai carimbado
                release=release_atual(),
            )
            _handler = CallbackHandler()
            log.info("Langfuse ligado (%s)", settings.langfuse_host)
        except Exception:
            log.exception("Langfuse indisponível — seguindo sem traces")
            return []
    return [_handler]


def checar_langfuse() -> bool:
    """Valida as credenciais no boot e diz em ALTO E BOM SOM se a observabilidade
    está desligada. Observabilidade que falha calada é pior que não ter nenhuma:
    você descobre semanas depois, sem os traces do período."""
    if not (settings.langfuse_public_key and settings.langfuse_secret_key):
        log.warning("Langfuse SEM CHAVES no .env — rodando sem traces (de propósito)")
        return False
    if not langfuse_callbacks():
        return False
    try:
        from langfuse import get_client

        get_client().auth_check()
        log.info("Langfuse: credenciais válidas")
        return True
    except Exception as erro:  # noqa: BLE001 — nenhuma falha de trace derruba a conversa
        log.error("Langfuse: credenciais RECUSADAS (%s) — seguindo sem traces", erro)
        return False


def session_id_de(member_id: int) -> str:
    """Sessão diária por membro (no fuso da família) — replay no Langfuse."""
    hoje = datetime.now(ZoneInfo(settings.tz_familia)).date()
    return f"{member_id}:{hoje.isoformat()}"


def session_de_grupo(grupo_id: str) -> str:
    """Sessão diária do GRUPO (ADR-008): a conversa coletiva tem replay próprio."""
    hoje = datetime.now(ZoneInfo(settings.tz_familia)).date()
    return f"g{grupo_id}:{hoje.isoformat()}"


def config_invocacao(
    member_id: int,
    member_nome: str,
    member_papel: str,
    turn_id: str,
    grupo_id: str | None = None,
    canal: str | None = None,
) -> dict:
    """Config do LangGraph: thread por MEMBRO (ADR-003) — ou por GRUPO (ADR-008),
    quando a conversa é coletiva; a identidade continua individual.

    `session_id` e `turn_id` viajam no MESMO `configurable` que já carrega o
    member_id — assim qualquer nó ou tool consegue emitir analytics rastreável
    sem receber parâmetro extra nem depender de variável global."""
    if grupo_id:
        thread_id, session_id = f"grupo-{grupo_id}", session_de_grupo(grupo_id)
    else:
        thread_id, session_id = f"membro-{member_id}", session_id_de(member_id)
    return {
        # Nome do run raiz = nome do TRACE no Langfuse. Sem isto, todo trace
        # se chama "LangGraph" e a lista vira parede; com a thread no nome,
        # dá para varrer a lista e saber de quem é cada turno sem abrir.
        "run_name": f"turno {thread_id}",
        "configurable": {
            "thread_id": thread_id,
            "member_id": member_id,
            "member_nome": member_nome,
            "member_papel": member_papel,
            "session_id": session_id,
            "turn_id": turn_id,
            # Nós que precisam saber que a conversa é COLETIVA (ex.: o cofre
            # recusa responder em grupo) leem daqui — nunca do texto do LLM.
            "grupo_id": grupo_id,
        },
        "callbacks": langfuse_callbacks(),
        "metadata": {
            "langfuse_user_id": str(member_id),
            "langfuse_session_id": session_id,
            # canal:whatsapp / canal:telegram — o filtro da semana de canário:
            # "me mostra só os replays do WhatsApp" e o score do juiz por canal.
            "langfuse_tags": ["mordomo", settings.ambiente]
            + ([f"canal:{canal}"] if canal else []),
            # Chaves sem prefixo langfuse_ viram metadata do trace: é a ponte
            # entre um trace e as linhas de product_events do mesmo turno.
            "turn_id": turn_id,
            **({"canal": canal} if canal else {}),
        },
    }
