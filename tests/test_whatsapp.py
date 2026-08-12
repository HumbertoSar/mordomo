"""Canal WhatsApp (fase 3) — tudo o que dá para provar SEM rede e SEM Meta.

O adapter foi escrito direto sobre a Cloud API (ADR-009) justamente para que o
miolo — assinatura, parsing, dedupe, ordem, renderer, janela de 24h — fosse
lógica pura, testável antes de existir número, token ou webhook. O que estes
testes cobrem é o que só apareceria em produção depois de um erro caro:

  - webhook reentregue pela Meta (retry de até 7 dias) criando lembrete duplo
  - mensagens fora de ordem invertendo o sentido da conversa
  - proativo fora da janela de 24h sendo recusado pela Meta
  - texto acima de 1024 chars sendo REJEITADO inteiro (não truncado)
"""

import asyncio
import hashlib
import hmac
import json
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from langchain_core.messages import AIMessage
from sqlalchemy import select

from apoio import criar_membro
from mordomo import notify
from mordomo.channels import whatsapp as wa
from mordomo.channels.contract import (
    Choice,
    ChoiceOption,
    Confirmation,
    OutboundMessage,
)
from mordomo.channels.whatsapp_api import cortar, so_digitos
from mordomo.config import settings
from mordomo.db.models import ChannelIdentity, ProductEvent
from mordomo.db.session import Sessao

SEGREDO = "segredo-do-app"


# ── Duplos ───────────────────────────────────────────────────────────────


class ApiFalsa:
    """Registra o que teria ido para a Meta. Mesma superfície do WhatsAppAPI."""

    def __init__(self) -> None:
        self.textos: list[tuple[str, str]] = []
        self.botoes: list[tuple[str, str, list]] = []
        self.listas: list[tuple[str, str, list]] = []
        self.templates: list[tuple[str, str, list | None]] = []
        self.lidos: list[str] = []
        self.midias: list[tuple[str, str]] = []

    async def enviar_texto(self, para, texto):
        self.textos.append((para, texto))
        return f"wamid.{len(self.textos)}"

    async def enviar_botoes(self, para, corpo, botoes):
        self.botoes.append((para, corpo, botoes))
        return "wamid.b"

    async def enviar_lista(self, para, corpo, opcoes, rotulo_botao="Escolher"):
        self.listas.append((para, corpo, opcoes))
        return "wamid.l"

    async def enviar_template(self, para, nome, idioma="pt_BR", parametros=None):
        self.templates.append((para, nome, parametros))
        return "wamid.t"

    async def enviar_midia(self, para, media_id, mime, legenda="", nome=""):
        self.midias.append((para, media_id))
        return "wamid.m"

    async def subir_midia(self, dados, mime, nome="arquivo"):
        return "media-123"

    async def baixar_midia(self, media_id):
        return b"bytes", "audio/ogg"

    async def marcar_lido(self, wamid, digitando=True):
        self.lidos.append(wamid)

    async def fechar(self):
        ...


class GrafoOk:
    def __init__(self, resposta: str = "Anotado!") -> None:
        self.resposta = resposta
        self.chamadas = 0
        self.textos: list[str] = []

    async def ainvoke(self, entrada, cfg):
        self.chamadas += 1
        self.textos.append(entrada["messages"][-1].content)
        return {"messages": [AIMessage(self.resposta)]}


@pytest.fixture
def adapter(monkeypatch):
    """Adapter isolado: sem rede, sem registro global de canal, sem espera."""
    monkeypatch.setattr(notify, "_adapters", {})
    # Debounce curto, mas MAIOR que o tempo de um processar() (que escreve no
    # banco 3 vezes) — senão o teste do picotado mede o relógio, não a regra.
    monkeypatch.setattr(settings, "debounce_segundos", 0.3)
    api = ApiFalsa()
    a = wa.WhatsAppAdapter(GrafoOk(), api=api)
    return a


