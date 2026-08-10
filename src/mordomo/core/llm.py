"""Fábrica de modelos (OpenRouter) e de subagentes.

OpenRouter expõe API compatível com OpenAI: uma chave, vários modelos
(anthropic/*, google/*, openai/*, meta-llama/*…). Trocar de modelo = trocar
o id no .env — ótimo para comparar modelos nos evals depois.
"""

from langchain_openai import ChatOpenAI

from ..config import settings


def chat_model(nome_modelo: str, temperatura: float = 0.0) -> ChatOpenAI:
    extra = {}
    if settings.openrouter_provider_sort:
        # O OpenRouter serve o mesmo modelo a partir de hosts diferentes; sem
        # preferência, a escolha é dele. Medido em produção: p50 1,3s mas uma
        # chamada de 10,6s no mesmo prompt (ADR-006).
        extra["provider"] = {"sort": settings.openrouter_provider_sort}

    return ChatOpenAI(
        model=nome_modelo,
        api_key=settings.openrouter_api_key or "defina-OPENROUTER_API_KEY",
        base_url=settings.openrouter_base_url,
        temperature=temperatura,
        # Cauda de latência: abortar em 8s e repetir sai MUITO mais barato, em
        # tempo de espera do usuário, que aguardar um provedor travado.
        timeout=settings.llm_timeout_segundos,
        max_retries=settings.llm_max_retries,
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
