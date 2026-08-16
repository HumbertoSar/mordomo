"""Rota pública do callback OAuth do Google — a borda HTTP, e só ela.

Mora no MESMO FastAPI do webhook do WhatsApp de propósito: um processo, uma
porta interna, uma entrada no Caddy. Um segundo servidor só para receber um
redirect seria mais superfície exposta para nada.

  GET /integracoes/google/callback?code=…&state=…  → troca e conecta
  GET /integracoes/google/callback?error=…&state=… → a pessoa clicou Cancelar

A página devolvida é HTML mínimo e SEM detalhe sensível: quem vê é o
navegador, e a URL desta rota fica no histórico. Motivo técnico vai para o
log do servidor, nunca para a tela.

SEM `from __future__ import annotations` — mesma armadilha do
`whatsapp_webhook.py`: com o future import as anotações viram string, o
FastAPI as resolve contra os globais do módulo e um parâmetro importado
preguiçosamente vira query obrigatória (422 em tudo).
"""

import html
import logging

from .google import (
    PAINEL_GOOGLE,
    processar_callback,
)
from .google_api import GoogleAPI

log = logging.getLogger(__name__)

ROTA = "/integracoes/google/callback"

# Texto por motivo categórico. O que NÃO estiver aqui cai no genérico: é
# melhor uma página educada que um vazamento de detalhe interno.
_EXPLICACAO = {
    # Recusar não é erro: o texto acolhe E deixa o caminho de volta aberto,
    # senão quem clicou em Cancelar sem querer fica sem saber como tentar.
    "negado_pelo_usuario": (
        "Você escolheu não autorizar — está tudo bem, nada foi conectado. "
        "Se mudar de ideia, é só mandar /google na conversa."
    ),
    "state_expirado": "O link expirou. Peça outro com /google na conversa.",
    "state_usado": "Este link já foi usado uma vez. Peça outro com /google.",
    "state_invalido": "Este link não confere. Peça outro com /google na conversa.",
    "sem_code": "O Google não completou a autorização. Tente de novo com /google.",
    # A tela do Google tem caixinha: dá para autorizar "quase tudo". Sem a
    # permissão de agenda não guardamos nada — e a pessoa precisa saber disso.
    "escopo_insuficiente": (
        "Faltou marcar a permissão de ver e criar eventos na sua agenda — sem "
        "ela eu não consigo ajudar, e por isso não guardei nada. Tente de novo "
        "com /google e mantenha essa caixinha marcada."
    ),
    "troca_falhou": "Não consegui concluir com o Google agora. Tente de novo com /google.",
    "indisponivel": "A integração com o Google não está disponível no momento.",
}
_GENERICA = "Não consegui concluir a conexão. Tente de novo com /google na conversa."


def _pagina(titulo: str, mensagem: str, rodape: str = "") -> str:
    """HTML mínimo, autocontido, sem CSS externo, sem JS, sem imagem.

    `noindex` porque esta página é o fim de um fluxo de conta — não conteúdo
    a ser encontrado numa busca. `escape` em tudo que é texto: mesmo vindo de
    constante nossa, é a rota pública do projeto e não abre exceção."""
    return (
        "<!doctype html><html lang='pt-BR'><head><meta charset='utf-8'>"
        "<meta name='robots' content='noindex,nofollow'>"
        "<meta name='viewport' content='width=device-width,initial-scale=1'>"
        f"<title>{html.escape(titulo)}</title></head>"
        "<body style='font-family:system-ui,sans-serif;max-width:32rem;"
        "margin:3rem auto;padding:0 1rem;line-height:1.5'>"
        f"<h1 style='font-size:1.3rem'>{html.escape(titulo)}</h1>"
        f"<p>{html.escape(mensagem)}</p>"
        + (f"<p style='color:#555;font-size:.9rem'>{rodape}</p>" if rodape else "")
        + "</body></html>"
    )


def pagina_de_sucesso() -> str:
    return _pagina(
        "Agenda conectada 🤵",
        "Pronto! Já posso cuidar da sua agenda. Pode fechar esta página e "
        "voltar para a nossa conversa no WhatsApp ou no Telegram.",
        "Para conferir agora, mande <strong>/google_teste</strong> — eu crio um "
        "evento de teste. Para encerrar quando quiser, "
        "<strong>/google_desconectar</strong>.",
    )


def pagina_de_erro(motivo: str) -> str:
    return _pagina(
        "Não deu certo",
        _EXPLICACAO.get(motivo, _GENERICA),
        "Você pode fechar esta página e voltar para a conversa no WhatsApp ou "
        f"no Telegram. Seus acessos ficam sempre visíveis em {PAINEL_GOOGLE}.",
    )


def criar_router(api: GoogleAPI | None = None):
    """Router do callback. `api` é injeção de transporte para os testes —
    em produção fica None e o cliente httpx real nasce na hora."""
    from fastapi import APIRouter, Response

    router = APIRouter()

    @router.get(ROTA, include_in_schema=False)
    async def callback(
        code: str | None = None,
        state: str | None = None,
        error: str | None = None,
    ):
        # Nenhum parâmetro daqui é confiável: o member_id sai do `state`
        # PERSISTIDO, dentro de processar_callback. Nada do que chega na query
        # é logado inteiro — nem code, nem state.
        try:
            ok, motivo = await processar_callback(code, state, erro=error, api=api)
        except Exception:
            # Rota pública: um erro nosso não pode virar stacktrace na tela
            # (o FastAPI devolveria 500 com detalhe em modo debug).
            log.exception("Google: callback estourou")
            return Response(pagina_de_erro("erro_interno"), status_code=500,
                            media_type="text/html")
        if ok:
            log.info("Google: callback concluído com sucesso")
            return Response(pagina_de_sucesso(), media_type="text/html")
        # `motivo` é categórico (nunca texto do Google) — seguro para o log.
        log.warning("Google: callback recusado (%s)", motivo)
        return Response(pagina_de_erro(motivo), status_code=400, media_type="text/html")

    return router
