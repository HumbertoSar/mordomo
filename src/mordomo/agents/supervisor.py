"""Supervisor ("o Mordomo") — roteador construído à mão (ADR-002): structured
output decide o destino e `Command(goto=...)` faz o handoff. Mais didático que
o pacote langgraph-supervisor; a acurácia deste roteamento é o eval nº 2
(evals/datasets/roteamento.json)."""

from typing import Literal

from langchain_core.messages import AIMessage, SystemMessage
from langgraph.graph import END
from langgraph.types import Command
from pydantic import BaseModel, Field

from ..analytics import emitir_de, uso_de
from ..config import settings
from ..core.contexto import janela
from ..core.llm import chat_model
from ..core.state import EstadoMordomo

PROMPT_SUPERVISOR = """Você é o Mordomo da Família: educado, caloroso, direto \
e brasileiro. Você conversa pelo Telegram/WhatsApp, então respostas são CURTAS.

Sua única função neste passo é DECIDIR o destino da mensagem:

- "lembretes": criar/listar/cancelar lembretes ("me lembra…", "que lembretes eu tenho?")
- "agenda": compromissos da família ("marca consulta…", "o que temos sábado?")
- "cofre": guardar/consultar informações da família ("anota o CEP…", "qual o
  número da carteirinha do Davi?") e também ENVIAR documentos e fotos
  guardados ("me manda o RG do Davi", "preciso da carteirinha de saúde")
- "pedido": a pessoa SUGERE uma funcionalidade nova ("você devia saber pedir
  pizza"), PEDE uma mudança de comportamento/preferência ("quero que o CPF
  seja sempre gravado no formato X", "pare de usar emoji comigo") OU RELATA
  um problema/erro seu ("deu erro quando pedi o lembrete", "você entendeu
  tudo errado no meu áudio"). Preencha
  `resposta` com um TÍTULO curto e `tipo_pedido` com "funcionalidade" ou
  "problema".
  Atenção: pedir algo que você NÃO faz hoje ("me indica um filme") é
  "responder" (fora de escopo); e reclamação que você pode RESOLVER AGORA
  ("cancela esse lembrete errado") vai para o subagente certo, não é pedido.
- "responder": cumprimentos, agradecimentos, small talk ou perguntas gerais que
  você mesmo responde em 1-2 frases (preencha o campo `resposta`).

Regras:
- Ambiguidade real ("marca aí" sem contexto): destino "responder" com uma
  pergunta curta de esclarecimento.
- Pedidos fora do escopo atual (e-mail, filmes, tarefas): destino "responder",
  explique com bom humor que essa mordomia chega em breve.
- Está falando com {nome} (papel: {papel}).
"""


class Decisao(BaseModel):
    """Decisão de roteamento do supervisor."""

    destino: Literal["lembretes", "agenda", "cofre", "pedido", "responder"] = Field(
        description="Subagente de destino, ou 'responder' para responder diretamente"
    )
    resposta: str | None = Field(
        default=None, description="Resposta curta ao usuário quando destino='responder'"
    )
    tipo_pedido: Literal["funcionalidade", "problema"] = Field(
        default="funcionalidade",
        description="Só quando destino='pedido': sugestão nova ou relato de erro",
    )


async def no_supervisor(
    state: EstadoMordomo, config
) -> Command[Literal["lembretes", "agenda", "cofre", "pedido", "__end__"]]:
    # include_raw=True devolve {"raw", "parsed", "parsing_error"}. Sem ele o
    # AIMessage — e portanto o usage_metadata — era descartado, e uma falha de
    # parse virava exceção genérica ("Ops, tropecei"), indistinguível de um bug
    # de banco. Com ele, "o modelo não produziu JSON válido" ganha evento
    # próprio: é o sintoma exato de modelo fraco em tool calling.
    modelo = chat_model(settings.model_supervisor).with_structured_output(
        Decisao, include_raw=True
    )
    sistema = SystemMessage(
        PROMPT_SUPERVISOR.format(
            nome=state.get("member_nome", "?"), papel=state.get("member_papel", "?")
        )
    )
    # Janela (ADR-007): rotear entre três destinos não precisa da conversa
    # inteira — e sem isto a entrada crescia ~72 tokens/turno, para sempre.
    saida = await modelo.ainvoke([sistema, *janela(state["messages"])], config)

    bruto = saida.get("raw") if isinstance(saida, dict) else None
    if (uso := uso_de([bruto] if bruto is not None else [])) is not None:
        await emitir_de(config, "llm_usage", no="supervisor", **uso)

    decisao: Decisao | None = saida.get("parsed") if isinstance(saida, dict) else saida
    if decisao is None:
        erro = saida.get("parsing_error") if isinstance(saida, dict) else None
        await emitir_de(
            config,
            "orchestrator_parse_error",
            modelo=settings.model_supervisor,
            erro=str(erro)[:200],
        )
        return Command(
            goto=END,
            update={"messages": [AIMessage("Me perdi por um instante. Pode repetir?")]},
        )

    await emitir_de(config, "orchestrator_decision", destino=decisao.destino)

    if decisao.destino == "responder":
        return Command(
            goto=END,
            update={"messages": [AIMessage(decisao.resposta or "Às ordens!")]},
        )
    if decisao.destino == "pedido":
        # Título e categoria viajam no estado; o nó de pedidos é determinístico.
        return Command(
            goto="pedido",
            update={
                "pedido_titulo": decisao.resposta or "",
                "pedido_tipo": decisao.tipo_pedido,
            },
        )
    return Command(goto=decisao.destino)
