"""Criar compromisso é DOIS passos: preparar e confirmar.

Conversa real de 17/08/2026, "Reunião Xiao Long Mo — Negócios Brasil x China":
o Mordomo criou o evento sem confirmação consistente, prometeu convidar
moxiaolongshifu@gmail.com (o evento nasceu com ZERO attendees), prometeu Google
Meet (nasceu sem conferenceData) e, quando o usuário confirmou, o LLM reescreveu
"24/08 às 9h" como "segunda, 24/08, das 9h" — expressão que o parser não
entendia, já DEPOIS de a data ter sido acertada.

A trava desta fatia é estrutural: o que o usuário confirma fica GRAVADO em
valores estruturados (início/fim em UTC, convidados, Meet, destino). A
confirmação executa esses valores — nunca relê a frase.

Todo o Google é falso aqui (httpx.MockTransport): sem rede, sem chave (regra
nº 7)."""

import json
from datetime import UTC, datetime, timedelta

import httpx
from sqlalchemy import select

from apoio import (
    cfg_de,
    criar_membro,
    google_configurado,
    gravador,
    injetar_google,
    membro_conectado,
)
from mordomo import propostas
from mordomo.db.models import EventProposal, FamilyEvent, ProductEvent
from mordomo.db.session import Sessao
from mordomo.integracoes import google
from mordomo.integracoes.google_api import GoogleAPI
from mordomo.tools.agenda import confirmar_evento, descartar_evento, preparar_evento

TITULO = "Reunião Xiao Long Mo - Negócios Brasil x China"
CONVIDADO = "moxiaolongshifu@gmail.com"
LINK = "https://calendar.google.com/event?eid=xyz"


# ── andaimes ─────────────────────────────────────────────────────────────


def _amanha(hora: int) -> str:
    return f"amanhã às {hora}h"


async def _preparar(membro, turn: str, **campos) -> str:
    argumentos = {
        "titulo": TITULO,
        "quando": _amanha(9),
        "ate": None,
        "local": None,
        "convidados": None,
        "com_meet": False,
    }
    argumentos.update(campos)
    return await preparar_evento.ainvoke(argumentos, cfg_de(membro, turn))


async def _confirmar(membro, turn: str, codigo: str | None = None) -> str:
    return await confirmar_evento.ainvoke({"codigo": codigo}, cfg_de(membro, turn))


def _posts(handler) -> list[httpx.Request]:
    return [c for c in handler.chamadas if c.method == "POST"]


def _corpo(handler, indice: int = 0) -> dict:
    return json.loads(_posts(handler)[indice].content)


async def _propostas(member_id: int) -> list[EventProposal]:
    async with Sessao() as s:
        res = await s.execute(
            select(EventProposal).where(EventProposal.member_id == member_id)
        )
        return list(res.scalars())


async def _eventos_nativos(member_id: int) -> list[FamilyEvent]:
    async with Sessao() as s:
        res = await s.execute(select(FamilyEvent).where(FamilyEvent.criado_por == member_id))
        return list(res.scalars())


async def _payloads(turn: str) -> str:
    async with Sessao() as s:
        res = await s.execute(select(ProductEvent).where(ProductEvent.turn_id == turn))
        return repr([e.payload for e in res.scalars()])


# ── preparar NÃO cria ────────────────────────────────────────────────────


async def test_primeira_fala_completa_nao_cria_nada_e_pede_confirmacao(monkeypatch):
    """Mesmo com título, dia e hora na mesma frase: gravar antes do "sim" foi o
    que fez um evento aparecer na agenda de alguém sem que ninguém confirmasse."""
    with google_configurado():
        membro = await membro_conectado("ConfirmaPreparaSoPrepara")
        handler = gravador()
        injetar_google(monkeypatch, handler)
        resposta = await _preparar(membro, "t-cf-1", ate=_amanha(10))

    assert _posts(handler) == [], "nenhuma criação antes do sim"
    assert await _eventos_nativos(membro.id) == []
    assert len(await _propostas(membro.id)) == 1
    assert "confirm" in resposta.lower()


async def test_preparacao_mostra_destino_data_horas_local_convidado_e_meet(monkeypatch):
    with google_configurado():
        membro = await membro_conectado("ConfirmaResumo")
        injetar_google(monkeypatch, gravador())
        resposta = await _preparar(
            membro,
            "t-cf-2",
            ate=_amanha(10),
            local="Av. Paulista 1000",
            convidados=[CONVIDADO],
            com_meet=True,
        )

    amanha = (datetime.now(UTC) + timedelta(days=1)).astimezone().strftime("%d/%m")
    assert "Google Agenda" in resposta
    assert amanha in resposta
    assert "09:00" in resposta and "10:00" in resposta
    assert "Av. Paulista 1000" in resposta
    assert CONVIDADO in resposta
    assert "meet" in resposta.lower()


