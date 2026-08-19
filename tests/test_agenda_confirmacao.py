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
from zoneinfo import ZoneInfo

import httpx
import pytest
from langchain_core.messages import AIMessage, HumanMessage
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


async def test_reivindicacao_orfa_reexecuta_google_com_id_deterministico(monkeypatch):
    """Se o worker morreu depois do efeito externo, o retry precisa voltar ao
    Google com a mesma proposta; um 409/ID idempotente permite reconciliar."""
    with google_configurado():
        membro = await membro_conectado("ConfirmaReivindicacaoOrfa")
        handler = gravador()
        injetar_google(monkeypatch, handler)
        await _preparar(membro, "t-cf-lease", ate=_amanha(10))
        velha = datetime.now(UTC) - timedelta(minutes=10)
        tomada, motivo = await propostas.reivindicar(membro.id, agora=velha)
        assert tomada is not None and motivo == "ok"

        resposta = await _confirmar(membro, "t-cf-lease-b")

    assert len(_posts(handler)) == 1
    assert "process" not in resposta.lower()
    atual = await _proposta_unica(membro)
    assert atual.concluido_em is not None


async def test_worker_antigo_nao_devolve_lease_retomada():
    membro = await criar_membro("LeaseNaoDevolveNova")
    inicio = datetime.now(UTC) + timedelta(days=1)
    await propostas.guardar(
        membro.id,
        titulo="teste lease",
        inicio=inicio,
        fim=inicio + timedelta(hours=1),
        local=None,
        convidados=[],
        com_meet=False,
        destino="nativo",
    )
    velha = datetime.now(UTC) - timedelta(minutes=10)
    primeira, motivo = await propostas.reivindicar(membro.id, agora=velha)
    assert primeira is not None and motivo == "ok" and primeira.usado_em == velha
    nova, motivo = await propostas.reivindicar(membro.id, agora=datetime.now(UTC))
    assert nova is not None and motivo == "ok" and nova.usado_em != velha

    devolveu = await propostas.devolver(primeira.id, reivindicada_em=velha)

    assert devolveu is False
    atual = await _proposta_unica(membro)
    assert atual.usado_em == nova.usado_em


async def test_google_nao_anuncia_novo_se_outro_worker_ja_concluiu(monkeypatch):
    async def ja_concluida(*args, **kwargs):
        return False

    with google_configurado():
        membro = await membro_conectado("ConfirmaGoogleConclusaoPerdida")
        handler = gravador()
        injetar_google(monkeypatch, handler)
        await _preparar(membro, "t-cf-google-race", ate=_amanha(10))
        monkeypatch.setattr(propostas, "concluir", ja_concluida)

        resposta = await _confirmar(membro, "t-cf-google-race-b")

    assert len(_posts(handler)) == 1
    assert "já" in resposta.lower() and "evento criado" not in resposta.lower()
    assert "'novo': False" in await _payloads("t-cf-google-race-b")


async def test_concluir_exige_proposta_reivindicada_existente():
    membro = await criar_membro("ConcluirSemProposta")
    inicio = datetime.now(UTC) + timedelta(days=1)
    proposta = await propostas.guardar(
        membro.id,
        titulo="teste",
        inicio=inicio,
        fim=inicio + timedelta(hours=1),
        local=None,
        convidados=[],
        com_meet=False,
        destino="nativo",
        journey_id="j-concluir-ausente",
    )
    reivindicada, motivo = await propostas.reivindicar(membro.id)
    assert reivindicada is not None and motivo == "ok"
    async with Sessao() as s:
        await s.delete(await s.get(EventProposal, proposta.id))
        await s.commit()

    resolveu = ProductEvent(
        tipo="journey_resolved",
        journey_id="j-concluir-ausente",
        payload={"journey_type": "calendar_create"},
    )
    concluida = await propostas.concluir(proposta.id, link="nativo:1", eventos=[resolveu])

    assert concluida is False
    assert "journey_resolved" not in await _tipos_de_jornada("j-concluir-ausente")


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


