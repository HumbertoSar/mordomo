"""Fábrica de modelos (OpenRouter) e de subagentes.

OpenRouter expõe API compatível com OpenAI: uma chave, vários modelos
(anthropic/*, google/*, openai/*, meta-llama/*…). Trocar de modelo = trocar
o id no .env — ótimo para comparar modelos nos evals depois.
"""

import asyncio
from typing import Any

from langchain_openai import ChatOpenAI
from pydantic import Field

from ..config import settings


class LLMDeadlineExceeded(TimeoutError):
    """Uma chamada assíncrona ao modelo excedeu seu prazo total de parede."""

    def __init__(self, limite_segundos: float):
        self.limite_segundos = limite_segundos
        super().__init__(f"prazo total do LLM excedido ({limite_segundos:g}s)")


class DeadlineChatOpenAI(ChatOpenAI):
    """ChatOpenAI cujo prazo cobre somente a chamada de modelo, nunca tools."""

    prazo_total_segundos: float = Field(gt=0, allow_inf_nan=False, exclude=True)

    async def _agenerate(self, *args: Any, **kwargs: Any):
        prazo = asyncio.timeout(self.prazo_total_segundos)
        try:
            async with prazo:
                return await super()._agenerate(*args, **kwargs)
        except TimeoutError as erro:
            # Não reclassifique timeout HTTP/de dependência: só o vencimento do
            # nosso relógio ganha a exceção específica usada pelo analytics.
            if not prazo.expired():
                raise
            raise LLMDeadlineExceeded(self.prazo_total_segundos) from erro


def chat_model(nome_modelo: str, temperatura: float = 0.0) -> ChatOpenAI:
    # `require_parameters` é o que impede o incidente de 13/08/2026: o mesmo
    # modelo servido por hosts diferentes NÃO tem as mesmas capacidades. Com
    # sort=latency o OpenRouter mandou o supervisor para o Amazon Bedrock, que
    # ignora `response_format: json_schema` — o modelo respondeu texto comum, o
    # parser do SDK explodiu em ValidationError e a família levou "Ops,
    # tropecei" em TODOS os turnos, nos dois canais, por horas.
    #
    # Com a flag, o OpenRouter só roteia para provedores que suportam TODOS os
    # parâmetros enviados; quem não faz structured output sai do sorteio.
    # Custa nada em quem já suporta e é a diferença entre roteador funcionando
    # e bot mudo.
    extra: dict = {"provider": {"require_parameters": True}}
    if settings.openrouter_provider_order:
        # Trava o roteamento nos provedores que HONRAM o json_schema. Sem
        # `allow_fallbacks: False` o OpenRouter volta a cair em quem só finge
        # suportar — e a falha é silenciosa: em vez de erro, vem texto que
        # rebenta no parser. Provedor fora do ar aqui devolve ERRO, que o
        # pipeline trata com retry e mensagem honesta; é o mal menor.
        extra["provider"]["order"] = [
            p.strip() for p in settings.openrouter_provider_order.split(",") if p.strip()
        ]
        extra["provider"]["allow_fallbacks"] = False
    elif settings.openrouter_provider_sort:
        # O OpenRouter serve o mesmo modelo a partir de hosts diferentes; sem
        # preferência, a escolha é dele. Medido em produção: p50 1,3s mas uma
        # chamada de 10,6s no mesmo prompt (ADR-006).
        extra["provider"]["sort"] = settings.openrouter_provider_sort

    return DeadlineChatOpenAI(
        model=nome_modelo,
        api_key=settings.openrouter_api_key or "defina-OPENROUTER_API_KEY",
        base_url=settings.openrouter_base_url,
        temperature=temperatura,
        # Cauda de latência: abortar em 8s e repetir sai MUITO mais barato, em
        # tempo de espera do usuário, que aguardar um provedor travado.
        timeout=settings.llm_timeout_segundos,
        max_retries=settings.llm_max_retries,
        prazo_total_segundos=settings.llm_prazo_total_segundos,
        # Cabeçalhos opcionais de atribuição do OpenRouter:
        default_headers={"X-Title": "Mordomo da Familia"},
        **({"extra_body": extra} if extra else {}),
    )


def criar_agente(model, tools: list, prompt: str):
    """Cria um subagente ReAct (LLM + tools em loop).

    Tenta a API atual do LangChain 1.x (`create_agent`) e cai para o prebuilt
    do LangGraph se a assinatura mudar — se ambos falharem, confira a versão
    instalada e a documentação (ver CLAUDE.md).
    """
    try:
        from langchain.agents import create_agent

        return create_agent(model, tools, system_prompt=prompt)
    except (ImportError, TypeError):
        from langgraph.prebuilt import create_react_agent

        return create_react_agent(model, tools, prompt=prompt)
