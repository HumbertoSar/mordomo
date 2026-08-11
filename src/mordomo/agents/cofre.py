"""Subagente Cofre — informações da família (CEP, documentos, dados de uso
recorrente). O dado mais sensível do sistema: ver ADR-005 e tools/cofre.py."""

from langchain_core.messages import AIMessage

from ..analytics import emitir_de
from ..core.state import EstadoMordomo
from ..tools.cofre import TOOLS_COFRE
from ._base import NoSubagente

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

_no_cofre_base = NoSubagente("cofre", TOOLS_COFRE, PROMPT_COFRE)


async def no_cofre(state: EstadoMordomo, config) -> dict:
    # Cofre NUNCA responde em grupo (ADR-008): a resposta iria para todos no
    # chat — inclusive não-membros que estejam lá — com valor ou documento.
    # Guard determinístico ANTES do LLM: prompt não é barreira de segurança.
    if config.get("configurable", {}).get("grupo_id"):
        await emitir_de(config, "cofre_recusado_grupo")
        return {"messages": [AIMessage(
            "Assunto de cofre é só no privado — me chame lá que eu te atendo. 🤫"
        )]}
    return await _no_cofre_base(state, config)