async def test_descarte_e_um_delete_condicional_atomico(monkeypatch):
    """SELECT seguido de DELETE permite a confirmação reivindicar no intervalo.
    O contrato exige uma única escrita que devolva só o que realmente removeu."""
    comandos = []

    class Resultado:
        def scalars(self):
            return ["j-real"]

    class SessaoFalsa:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def execute(self, comando):
            comandos.append(comando)
            return Resultado()

        def add_all(self, eventos):
            pass

        async def commit(self):
            pass

    monkeypatch.setattr(propostas, "Sessao", SessaoFalsa)

    jornadas = await propostas.descartar(7, eventos_por_jornada=lambda _: [])

    assert jornadas == ["j-real"]
    assert len(comandos) == 1, "não pode haver janela SELECT→DELETE"
    sql = str(comandos[0]).lower()
    assert sql.lstrip().startswith("delete")
    assert "usado_em is null" in sql and "expira_em" in sql


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


async def test_criacao_nativa_reverte_evento_se_conclusao_da_proposta_falhar(monkeypatch):
    async def falhar(*args, **kwargs):
        raise RuntimeError("falha injetada antes da conclusão")

    with google_configurado():
        membro = await criar_membro("ConfirmaNativaAtomica")
        injetar_google(monkeypatch, gravador())
        await _preparar(membro, "t-cf-nativa-atomica", ate=_amanha(10))
        monkeypatch.setattr(propostas, "concluir_na_sessao", falhar)

        with pytest.raises(RuntimeError, match="falha injetada"):
            await _confirmar(membro, "t-cf-nativa-atomica-b")

    assert await _eventos_nativos(membro.id) == []
    proposta = await _proposta_unica(membro)
    assert proposta.concluido_em is None


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


# ── intervalos naturais (canário real de 18/08/2026) ─────────────────────
# O LLM fatiou "amanhã das 18h às 18h30" em quando="amanhã das 18h" +
# ate="18h30" e a preparação morria em "não entendi quando começa".


async def _proposta_unica(membro) -> EventProposal:
    todas = await _propostas(membro.id)
    assert len(todas) == 1
    return todas[0]


def _hora_local(quando: datetime) -> tuple[int, int]:
    local = quando.replace(tzinfo=UTC).astimezone(ZoneInfo("America/Sao_Paulo"))
    return local.hour, local.minute


async def test_fragmento_de_intervalo_prepara_com_comeco_e_fim(monkeypatch):
    with google_configurado():
        membro = await membro_conectado("ConfirmaFragmentoIntervalo")
        injetar_google(monkeypatch, gravador())
        resposta = await _preparar(
            membro, "t-cf-frag", quando="amanhã das 18h", ate="18h30"
        )

    proposta = await _proposta_unica(membro)
    assert _hora_local(proposta.inicio_utc) == (18, 0)
    assert _hora_local(proposta.fim_utc) == (18, 30)
    assert "18:00" in resposta and "18:30" in resposta


async def test_frase_inteira_de_intervalo_dispensa_o_ate(monkeypatch):
    """Quando o LLM NÃO fatia, o começo e o fim têm que sair da mesma frase —
    não da duração padrão de 1 hora."""
    with google_configurado():
        membro = await membro_conectado("ConfirmaFraseIntervalo")
        injetar_google(monkeypatch, gravador())
        await _preparar(membro, "t-cf-frase", quando="amanhã das 10h às 10h30", ate=None)

    proposta = await _proposta_unica(membro)
    assert _hora_local(proposta.inicio_utc) == (10, 0)
    assert _hora_local(proposta.fim_utc) == (10, 30)


async def test_intervalo_invertido_pergunta_em_vez_de_marcar(monkeypatch):
    """"das 20h às 19h" não vira compromisso de 23 horas nem de 1 hora calada."""
    with google_configurado():
        membro = await membro_conectado("ConfirmaIntervaloInvertido")
        injetar_google(monkeypatch, gravador())
        resposta = await _preparar(
            membro, "t-cf-inv", quando="amanhã das 20h às 19h", ate=None
        )

    assert await _propostas(membro.id) == []
    assert "termina" in resposta.lower()


# ── desistência: a tool tem que RODAR antes de a resposta afirmar ────────
# Canário real de 18/08/2026: o usuário disse "não" com um compromisso
# preparado, o Mordomo respondeu "descartado" e a proposta continuou de pé —
# o "sim" seguinte, sobre outro assunto, encontraria ela esperando.