async def membro_wa(nome: str) -> tuple:
    """Membro com identidade whatsapp. wa_id único por execução (o banco de
    teste é compartilhado pela sessão)."""
    wa_id = "5521" + uuid4().int.__str__()[:9]
    membro = await criar_membro(nome)
    async with Sessao() as s:
        s.add(ChannelIdentity(member_id=membro.id, canal="whatsapp", external_id=wa_id))
        await s.commit()
    return membro, wa_id


def payload_texto(wa_id: str, texto: str, wamid: str, ts: int = 1786000000) -> dict:
    return {
        "object": "whatsapp_business_account",
        "entry": [{
            "id": "WABA",
            "changes": [{
                "field": "messages",
                "value": {
                    "messaging_product": "whatsapp",
                    "metadata": {"phone_number_id": "123"},
                    "contacts": [{"profile": {"name": "Fulano"}, "wa_id": wa_id}],
                    "messages": [{
                        "from": wa_id, "id": wamid, "timestamp": str(ts),
                        "type": "text", "text": {"body": texto},
                    }],
                },
            }],
        }],
    }


# ── Assinatura ───────────────────────────────────────────────────────────


def _assinar(corpo: bytes) -> str:
    return "sha256=" + hmac.new(SEGREDO.encode(), corpo, hashlib.sha256).hexdigest()


def test_assinatura_valida_aceita_e_qualquer_desvio_recusa():
    corpo = json.dumps({"entry": []}).encode()
    assert wa.assinatura_valida(corpo, _assinar(corpo), SEGREDO) is True
    # corpo adulterado com a MESMA assinatura: é o ataque que isto barra
    assert wa.assinatura_valida(corpo + b" ", _assinar(corpo), SEGREDO) is False
    assert wa.assinatura_valida(corpo, _assinar(corpo), "outro-segredo") is False
    assert wa.assinatura_valida(corpo, None, SEGREDO) is False           # sem cabeçalho
    assert wa.assinatura_valida(corpo, _assinar(corpo), "") is False     # sem segredo configurado


# ── Parsing ──────────────────────────────────────────────────────────────


def test_parse_extrai_texto_e_ordena_por_timestamp():
    """A Meta não promete ordem: "esquece" antes de "compra pão" inverteria o
    sentido da conversa."""
    payload = {"entry": [{"changes": [{"value": {"messages": [
        {"from": "5521999", "id": "w2", "timestamp": "200", "type": "text",
         "text": {"body": "segunda"}},
        {"from": "5521999", "id": "w1", "timestamp": "100", "type": "text",
         "text": {"body": "primeira"}},
    ]}}]}]}
    lote = wa.parse_webhook(payload)
    assert [e.texto for e in lote.entradas] == ["primeira", "segunda"]
    assert lote.entradas[0].timestamp == datetime.fromtimestamp(100, UTC)


def test_parse_resposta_de_botao_vira_o_id_que_o_nucleo_mandou():
    payload = {"entry": [{"changes": [{"value": {"messages": [{
        "from": "5521999", "id": "w1", "timestamp": "1", "type": "interactive",
        "interactive": {"type": "button_reply", "button_reply": {"id": "sim", "title": "Sim"}},
    }]}}]}]}
    assert wa.parse_webhook(payload).entradas[0].texto == "sim"


def test_parse_statuses_traz_erro_e_categoria_de_cobranca():
    payload = {"entry": [{"changes": [{"value": {"statuses": [{
        "id": "wamid.X", "status": "failed", "timestamp": "5", "recipient_id": "5521999",
        "errors": [{"code": 131047}], "pricing": {"category": "utility"},
    }]}}]}]}
    status = wa.parse_webhook(payload).statuses[0]
    assert (status.status, status.erro, status.categoria) == ("failed", "131047", "utility")


def test_parse_de_payload_vazio_nao_explode():
    assert wa.parse_webhook({}).entradas == []
    assert wa.parse_webhook({"entry": [{"changes": [{"value": {}}]}]}).statuses == []


