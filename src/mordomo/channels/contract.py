"""Contrato interno de mensagens — o coração da portabilidade Telegram → WhatsApp.

Regra de ouro (ADR-001): o núcleo (LangGraph) NUNCA sabe em que canal está.
Ele consome `InboundMessage` e produz `OutboundMessage` SEMÂNTICA (texto +
intenção de interação), nunca widgets concretos. Quem decide entre inline
keyboard, reply buttons, list message ou lista numerada em texto é o renderer
de cada adapter, guiado por `Capabilities` + `plan_rendering()` (função pura,
coberta por testes em tests/test_contract_renderer.py).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Protocol

# ── Entrada ──────────────────────────────────────────────────────────────


@dataclass
class InboundMessage:
    member_id: int
    canal: str                       # "telegram" | "whatsapp"
    texto: str                       # já agregado (debounce) e transcrito (se áudio)
    message_id: str                  # id no canal (dedupe: wamid no WhatsApp)
    timestamp: datetime
    veio_de_audio: bool = False
    reply_to: str | None = None
    # ADR-008: preenchido quando a mensagem veio de um GRUPO — muda a thread
    # (grupo, não membro) e a sessão de analytics. O núcleo não sabe o canal;
    # sabe que a conversa é coletiva.
    grupo_id: str | None = None


# ── Saída (semântica, não visual) ────────────────────────────────────────


@dataclass
class ChoiceOption:
    id: str                          # curto e estável (vira callback_data / row id)
    rotulo: str                      # o que o usuário vê (respeitar limites do canal!)


@dataclass
class Confirmation:
    """Confirmação sim/não (ex.: antes de enviar e-mail — HITL, fase 2)."""

    sim_rotulo: str = "Sim"
    nao_rotulo: str = "Não"


@dataclass
class Choice:
    """Escolha única entre N opções."""

    opcoes: list[ChoiceOption] = field(default_factory=list)


Interaction = Confirmation | Choice | None


@dataclass
class Anexo:
    """Arquivo a ENVIAR junto com a mensagem — semântico, não bytes.

    O núcleo referencia o documento por id; quem carrega os bytes do banco e
    escolhe como entregar (foto, documento, link) é o adapter do canal. Assim
    o conteúdo nunca passa pelo LLM nem pelos traces (ADR-005)."""

    documento_id: int
    nome: str                        # "RG do Davi" — vira legenda/nome de arquivo
    mime: str = "image/jpeg"


@dataclass
class OutboundMessage:
    texto: str
    interacao: Interaction = None    # None = informativo (texto puro)
    anexos: list[Anexo] = field(default_factory=list)


# ── Capacidades por canal + degradação ───────────────────────────────────


@dataclass(frozen=True)
class Capabilities:
    canal: str
    max_buttons: int                 # WhatsApp: 3 reply buttons (20 chars)
    max_list_items: int              # WhatsApp: list message com 10 itens (24 chars)
    supports_list: bool
    supports_free_proactive: bool    # WhatsApp: só na janela de 24h; fora dela, template
    max_texto: int = 4096            # corpo interativo no WhatsApp é 1024 — renderer trunca
    supports_media: bool = True      # Telegram e WhatsApp enviam imagem/arquivo


TELEGRAM_CAPS = Capabilities(
    canal="telegram", max_buttons=8, max_list_items=0,
    supports_list=False, supports_free_proactive=True,
)

WHATSAPP_CAPS = Capabilities(
    canal="whatsapp", max_buttons=3, max_list_items=10,
    supports_list=True, supports_free_proactive=False, max_texto=1024,
)


class RenderMode(str, Enum):
    BUTTONS = "buttons"
    LIST = "list"
    NUMBERED_TEXT = "numbered_text"  # fallback universal: "responda com o número"
    PLAIN = "plain"


def plan_rendering(interacao: Interaction, caps: Capabilities) -> RenderMode:
    """Regra de degradação (pura, testada): botões → lista → texto numerado."""
    if interacao is None:
        return RenderMode.PLAIN
    if isinstance(interacao, Confirmation):
        return RenderMode.BUTTONS if caps.max_buttons >= 2 else RenderMode.NUMBERED_TEXT
    n = len(interacao.opcoes)
    if n <= caps.max_buttons:
        return RenderMode.BUTTONS
    if caps.supports_list and n <= caps.max_list_items:
        return RenderMode.LIST
    return RenderMode.NUMBERED_TEXT


def render_numbered_text(texto: str, interacao: Choice) -> str:
    """Fallback texto-primeiro: funciona em qualquer canal, com qualquer usuário."""
    linhas = [texto, ""]
    linhas += [f"{i}. {op.rotulo}" for i, op in enumerate(interacao.opcoes, start=1)]
    linhas.append("\nResponda com o número da opção.")
    return "\n".join(linhas)


# ── Interface dos adapters ───────────────────────────────────────────────


class ChannelAdapter(Protocol):
    caps: Capabilities

    async def enviar(self, member_id: int, msg: OutboundMessage) -> None: ...

    async def notificar(self, member_id: int, texto: str) -> None:
        """Mensagem proativa (lembrete, briefing). No WhatsApp o adapter decide
        free-form (janela de 24h aberta) vs. template aprovado (janela fechada)."""
        ...
