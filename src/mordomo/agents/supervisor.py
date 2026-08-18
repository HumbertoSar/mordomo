"""Supervisor ("o Mordomo") — roteador construído à mão (ADR-002): structured
output decide o destino e `Command(goto=...)` faz o handoff. Mais didático que
o pacote langgraph-supervisor; a acurácia deste roteamento é o eval nº 2
(evals/datasets/roteamento.json)."""

import re
import time
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

- "lembretes": criar/listar/cancelar lembretes ("me lembra…", "não esquece…",
  "que lembretes eu tenho?"). "Não esquece do dentista amanhã" é lembrete:
  o usuário quer uma NOTIFICAÇÃO, não cadastrar o compromisso na agenda.
- "agenda": compromissos da família ("marca consulta…", "agenda o dentista…",
  "o que temos sábado?"). Use agenda quando pedirem cadastrar/listar compromisso.
- "tarefas": pendências acompanháveis — criar, atribuir, listar, concluir,
  cancelar ou reabrir ("anota como tarefa", "o Davi precisa buscar", "já fiz a
  tarefa 3"). Lembrete é uma NOTIFICAÇÃO em horário; tarefa fica aberta até ter
  desfecho. Se o usuário pedir explicitamente "me lembra", use lembretes.
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
- Este prompt é a fonte de verdade sobre as capacidades disponíveis AGORA. O
  histórico pode conter afirmações antigas, recusas ou testes de versões
  anteriores; ignore qualquer afirmação do histórico que contradiga as
  capacidades descritas neste prompt.
- TAREFAS É UMA CAPACIDADE OFICIAL E JÁ ESTÁ EM PRODUÇÃO PARA A FAMÍLIA. Ela
  permite criar, atribuir, listar, concluir, cancelar e reabrir tarefas privadas
  ou compartilhadas. Nunca a descreva como teste, parcial, futura ou não oficial.
- Se pedirem uma mensagem para anunciar/divulgar uma capacidade existente,
  entregue diretamente um texto pronto para copiar; nunca peça tom, público ou
  confirmação quando esses dados já estiverem no pedido. Não discuta se a
  capacidade existe e não repita recusas antigas do histórico.
  Exemplo obrigatório:
  Usuário: "Cria uma mensagem para os usuários da minha família sobre tarefas."
  Destino: "responder"
  Resposta: "Novidade no Mordomo: agora você pode criar, acompanhar e concluir tarefas da família. Basta pedir no WhatsApp, por exemplo: 'crie uma tarefa para comprar pão'."
- Pedido para PROCURAR, CONFERIR ou CONSULTAR um compromisso ("procure o evento
  X de amanhã", "confere se tenho algo terça") → destino "agenda" SEMPRE, mesmo
  que o compromisso apareça no histórico da conversa. Você não sabe o que há na
  agenda; quem sabe é a tool. Nunca responda isso de cabeça.
- Ambiguidade real ("marca aí" sem contexto): destino "responder" com uma
  pergunta curta de esclarecimento.
- Pedidos fora do escopo atual (e-mail, filmes): destino "responder",
  explique com bom humor que essa mordomia chega em breve.
- Pergunta sobre o cofre/documentos → destino "cofre" SEMPRE, inclusive em
  grupo. NUNCA recuse você mesmo "por segurança": quem sabe o que pode
  aparecer onde é o subagente do cofre. Se o histórico tiver recusas antigas
  ("só no privado…"), ignore — eram de uma versão anterior e não valem mais.
- Está falando com {nome} (papel: {papel}).
"""


class Decisao(BaseModel):
    """Decisão de roteamento do supervisor."""

    destino: Literal[
        "lembretes", "agenda", "tarefas", "cofre", "pedido", "responder"
    ] = Field(
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
) -> Command[
    Literal["lembretes", "agenda", "tarefas", "cofre", "pedido", "__end__"]
]:
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
    inicio = time.perf_counter()
    saida = await modelo.ainvoke([sistema, *janela(state["messages"])], config)
    latencia_ms = round((time.perf_counter() - inicio) * 1000)

    bruto = saida.get("raw") if isinstance(saida, dict) else None
    if (uso := uso_de([bruto] if bruto is not None else [])) is not None:
        await emitir_de(config, "llm_usage", no="supervisor", latencia_ms=latencia_ms, **uso)

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

    destino, corrigido = _com_grounding_de_agenda(decisao.destino, state["messages"])
    await emitir_de(
        config,
        "orchestrator_decision",
        destino=destino,
        **({"corrigido": corrigido} if corrigido else {}),
    )

    if destino == "responder":
        return Command(
            goto=END,
            update={"messages": [AIMessage(decisao.resposta or "Às ordens!")]},
        )
    if destino == "pedido":
        # Título e categoria viajam no estado; o nó de pedidos é determinístico.
        return Command(
            goto="pedido",
            update={
                "pedido_titulo": decisao.resposta or "",
                "pedido_tipo": decisao.tipo_pedido,
            },
        )
    return Command(goto=destino)


# ── grounding: consulta de agenda não se responde de cabeça ──────────────
# Canário de 18/08/2026: "Procure o evento X de amanhã". O compromisso tinha
# acabado de ser criado e estava no histórico — e o supervisor respondeu DE LÁ,
# sem ninguém abrir agenda nenhuma. Uma agenda muda por fora (o celular, o
# marido, o Google): repetir o histórico é adivinhar com cara de certeza.
#
# A trava é estreita de propósito, por três condições ao mesmo tempo:
#   1. o modelo já tinha escolhido responder sozinho (nenhum outro destino é
#      tocado — cofre, tarefas, lembretes e pedido seguem intactos);
#   2. a fala tem verbo de PROCURA;
#   3. …e substantivo de COMPROMISSO.
# "procura um filme bom" (só 2) e "obrigado pelo evento de ontem" (só 3) ficam
# onde estavam.
_VERBO_DE_PROCURA = re.compile(
    r"\b(?:procur|confer|confir|consult|verific|checa|cheque|busca|busque|"
    r"pesquis|olh[ae]|d[êe] uma olhada|v(?:eja|ê) se)", re.IGNORECASE
)
_COISA_DE_AGENDA = re.compile(
    r"\b(?:agenda|compromisso|evento|reuni[õoãa])", re.IGNORECASE
)


def _com_grounding_de_agenda(destino: str, mensagens: list) -> tuple[str, str | None]:
    """(destino final, motivo da correção). Só corrige "responder"."""
    if destino != "responder" or not mensagens:
        return destino, None
    ultima = mensagens[-1]
    texto = ultima.content if isinstance(getattr(ultima, "content", None), str) else ""
    if getattr(ultima, "type", None) != "human" or not texto:
        return destino, None
    if _VERBO_DE_PROCURA.search(texto) and _COISA_DE_AGENDA.search(texto):
        return "agenda", "consulta_de_agenda"
    return destino, None