# ── Identidade (nono dígito) ─────────────────────────────────────────────


def test_variantes_cobrem_o_nono_digito_brasileiro():
    assert wa.variantes_wa_id("5521987654321") == ["5521987654321", "552187654321"]
    assert wa.variantes_wa_id("552187654321") == ["552187654321", "5521987654321"]
    # fixo (não começa com 6-9) e número estrangeiro não ganham variante
    assert wa.variantes_wa_id("552133334444") == ["552133334444"]
    assert wa.variantes_wa_id("+1 415 555 2671") == ["14155552671"]


def test_so_digitos_e_cortar():
    assert so_digitos("+55 (21) 99999-8888") == "5521999998888"
    assert cortar("Confirmar mesmo assim, por favor", 20) == "Confirmar mesmo ass…"
    assert cortar("curto", 20) == "curto"


# ── Dedupe (o retry de 7 dias da Meta) ───────────────────────────────────


async def test_mesma_mensagem_so_e_registrada_uma_vez():
    wamid = f"wamid.{uuid4().hex}"
    assert await wa.registrar_entrada("whatsapp", wamid) is True
    assert await wa.registrar_entrada("whatsapp", wamid) is False


async def test_reentrega_da_meta_nao_roda_o_grafo_de_novo(adapter):
    _, wa_id = await membro_wa("WaDedupe")
    payload = payload_texto(wa_id, "me lembra de pagar o boleto", f"wamid.{uuid4().hex}")

    await adapter.processar(payload)
    await adapter.processar(payload)          # a Meta insistindo
    await asyncio.sleep(0.5)                  # deixa o debounce fechar

    assert adapter.grafo.chamadas == 1        # UM lembrete, não dois
    async with Sessao() as s:
        res = await s.execute(
            select(ProductEvent).where(ProductEvent.tipo == "message_duplicated")
        )
        assert res.scalars().first() is not None


# ── Fluxo de entrada ─────────────────────────────────────────────────────


async def test_texto_vira_turno_e_a_resposta_sai_pelo_canal(adapter):
    membro, wa_id = await membro_wa("WaFluxo")
    await adapter.processar(payload_texto(wa_id, "o que temos sábado?", f"wamid.{uuid4().hex}"))
    await asyncio.sleep(0.5)

    assert adapter.grafo.textos == ["o que temos sábado?"]
    assert adapter.api.textos[-1] == (wa_id, "Anotado!")
    assert adapter.api.lidos                      # marcou como lida (dois checks azuis)

    async with Sessao() as s:
        res = await s.execute(
            select(ProductEvent).where(
                ProductEvent.tipo == "message_sent", ProductEvent.member_id == membro.id
            )
        )
        evento = res.scalars().first()
    assert evento is not None
    assert evento.payload["canal"] == "whatsapp"
    assert evento.turn_id is not None             # regra nº 4: nada de órfão


async def test_mensagens_picotadas_viram_um_turno_so(adapter):
    """A família manda "me lembra", "amanhã", "do dentista" em 3 mensagens."""
    _, wa_id = await membro_wa("WaDebounce")
    for parte in ("me lembra", "amanhã", "do dentista"):
        await adapter.processar(payload_texto(wa_id, parte, f"wamid.{uuid4().hex}"))
    await asyncio.sleep(0.5)

    assert adapter.grafo.chamadas == 1
    assert adapter.grafo.textos == ["me lembra\namanhã\ndo dentista"]


async def test_desconhecido_recebe_recusa_educada_e_nao_acorda_o_grafo(adapter):
    await adapter.processar(payload_texto("5511900000001", "oi", f"wamid.{uuid4().hex}"))
    await asyncio.sleep(0.05)
    assert adapter.grafo.chamadas == 0
    assert "vincular" in adapter.api.textos[-1][1].lower()


