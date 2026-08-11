"""Subagente Cofre — informações da família (CEP, documentos, dados de uso
recorrente). O dado mais sensível do sistema: ver ADR-005 e tools/cofre.py."""

from ..analytics import emitir_de, uso_de
from ..config import settings
from ..core.contexto import janela
from ..core.llm import chat_model, criar_agente
from ..core.state import EstadoMordomo
from ..tools.cofre import TOOLS_COFRE

PROMPT_COFRE = """Você é o guardião do COFRE do Mordomo da Família — informações
que a família consulta sempre: CEP, números de documento, carteirinha do plano,
placa do carro etc.

Você também cuida dos DOCUMENTOS em imagem (RG, carteirinha, comprovantes):
use buscar_documento para enviar e listar_documentos para conferir o que há.
Para GUARDAR um documento novo, o usuário envia a foto com legenda — explique
isso quando pedirem para guardar imagem.

Regras:
- Ao guardar, escolha uma chave curta e natural, do jeito que o usuário pediria
  depois (ex.: "CEP de casa", "RG do Davi"). Repita a chave na confirmação.
- Ao responder uma consulta, use SOMENTE o que a tool devolver. Se não achou,
  diga que não está no cofre e ofereça guardar — NUNCA invente um valor.
- "só pra mim" / "particular" → so_para_mim=True.
- Respostas curtas, tom de mordomo discreto (é um cofre!), formato WhatsApp.
"""

_agente = None


def agente_cofre():
    global _agente
    if _agente is None:
        _agente = criar_agente(chat_model(settings.model_agente), TOOLS_COFRE, PROMPT_COFRE)
    return _agente


async def no_cofre(state: EstadoMordomo, config) -> dict:
    # Janela (ADR-007) — ver nota em agents/lembretes.py.
    entrada = janela(state["messages"])
    anteriores = len(entrada)
    resultado = await agente_cofre().ainvoke({"messages": entrada}, config)

    novas = resultado["messages"][anteriores:] or resultado["messages"][-1:]
    if (uso := uso_de(novas)) is not None:
        await emitir_de(config, "llm_usage", no="cofre", **uso)

    return {"messages": [resultado["messages"][-1]]}
