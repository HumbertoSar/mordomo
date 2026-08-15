"""Roteamento e prazo de parede do OpenRouter.

O supervisor usa structured output. O MESMO modelo servido por hosts
diferentes não tem as mesmas capacidades: Bedrock e Azure ignoram
`response_format: json_schema` para `anthropic/*` e devolvem texto comum, o
que estoura o parser e deixa o bot mudo nos dois canais. Estes testes fixam a
forma do payload que impede isso de voltar."""

import asyncio
import json
import time

import pytest
from langchain_core.messages import HumanMessage
from pydantic import ValidationError

from mordomo.config import Settings, settings
from mordomo.core.llm import LLMDeadlineExceeded, chat_model


def _provider(modelo) -> dict:
    return (modelo.extra_body or {})["provider"]


def test_require_parameters_vai_sempre(monkeypatch):
    """Barra provedor que não aceita os parâmetros da requisição."""
    monkeypatch.setattr(settings, "openrouter_provider_order", "")
    monkeypatch.setattr(settings, "openrouter_provider_sort", "")
    assert _provider(chat_model("anthropic/claude-haiku-4.5"))["require_parameters"] is True


def test_ordem_de_provedores_trava_e_desliga_fallback(monkeypatch):
    """Com ordem definida, nada de cair em quem só finge suportar schema."""
    monkeypatch.setattr(settings, "openrouter_provider_order", "anthropic, google")
    p = _provider(chat_model("anthropic/claude-haiku-4.5"))
    assert p["order"] == ["anthropic", "google"]
    assert p["allow_fallbacks"] is False
    # sort não convive com order: ordenar por latência é o que trazia o Bedrock
    assert "sort" not in p


def test_sem_ordem_mantem_a_preferencia_de_latencia(monkeypatch):
    monkeypatch.setattr(settings, "openrouter_provider_order", "")
    monkeypatch.setattr(settings, "openrouter_provider_sort", "latency")
    p = _provider(chat_model("anthropic/claude-haiku-4.5"))
    assert p["sort"] == "latency" and "order" not in p


async def test_prazo_de_parede_corta_keep_alives_antes_do_fim(monkeypatch):
    """Bytes frequentes evitam read-timeout, mas não o prazo total do LLM."""
    resposta = json.dumps({
        "id": "chatcmpl-local",
        "object": "chat.completion",
        "created": 1,
        "model": "modelo-local",
        "choices": [{
            "index": 0,
            "message": {"role": "assistant", "content": "tarde demais"},
            "finish_reason": "stop",
        }],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
    }).encode()
    corpo = b" " * 30 + resposta

    async def servidor_lento(reader, writer):
        try:
            await reader.readuntil(b"\r\n\r\n")
            writer.write(
                b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
                + f"Content-Length: {len(corpo)}\r\nConnection: close\r\n\r\n".encode()
            )
            await writer.drain()
            for byte in corpo:
                writer.write(bytes([byte]))
                await writer.drain()
                await asyncio.sleep(0.02)  # abaixo do read-timeout de 50ms
        except (ConnectionError, asyncio.CancelledError):
            pass
        finally:
            writer.close()

    server = await asyncio.start_server(servidor_lento, "127.0.0.1", 0)
    porta = server.sockets[0].getsockname()[1]
    monkeypatch.setattr(settings, "openrouter_base_url", f"http://127.0.0.1:{porta}/v1")
    monkeypatch.setattr(settings, "llm_timeout_segundos", 0.05)
    monkeypatch.setattr(settings, "llm_prazo_total_segundos", 0.12)
    monkeypatch.setattr(settings, "llm_max_retries", 0)

    modelo = chat_model("modelo-local")
    inicio = time.perf_counter()
    try:
        with pytest.raises(LLMDeadlineExceeded, match="0.12"):
            await modelo.ainvoke([HumanMessage("oi")])
    finally:
        server.close()
        await server.wait_closed()

    assert 0.08 <= time.perf_counter() - inicio < 0.30


def test_prazo_total_do_llm_precisa_ser_finito_e_positivo():
    for invalido in (0, -1, float("nan"), float("inf")):
        with pytest.raises(ValidationError):
            Settings(_env_file=None, llm_prazo_total_segundos=invalido)

    assert Settings(_env_file=None).llm_prazo_total_segundos == 15.0
    assert Settings(_env_file=None, llm_prazo_total_segundos=0.1).llm_prazo_total_segundos == 0.1