async def test_entrada_abre_a_janela_de_24h(adapter):
    membro, wa_id = await membro_wa("WaJanela")
    await adapter.processar(payload_texto(wa_id, "oi", f"wamid.{uuid4().hex}"))
    await asyncio.sleep(0.05)
    identidade = await wa.identidade_wa(membro.id)
    assert identidade.ultima_entrada_em is not None


async def test_status_vira_evento_de_analytics(adapter):
    membro, wa_id = await membro_wa("WaStatus")
    await adapter.processar({"entry": [{"changes": [{"value": {"statuses": [{
        "id": "wamid.Z", "status": "read", "timestamp": "9", "recipient_id": wa_id,
    }]}}]}]})
    async with Sessao() as s:
        res = await s.execute(
            select(ProductEvent).where(
                ProductEvent.tipo == "message_status", ProductEvent.member_id == membro.id
            )
        )
        evento = res.scalars().first()
    assert evento is not None and evento.payload["status"] == "read"


async def test_tipo_nao_suportado_recebe_recusa_simpatica(adapter):
    _, wa_id = await membro_wa("WaSticker")
    payload = payload_texto(wa_id, "", f"wamid.{uuid4().hex}")
    payload["entry"][0]["changes"][0]["value"]["messages"][0]["type"] = "sticker"
    await adapter.processar(payload)
    await asyncio.sleep(0.05)
    assert "ainda não sei ler" in adapter.api.textos[-1][1]


# ── Renderização (contrato → WhatsApp) ───────────────────────────────────


async def test_confirmacao_vira_dois_botoes(adapter):
    _, wa_id = await membro_wa("WaBotao")
    await adapter._enviar_wa(wa_id, OutboundMessage(texto="Envio?", interacao=Confirmation()))
    _, corpo, botoes = adapter.api.botoes[-1]
    assert corpo == "Envio?"
    assert [b[0] for b in botoes] == ["sim", "nao"]


async def test_quatro_opcoes_degradam_para_lista(adapter):
    """3 botões é o teto do WhatsApp; a 4ª opção tem que virar list message."""
    _, wa_id = await membro_wa("WaLista")
    opcoes = [ChoiceOption(id=f"o{i}", rotulo=f"Opção {i}") for i in range(4)]
    await adapter._enviar_wa(wa_id, OutboundMessage(texto="Qual?", interacao=Choice(opcoes)))
    assert adapter.api.botoes == []
    assert len(adapter.api.listas[-1][2]) == 4


async def test_onze_opcoes_degradam_para_texto_numerado(adapter):
    """Acima de 10 itens nem lista serve — o fallback universal é texto."""
    _, wa_id = await membro_wa("WaNumerado")
    opcoes = [ChoiceOption(id=f"o{i}", rotulo=f"Opção {i}") for i in range(11)]
    await adapter._enviar_wa(wa_id, OutboundMessage(texto="Qual?", interacao=Choice(opcoes)))
    assert adapter.api.listas == [] and adapter.api.botoes == []
    assert "Responda com o número" in adapter.api.textos[-1][1]


async def test_texto_longo_e_fatiado_e_nao_rejeitado(adapter):
    """Acima do limite a API recusa a mensagem INTEIRA — sem fatiar, um
    listar_lembretes longo simplesmente não chega."""
    _, wa_id = await membro_wa("WaLongo")
    texto = "\n".join(f"linha {i} do relatório de lembretes" for i in range(80))
    await adapter._enviar_wa(wa_id, OutboundMessage(texto=texto))
    assert len(adapter.api.textos) > 1
    assert all(len(t) <= 1024 for _, t in adapter.api.textos)


# ── Proatividade: janela de 24h vs. template ─────────────────────────────


def test_janela_aberta_so_conta_do_ultimo_contato():
    agora = datetime.now(UTC)
    assert wa.janela_aberta(agora - timedelta(hours=2)) is True
    assert wa.janela_aberta(agora - timedelta(hours=25)) is False
    assert wa.janela_aberta(None) is False      # nunca falou = janela fechada


