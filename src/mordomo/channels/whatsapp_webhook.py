"""Webhook do WhatsApp — a borda HTTP, e só ela.

Duas rotas, as duas do jeito que a Meta exige:

  GET  /whatsapp/webhook  — verificação: devolve `hub.challenge` quando o
       `hub.verify_token` bate. É o que faz a tela do painel ficar verde.
  POST /whatsapp/webhook  — evento: valida a assinatura, ENFILEIRA e responde
       200 na hora. Processar antes de responder é o erro clássico: a Meta
       considera timeout (>~10s) e reentrega — por até 7 dias.

`fastapi` e `uvicorn` são importados DENTRO das funções de propósito: assim
`make test` roda sem o extra `whatsapp` instalado (regra nº 7) e o bot só
Telegram nunca depende deles.
"""

# SEM `from __future__ import annotations` de propósito: com ele as anotações
# viram strings e o FastAPI as resolve contra os GLOBAIS do módulo. Como
# `Request` é importado dentro da função (import preguiçoso), ele não estaria
# lá — e o FastAPI trataria `request` como parâmetro de QUERY, respondendo 422
# na verificação da Meta.
import hmac
import json
import logging

from ..config import settings
from .whatsapp import assinatura_valida

log = logging.getLogger(__name__)

ROTA = "/whatsapp/webhook"


def criar_app(adapter):
    """App FastAPI acoplado a UM adapter (quem tem a fila e o grafo)."""
    from fastapi import FastAPI, Request, Response

    app = FastAPI(title="Mordomo — webhook WhatsApp", docs_url=None, redoc_url=None)

    @app.get(ROTA)
    async def verificar(request: Request):
        p = request.query_params
        # compare_digest: o verify token é segredo compartilhado; comparar com
        # == vazaria, pelo tempo de resposta, quantos caracteres bateram.
        combina = hmac.compare_digest(
            (p.get("hub.verify_token") or ""), settings.whatsapp_verify_token
        )
        if p.get("hub.mode") == "subscribe" and combina:
            log.info("WhatsApp: verificação do webhook aceita")
            return Response(content=p.get("hub.challenge", ""), media_type="text/plain")
        log.warning("WhatsApp: verificação RECUSADA (token não confere)")
        return Response(status_code=403)

    @app.post(ROTA)
    async def receber(request: Request):
        corpo = await request.body()
        if not assinatura_valida(
            corpo, request.headers.get("X-Hub-Signature-256"), settings.whatsapp_app_secret
        ):
            # Quem conhece a URL mas não a chave secreta não fala com a família.
            log.warning("WhatsApp: POST com assinatura inválida — recusado")
            return Response(status_code=403)
        try:
            payload = json.loads(corpo)
        except ValueError:
            # 200 de propósito: payload que não é JSON não melhora com retry;
            # 4xx faria a Meta insistir por 7 dias no mesmo lixo.
            log.warning("WhatsApp: POST com corpo ilegível — ignorado")
            return Response(status_code=200)
        adapter.enfileirar(payload)
        return Response(status_code=200)

    @app.get("/healthz")
    async def saude():
        # Para o Caddy/monitoramento saberem que o processo está vivo sem
        # precisar mandar mensagem de verdade.
        return {"ok": True, "fila": adapter.fila.qsize()}

    return app


async def servir(adapter, porta: int | None = None) -> None:
    """Sobe o uvicorn no mesmo event loop do bot (mesmo processo, mesmo grafo,
    mesmo checkpointer — o WhatsApp é um SEGUNDO canal, não um segundo bot)."""
    import uvicorn

    porta = porta or settings.whatsapp_porta
    config = uvicorn.Config(
        criar_app(adapter),
        # 0.0.0.0 dentro do container; quem restringe a exposição é o
        # "127.0.0.1:8090:8090" do compose + o Caddy do host (docs/deploy-vps.md).
        host="0.0.0.0",
        port=porta,
        log_level="warning",
        access_log=False,
    )
    servidor = uvicorn.Server(config)
    log.info("WhatsApp: webhook ouvindo em :%d%s", porta, ROTA)
    await servidor.serve()