class _AgenteMudo:
    """Substitui o subagente ReAct. Se for chamado, o teste vê `chamado=True` —
    é assim que se prova que o descarte NÃO dependeu do LLM."""

    def __init__(self, resposta: str = "descartado!"):
        self.chamado = False
        self._resposta = resposta

    async def ainvoke(self, entrada, config=None):
        self.chamado = True
        return {"messages": [*entrada["messages"], AIMessage(self._resposta)]}


async def _falar_com_agenda(membro, turn: str, texto: str, agente: _AgenteMudo):
    from mordomo.agents.agenda import no_agenda

    anterior, no_agenda.agente = no_agenda.agente, agente
    try:
        estado = {"messages": [HumanMessage(texto)]}
        return await no_agenda(estado, cfg_de(membro, turn))
    finally:
        no_agenda.agente = anterior


async def test_desistencia_executa_o_descarte_sem_passar_pelo_llm(monkeypatch):
    with google_configurado():
        membro = await membro_conectado("AgendaDesiste")
        injetar_google(monkeypatch, gravador())
        await _preparar(membro, "t-cf-des1")
        agente = _AgenteMudo()
        saida = await _falar_com_agenda(membro, "t-cf-des2", "não, desisti", agente)

    assert await _propostas(membro.id) == [], "a proposta tem que sumir de verdade"
    assert agente.chamado is False
    assert "descartei" in saida["messages"][-1].content.lower()


async def test_desistencia_sem_nada_pendente_nao_afirma_descarte(monkeypatch):
    """Sem proposta esperando, "não" é conversa — quem responde é o LLM."""
    with google_configurado():
        membro = await membro_conectado("AgendaDesisteVazio")
        injetar_google(monkeypatch, gravador())
        agente = _AgenteMudo("Não há nada preparado.")
        saida = await _falar_com_agenda(membro, "t-cf-des3", "não", agente)

    assert agente.chamado is True
    assert saida["messages"][-1].content == "Não há nada preparado."


async def test_frase_com_pedido_novo_nao_vira_descarte(monkeypatch):
    """"não precisa avisar o Davi" tem pedido dentro — quem lê é o LLM, e a
    proposta continua de pé até alguém decidir de verdade."""
    with google_configurado():
        membro = await membro_conectado("AgendaNegacaoLegitima")
        injetar_google(monkeypatch, gravador())
        await _preparar(membro, "t-cf-des4")
        agente = _AgenteMudo("Certo.")
        await _falar_com_agenda(membro, "t-cf-des5", "não precisa avisar o Davi", agente)

    assert agente.chamado is True
    assert len(await _propostas(membro.id)) == 1


async def test_descarte_singular_com_duas_propostas_pede_qual_sem_apagar(monkeypatch):
    """"Descarte" no singular é ambíguo quando há duas propostas: informar a
    quantidade depois de apagar não desfaz o efeito destrutivo."""
    with google_configurado():
        membro = await membro_conectado("AgendaDuasPropostas")
        injetar_google(monkeypatch, gravador())
        await _preparar(membro, "t-cf-des6")
        await _preparar(membro, "t-cf-des7", quando=_amanha(15))
        saida = await _falar_com_agenda(membro, "t-cf-des8", "descarte", _AgenteMudo())

    assert len(await _propostas(membro.id)) == 2
    assert "2" in saida["messages"][-1].content
    assert "qual" in saida["messages"][-1].content.lower()


async def test_descarte_todas_com_duas_propostas_apaga_as_duas(monkeypatch):
    with google_configurado():
        membro = await membro_conectado("AgendaDuasPropostasTodas")
        injetar_google(monkeypatch, gravador())
        await _preparar(membro, "t-cf-des9")
        await _preparar(membro, "t-cf-des10", quando=_amanha(15))
        saida = await _falar_com_agenda(
            membro, "t-cf-des11", "descarte todas", _AgenteMudo()
        )

    assert await _propostas(membro.id) == []
    assert "2" in saida["messages"][-1].content


# ── jornada de criação (multi-turno: preparar hoje, confirmar depois) ────
# Marcar um compromisso não é um turno: é preparar, conferir e confirmar — às
# vezes com horas no meio. O turno já era medido; a NECESSIDADE, não.