async def test_proativo_dentro_da_janela_vai_como_texto_livre(adapter):
    membro, wa_id = await membro_wa("WaProativoDentro")
    await wa.marcar_entrada("whatsapp", wa_id, datetime.now(UTC))
    await adapter.notificar(membro.id, "⏰ Lembrete: remédio do Davi")
    assert adapter.api.textos[-1][0] == wa_id
    assert adapter.api.templates == []


async def test_proativo_fora_da_janela_vai_como_template_aprovado(adapter):
    membro, wa_id = await membro_wa("WaProativoFora")
    await wa.marcar_entrada("whatsapp", wa_id, datetime.now(UTC) - timedelta(hours=30))
    await adapter.notificar(membro.id, "⏰ Lembrete:\nremédio do Davi")
    assert adapter.api.textos == []
    para, nome, parametros = adapter.api.templates[-1]
    assert (para, nome) == (wa_id, settings.whatsapp_template_lembrete)
    # parâmetro de template não aceita quebra de linha — a Meta recusa
    assert "\n" not in parametros[0]


async def test_sem_template_configurado_o_proativo_e_engolido_com_aviso(adapter, monkeypatch):
    monkeypatch.setattr(settings, "whatsapp_template_lembrete", "")
    membro, wa_id = await membro_wa("WaProativoSemTemplate")
    await wa.marcar_entrada("whatsapp", wa_id, datetime.now(UTC) - timedelta(hours=30))
    await adapter.notificar(membro.id, "lembrete")
    assert adapter.api.templates == [] and adapter.api.textos == []


# ── Preferência de canal na migração ─────────────────────────────────────


def test_whatsapp_ganha_do_telegram_quando_o_membro_tem_os_dois(monkeypatch):
    class _Falso:
        def __init__(self, canal):
            self.caps = type("C", (), {"canal": canal})()

    monkeypatch.setattr(
        notify, "_adapters", {"telegram": _Falso("telegram"), "whatsapp": _Falso("whatsapp")}
    )
    monkeypatch.setattr(settings, "canal_preferido", "whatsapp")
    assert notify.canais_por_preferencia()[0] == "whatsapp"
    monkeypatch.setattr(settings, "canal_preferido", "telegram")
    assert notify.canais_por_preferencia()[0] == "telegram"


# ── Webhook (só roda com o extra `whatsapp` instalado) ───────────────────


def test_webhook_verifica_assina_e_enfileira(monkeypatch):
    pytest.importorskip("fastapi", reason="extra `whatsapp` não instalado")
    from fastapi.testclient import TestClient

    from mordomo.channels.whatsapp_webhook import ROTA, criar_app

    monkeypatch.setattr(settings, "whatsapp_verify_token", "verifica-me")
    monkeypatch.setattr(settings, "whatsapp_app_secret", SEGREDO)

    class _AdapterFila:
        def __init__(self):
            self.fila = asyncio.Queue()

        def enfileirar(self, payload):
            self.fila.put_nowait(payload)

    alvo = _AdapterFila()
    cliente = TestClient(criar_app(alvo))

    ok = cliente.get(ROTA, params={
        "hub.mode": "subscribe", "hub.verify_token": "verifica-me", "hub.challenge": "1234",
    })
    assert (ok.status_code, ok.text) == (200, "1234")
    assert cliente.get(ROTA, params={
        "hub.mode": "subscribe", "hub.verify_token": "errado", "hub.challenge": "1234",
    }).status_code == 403

    corpo = json.dumps(payload_texto("5521999", "oi", "wamid.1")).encode()
    assert cliente.post(
        ROTA, content=corpo, headers={"X-Hub-Signature-256": _assinar(corpo)}
    ).status_code == 200
    assert alvo.fila.qsize() == 1

    # sem assinatura válida não entra na fila — e a família não recebe estranho
    assert cliente.post(
        ROTA, content=corpo, headers={"X-Hub-Signature-256": "sha256=00"}
    ).status_code == 403
    assert alvo.fila.qsize() == 1