async def test_preparacao_recusa_email_invalido_sem_gravar_proposta(monkeypatch):
    """Convidado é e-mail — "o Xiao" não é endereço, e mandar isso ao Google
    derrubaria a criação inteira depois do sim."""
    with google_configurado():
        membro = await membro_conectado("ConfirmaEmailInvalido")
        handler = gravador()
        injetar_google(monkeypatch, handler)
        resposta = await _preparar(membro, "t-cf-3", convidados=["o Xiao"])

    assert _posts(handler) == []
    assert await _propostas(membro.id) == []
    assert "e-mail" in resposta.lower()


# ── confirmar executa os valores GRAVADOS ────────────────────────────────


async def test_confirmacao_faz_um_unico_post_com_os_valores_estruturados(monkeypatch):
    with google_configurado():
        membro = await membro_conectado("ConfirmaExecuta")
        handler = gravador()
        injetar_google(monkeypatch, handler)
        await _preparar(membro, "t-cf-4", ate=_amanha(10))
        resposta = await _confirmar(membro, "t-cf-4b")

    assert len(_posts(handler)) == 1
    corpo = _corpo(handler)
    assert corpo["summary"] == TITULO
    inicio = datetime.fromisoformat(corpo["start"]["dateTime"])
    fim = datetime.fromisoformat(corpo["end"]["dateTime"])
    assert (inicio.hour, fim.hour) == (9, 10)
    assert "Google Agenda" in resposta


async def test_confirmacao_nao_reparseia_a_frase(monkeypatch):
    """O defeito real: depois do sim, o LLM remontou a expressão ("segunda,
    24/08, das 9h") e o parser falhou — com a data JÁ acertada. A confirmação
    não pode nem tocar no parser."""
    with google_configurado():
        membro = await membro_conectado("ConfirmaSemReparse")
        handler = gravador()
        injetar_google(monkeypatch, handler)
        await _preparar(membro, "t-cf-5", ate=_amanha(10))

        def explodir(*args, **kwargs):
            raise AssertionError("a confirmação releu a frase em vez do que foi gravado")

        monkeypatch.setattr("mordomo.tools.agenda.resolver_data", explodir)
        monkeypatch.setattr("mordomo.tools._comum.resolver_data", explodir)
        resposta = await _confirmar(membro, "t-cf-5b")

    assert len(_posts(handler)) == 1
    assert "Google Agenda" in resposta


async def test_confirmacao_repetida_nao_duplica(monkeypatch):
    with google_configurado():
        membro = await membro_conectado("ConfirmaRetry")
        handler = gravador()
        injetar_google(monkeypatch, handler)
        await _preparar(membro, "t-cf-6", ate=_amanha(10))
        primeira = await _confirmar(membro, "t-cf-6b")
        segunda = await _confirmar(membro, "t-cf-6b")

    assert len(_posts(handler)) == 1, "o segundo sim não pode virar outro evento"
    assert "Google Agenda" in primeira
    assert "dupl" in segunda.lower() or "já" in segunda.lower()


async def test_confirmacao_em_andamento_nao_afirma_que_ja_criou(monkeypatch):
    """`usado_em` é adquirido ANTES da chamada externa. Enquanto o primeiro
    trabalhador ainda está no Google, o segundo não pode transformar a trava de
    concorrência em uma confirmação falsa de criação."""
    with google_configurado():
        membro = await membro_conectado("ConfirmaEmAndamento")
        handler = gravador()
        injetar_google(monkeypatch, handler)
        await _preparar(membro, "t-cf-andamento", ate=_amanha(10))
        proposta, motivo = await propostas.reivindicar(membro.id)
        assert proposta is not None and motivo == "ok"

        resposta = await _confirmar(membro, "t-cf-andamento-b")

    assert _posts(handler) == []
    assert "já criei" not in resposta.lower()
    assert "process" in resposta.lower() or "andamento" in resposta.lower()


