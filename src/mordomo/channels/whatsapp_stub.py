"""Adapter WhatsApp — esqueleto para a Fase 3 (NÃO usado no MVP).

Plano (doc do projeto `mordomo-familia-arquitetura-e-possibilidades-v2.md`,
seções 4.2–4.5):

  1. Cloud API oficial direto na Meta (sem BSP). Número de TESTE grátis
     conversa com ~5 números cadastrados — cabe a família. NUNCA Evolution
     API/Baileys no número pessoal (risco real de ban permanente).
  2. Biblioteca: `pywa` (webhook + assinatura + botões/listas/templates +
     mídia prontos, integra com FastAPI). Descomente o extra `whatsapp` no
     pyproject.toml e rode `uv sync`.
  3. Checklist de entrada (seção 4.4 do doc): GET de verificação; validar
     X-Hub-Signature-256; responder 200 em <5s e processar via fila; dedupe
     por `wamid` (retries por até 7 dias!); ordenar pelo timestamp do payload;
     mark-as-read + typing indicator; statuses (sent/delivered/read/failed)
     viram eventos de analytics; templates utility aprovados ANTES de precisar
     (nomeie com versão: `lembrete_v1`); opt-in documentado.
  4. `notificar()` aqui decide: janela de 24h aberta → free-form; fechada →
     template. É a única diferença semântica para o núcleo.

O contrato (InboundMessage/OutboundMessage) já cobre tudo isso — este arquivo
é só o "encaixe". Os limites do canal já estão em WHATSAPP_CAPS (3 botões /
20 chars; lista 10 itens / 24 chars; corpo interativo 1024).
"""

from .contract import WHATSAPP_CAPS, Capabilities, OutboundMessage


class WhatsAppAdapter:
    caps: Capabilities = WHATSAPP_CAPS

    def __init__(self) -> None:  # pragma: no cover - fase 3
        raise NotImplementedError(
            "Fase 3 — instale o extra `whatsapp` (pywa/FastAPI) e implemente "
            "seguindo o checklist no docstring deste módulo."
        )

    async def enviar(self, member_id: int, msg: OutboundMessage) -> None:  # pragma: no cover
        raise NotImplementedError

    async def notificar(self, member_id: int, texto: str) -> None:  # pragma: no cover
        raise NotImplementedError