async def _eventos_de_jornada(journey_id: str) -> list[ProductEvent]:
    async with Sessao() as s:
        res = await s.execute(
            select(ProductEvent)
            .where(ProductEvent.journey_id == journey_id)
            .order_by(ProductEvent.id)
        )
        return list(res.scalars())


async def _tipos_de_jornada(journey_id: str) -> list[str]:
    return [e.tipo for e in await _eventos_de_jornada(journey_id)]


async def test_preparar_inicia_a_jornada_e_grava_o_mesmo_id_na_proposta(monkeypatch):
    with google_configurado():
        membro = await membro_conectado("JornadaPrepara")
        injetar_google(monkeypatch, gravador())
        await _preparar(membro, "t-jc-1", ate=_amanha(10))

    proposta = await _proposta_unica(membro)
    assert proposta.journey_id, "a proposta é o elo entre os dois turnos"

    inicios = [
        e for e in await _eventos_de_jornada(proposta.journey_id)
        if e.tipo == "journey_started"
    ]
    assert len(inicios) == 1
    assert inicios[0].payload["journey_type"] == "calendar_create"
    assert inicios[0].payload["loads"] == ["mental"]
    assert inicios[0].turn_id == "t-jc-1", "a jornada nasce amarrada ao turno da preparação"


async def test_convidados_somam_carga_de_logistica(monkeypatch):
    with google_configurado():
        membro = await membro_conectado("JornadaLogistica")
        injetar_google(monkeypatch, gravador())
        await _preparar(membro, "t-jc-2", ate=_amanha(10), convidados=[CONVIDADO])

    proposta = await _proposta_unica(membro)
    inicio = (await _eventos_de_jornada(proposta.journey_id))[0]
    assert inicio.payload["loads"] == ["mental", "logistics"]


async def test_confirmacao_no_google_resolve_a_jornada_uma_unica_vez(monkeypatch):
    with google_configurado():
        membro = await membro_conectado("JornadaConfirmaGoogle")
        injetar_google(monkeypatch, gravador())
        await _preparar(membro, "t-jc-3", ate=_amanha(10))
        proposta = await _proposta_unica(membro)
        await _confirmar(membro, "t-jc-3b")
        await _confirmar(membro, "t-jc-3c")  # o "sim" repetido

    assert proposta.journey_id is not None
    eventos = await _eventos_de_jornada(proposta.journey_id)
    resolvidos = [e for e in eventos if e.tipo == "journey_resolved"]
    assert len(resolvidos) == 1, "confirmação repetida não resolve de novo"
    assert resolvidos[0].payload["journey_type"] == "calendar_create"


async def test_confirmacao_nativa_tambem_resolve(monkeypatch):
    with google_configurado():
        membro = await criar_membro("JornadaConfirmaNativa")
        injetar_google(monkeypatch, gravador())
        await _preparar(membro, "t-jc-4", ate=_amanha(10))
        proposta = await _proposta_unica(membro)
        await _confirmar(membro, "t-jc-4b")

    assert (await _tipos_de_jornada(proposta.journey_id)).count("journey_resolved") == 1


async def test_falha_no_google_nao_resolve_a_jornada(monkeypatch):
    """A proposta volta a valer e a necessidade continua ABERTA — resolver aqui
    seria afirmar que o compromisso existe quando ele não existe."""
    def so_a_criacao_falha(r):
        # A leitura (checagem de conflito, na preparação) segue normal: o que
        # cai é a CRIAÇÃO, depois do sim.
        if r.method == "GET":
            return httpx.Response(200, json={"items": []})
        return httpx.Response(503, json={"error": "indisponivel"})

    with google_configurado():
        membro = await membro_conectado("JornadaFalhaExterna")
        handler = gravador([so_a_criacao_falha] * 4)
        injetar_google(monkeypatch, handler)
        await _preparar(membro, "t-jc-5", ate=_amanha(10))
        proposta = await _proposta_unica(membro)
        await _confirmar(membro, "t-jc-5b")

    tipos = await _tipos_de_jornada(proposta.journey_id)
    assert "journey_resolved" not in tipos
    assert (await _proposta_unica(membro)).usado_em is None, "a proposta volta a valer"


