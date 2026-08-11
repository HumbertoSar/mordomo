"""Pedidos da família → issues no GitHub.

A versão SEGURA do "agente que desenvolve funcionalidades": o bot NÃO escreve
código — ele transforma o pedido em backlog rastreável (issue com rótulo
`pedido-da-familia`), e o desenvolvimento segue o ciclo medido do projeto
(teste → eval → review → deploy).

Sem GITHUB_TOKEN o fluxo degrada de leve: o pedido ainda vira evento em
product_events (o dado não se perde) e a família recebe a mesma resposta —
só a issue não nasce. Token: fine-grained PAT com Issues read/write no repo."""

import logging

import httpx

from .config import settings

log = logging.getLogger(__name__)


_CATEGORIAS = {
    "funcionalidade": ("[família]", "pedido-da-familia", "Pedido"),
    "problema": ("[problema]", "problema-reportado", "Problema relatado"),
}


def montar_payload(titulo: str, pedido: str, autor: str, papel: str,
                   categoria: str = "funcionalidade") -> dict:
    """Corpo da issue — puro e testável. Categoria decide prefixo e rótulo."""
    prefixo, rotulo, substantivo = _CATEGORIAS.get(categoria, _CATEGORIAS["funcionalidade"])
    return {
        "title": f"{prefixo} {titulo.strip()[:80]}",
        "body": (
            f"**{substantivo} por {autor}** (papel: {papel}), direto do chat do mordomo:\n\n"
            f"> {pedido.strip()}\n\n"
            "_Aberto automaticamente pelo fluxo família-pede-vira-issue._"
        ),
        "labels": [rotulo],
    }


async def criar_issue(titulo: str, pedido: str, autor: str, papel: str,
                      categoria: str = "funcionalidade") -> str | None:
    """Abre a issue e devolve a URL, ou None (sem token / falha — nunca explode)."""
    if not settings.github_token:
        log.info("GITHUB_TOKEN ausente — pedido registrado só em product_events")
        return None
    try:
        async with httpx.AsyncClient(timeout=30) as cliente:
            resposta = await cliente.post(
                f"https://api.github.com/repos/{settings.github_repo}/issues",
                headers={
                    "Authorization": f"Bearer {settings.github_token}",
                    "Accept": "application/vnd.github+json",
                },
                json=montar_payload(titulo, pedido, autor, papel, categoria),
            )
            resposta.raise_for_status()
        return resposta.json().get("html_url")
    except Exception:
        log.exception("Falha ao abrir issue (pedido segue registrado em product_events)")
        return None