async def test_409_nao_inventa_que_convidados_foram_aceitos(monkeypatch):
    """409 prova apenas que o ID já existe; não traz o corpo do evento. Logo não
    prova que attendees ou Meet foram registrados."""
    with google_configurado():
        membro = await membro_conectado("Confirma409SemProva")
        handler = gravador([lambda r: httpx.Response(409, json={"error": "duplicate"})])
        api = GoogleAPI(cliente=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
        inicio = datetime.now(UTC) + timedelta(days=1)
        try:
            resultado = await google.criar_evento_na_agenda(
                membro.id,
                titulo=TITULO,
                inicio=inicio,
                fim=inicio + timedelta(hours=1),
                convidados=[CONVIDADO],
                com_meet=True,
                chave="proposta-409",
                api=api,
            )
        finally:
            await api.fechar()

    assert resultado["ok"] is True and resultado["novo"] is False
    assert resultado["convidados_aceitos"] == []
    assert resultado["meet_link"] is None


async def test_confirmacao_sem_nada_pendente_falha_sem_post(monkeypatch):
    with google_configurado():
        membro = await membro_conectado("ConfirmaAusente")
        handler = gravador()
        injetar_google(monkeypatch, handler)
        resposta = await _confirmar(membro, "t-cf-7")

    assert _posts(handler) == []
    assert "não" in resposta.lower()


async def test_confirmacao_expirada_falha_sem_post(monkeypatch):
    with google_configurado():
        membro = await membro_conectado("ConfirmaExpirada")
        handler = gravador()
        injetar_google(monkeypatch, handler)
        await _preparar(membro, "t-cf-8", ate=_amanha(10))
        async with Sessao() as s:
            proposta = (await s.execute(
                select(EventProposal).where(EventProposal.member_id == membro.id)
            )).scalar_one()
            proposta.expira_em = datetime.now(UTC) - timedelta(minutes=1)
            await s.commit()
        resposta = await _confirmar(membro, "t-cf-8b")

    assert _posts(handler) == []
    assert await _eventos_nativos(membro.id) == []
    assert "de novo" in resposta.lower() or "expir" in resposta.lower()


async def test_confirmacao_de_outro_membro_nao_cria_o_evento_alheio(monkeypatch):
    """Thread = membro (ADR-003): o pendente de um não pode ser executado por
    outro, nem quando ele repete o código."""
    with google_configurado():
        dono = await membro_conectado("ConfirmaDono")
        intruso = await membro_conectado("ConfirmaIntruso")
        handler = gravador()
        injetar_google(monkeypatch, handler)
        await _preparar(dono, "t-cf-9", ate=_amanha(10))
        codigo = (await _propostas(dono.id))[0].codigo
        sem_codigo = await _confirmar(intruso, "t-cf-9b")
        com_codigo = await _confirmar(intruso, "t-cf-9c", codigo=codigo)

    assert _posts(handler) == []
    assert "não" in sem_codigo.lower() and "não" in com_codigo.lower()
    assert (await _propostas(dono.id))[0].usado_em is None, "o pendente do dono segue de pé"


async def test_duas_preparacoes_tornam_a_confirmacao_ambigua(monkeypatch):
    """Dois pendentes e um "sim" solto: executar o mais recente seria adivinhar
    qual compromisso a pessoa quis."""
    with google_configurado():
        membro = await membro_conectado("ConfirmaAmbigua")
        handler = gravador()
        injetar_google(monkeypatch, handler)
        await _preparar(membro, "t-cf-10", quando=_amanha(9), ate=_amanha(10))
        await _preparar(membro, "t-cf-10b", quando=_amanha(15), ate=_amanha(16))
        resposta = await _confirmar(membro, "t-cf-10c")

    assert _posts(handler) == []
    assert "qual" in resposta.lower()


async def test_descartar_apaga_o_pendente_sem_criar(monkeypatch):
    with google_configurado():
        membro = await membro_conectado("ConfirmaDescarta")
        handler = gravador()
        injetar_google(monkeypatch, handler)
        await _preparar(membro, "t-cf-11", ate=_amanha(10))
        descarte = await descartar_evento.ainvoke({}, cfg_de(membro, "t-cf-11b"))
        depois = await _confirmar(membro, "t-cf-11c")

    assert _posts(handler) == []
    assert await _propostas(membro.id) == []
    assert "descart" in descarte.lower() or "esque" in descarte.lower()
    assert "não" in depois.lower()


# ── sem Google: destino explícito e confirmação também ───────────────────


async def test_sem_google_tambem_confirma_antes_de_gravar(monkeypatch):
    with google_configurado():
        membro = await criar_membro("ConfirmaNativa")
        handler = gravador()
        injetar_google(monkeypatch, handler)
        preparo = await _preparar(membro, "t-cf-12", ate=_amanha(10))
        assert await _eventos_nativos(membro.id) == [], "nada gravado antes do sim"
        assert "agenda compartilhada do Mordomo" in preparo

        criada = await _confirmar(membro, "t-cf-12b")

    assert handler.chamadas == [], "sem conexão não se fala com o Google"
    eventos = await _eventos_nativos(membro.id)
    assert len(eventos) == 1 and eventos[0].fim_utc is not None
    assert "agenda compartilhada do Mordomo" in criada


async def test_sem_google_avisa_que_nao_convida_nem_cria_meet(monkeypatch):
    """Prometer convite numa agenda que não tem convidados é a mesma mentira do
    incidente, só que noutro destino."""
    with google_configurado():
        membro = await criar_membro("ConfirmaNativaSemConvite")
        injetar_google(monkeypatch, gravador())
        resposta = await _preparar(
            membro, "t-cf-13", convidados=[CONVIDADO], com_meet=True
        )

    assert "não" in resposta.lower()
    assert "convite" in resposta.lower() or "convidar" in resposta.lower()


# ── privacidade (ADR-005) ────────────────────────────────────────────────


async def test_analytics_da_confirmacao_nao_leva_titulo_email_link_nem_token(monkeypatch):
    with google_configurado():
        membro = await membro_conectado("ConfirmaPrivacidade")
        injetar_google(monkeypatch, gravador())
        await _preparar(
            membro,
            "t-cf-14",
            ate=_amanha(10),
            local="Av. Paulista 1000",
            convidados=[CONVIDADO],
            com_meet=True,
        )
        await _confirmar(membro, "t-cf-14b")

    bruto = await _payloads("t-cf-14") + await _payloads("t-cf-14b")
    for proibido in (TITULO, CONVIDADO, "Av. Paulista 1000", LINK, "access-valido"):
        assert proibido not in bruto