async def test_descarte_abandona_a_jornada_com_motivo_categorico(monkeypatch):
    with google_configurado():
        membro = await membro_conectado("JornadaDescarta")
        injetar_google(monkeypatch, gravador())
        await _preparar(membro, "t-jc-6", ate=_amanha(10))
        proposta = await _proposta_unica(membro)
        await descartar_evento.ainvoke({}, cfg_de(membro, "t-jc-6b"))

    eventos = await _eventos_de_jornada(proposta.journey_id)
    abandonos = [e for e in eventos if e.tipo == "journey_abandoned"]
    assert len(abandonos) == 1
    assert abandonos[0].payload["reason"] == "user_discarded"
    assert abandonos[0].payload["journey_type"] == "calendar_create"


async def test_descarte_de_duas_propostas_abandona_exatamente_duas_jornadas(monkeypatch):
    with google_configurado():
        membro = await membro_conectado("JornadaDescartaDuas")
        injetar_google(monkeypatch, gravador())
        await _preparar(membro, "t-jc-7", ate=_amanha(10))
        await _preparar(membro, "t-jc-7b", quando=_amanha(15))
        jornadas = {p.journey_id for p in await _propostas(membro.id)}
        assert len(jornadas) == 2, "cada compromisso preparado é uma jornada própria"
        await descartar_evento.ainvoke({}, cfg_de(membro, "t-jc-7c"))

    for journey_id in jornadas:
        assert (await _tipos_de_jornada(journey_id)).count("journey_abandoned") == 1


async def test_descarte_sem_nada_pendente_nao_abandona_nada(monkeypatch):
    """Nem toda desistência tem jornada: descartar o vazio não pode inventar
    abandono nenhum."""
    desde = datetime.now(UTC)
    with google_configurado():
        membro = await membro_conectado("JornadaDescartaVazio")
        injetar_google(monkeypatch, gravador())
        await descartar_evento.ainvoke({}, cfg_de(membro, "t-jc-8"))

    async with Sessao() as s:
        abandonos = list(
            (
                await s.execute(
                    select(ProductEvent).where(
                        ProductEvent.tipo == "journey_abandoned", ProductEvent.ts >= desde
                    )
                )
            ).scalars()
        )
    assert abandonos == []


async def test_proposta_expirada_nao_inventa_resolucao_nem_abandono(monkeypatch):
    with google_configurado():
        membro = await membro_conectado("JornadaExpirada")
        injetar_google(monkeypatch, gravador())
        await _preparar(membro, "t-jc-9", ate=_amanha(10))
        proposta = await _proposta_unica(membro)
        async with Sessao() as s:
            alvo = (await s.execute(
                select(EventProposal).where(EventProposal.id == proposta.id)
            )).scalar_one()
            alvo.expira_em = datetime.now(UTC) - timedelta(minutes=1)
            await s.commit()
        await _confirmar(membro, "t-jc-9b")
        await descartar_evento.ainvoke({}, cfg_de(membro, "t-jc-9c"))

    tipos = await _tipos_de_jornada(proposta.journey_id)
    assert "journey_resolved" not in tipos and "journey_abandoned" not in tipos


async def test_nenhum_evento_de_jornada_leva_titulo_email_local_ou_link(monkeypatch):
    with google_configurado():
        membro = await membro_conectado("JornadaPrivacidade")
        injetar_google(monkeypatch, gravador())
        await _preparar(
            membro, "t-jc-10", ate=_amanha(10), local="Av. Paulista 1000",
            convidados=[CONVIDADO], com_meet=True,
        )
        proposta = await _proposta_unica(membro)
        await _confirmar(membro, "t-jc-10b")

    bruto = repr([e.payload for e in await _eventos_de_jornada(proposta.journey_id)])
    for proibido in (TITULO, CONVIDADO, "Av. Paulista 1000", LINK, "access-valido"):
        assert proibido not in bruto


async def test_jornada_de_agenda_aparece_no_placar_de_jornadas(monkeypatch):
    from mordomo.reporting.queries import jornadas as agregar_jornadas

    desde = datetime.now(UTC)
    with google_configurado():
        membro = await membro_conectado("JornadaNoPlacar")
        injetar_google(monkeypatch, gravador())
        await _preparar(membro, "t-jc-11", ate=_amanha(10))
        await _confirmar(membro, "t-jc-11b")

    placar = await agregar_jornadas(desde)
    assert placar["por_tipo"].get("calendar_create") == 1
    assert placar["resolvidas"] == 1
