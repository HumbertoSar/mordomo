"""Agenda conversacional: quem tem Google conectado escreve NO GOOGLE.

Incidente real (16/08/2026): Humberto, com a conta já conectada, pediu por
áudio "almoço com a Manuzinha da FGV amanhã das 12h às 16h". A tool respondeu
"criado", gravou FamilyEvent id=2 — e nada apareceu no Google Agenda. O fim às
16h também se perdeu. Estes testes são a rede que impede a volta disso.

Todo o Google é falso aqui (httpx.MockTransport): sem rede, sem chave (regra
nº 7)."""

import json
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlparse
from zoneinfo import ZoneInfo

import httpx
import pytest
from sqlalchemy import func, select

from apoio import cfg_de, google_configurado
from apoio import criar_membro as _membro
from mordomo.db.models import FamilyEvent, ProductEvent
from mordomo.db.session import Sessao
from mordomo.integracoes import google
from mordomo.integracoes.google_api import GoogleAPI
from mordomo.tools.agenda import criar_evento, listar_agenda

SP = ZoneInfo("America/Sao_Paulo")
LINK = "https://calendar.google.com/event?eid=xyz"
TITULO = "almoço com a Manuzinha da FGV"


# ── andaimes ─────────────────────────────────────────────────────────────


def _gravador(respostas=None):
    """Handler que guarda cada requisição; por padrão responde 200."""
    chamadas: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        chamadas.append(request)
        if respostas:
            return respostas.pop(0)(request)
        if request.method == "GET":
            return httpx.Response(200, json={"items": []})
        return httpx.Response(200, json={"id": "evt-1", "htmlLink": LINK})

    handler.chamadas = chamadas
    return handler


def _injetar(monkeypatch, handler) -> None:
    """Faz `google.GoogleAPI()` nascer falando com o MockTransport.

    A tool não recebe `api` (os argumentos dela são do LLM), então a injeção
    acontece no ponto onde o cliente é criado."""
    monkeypatch.setattr(
        google,
        "GoogleAPI",
        lambda: GoogleAPI(cliente=httpx.AsyncClient(transport=httpx.MockTransport(handler))),
    )


async def _conectado(nome: str, *, expira_em_min: int = 60):
    membro = await _membro(nome)
    await google.salvar_conexao(
        membro.id,
        access_token="access-valido",
        refresh_token="refresh-valido",
        expira_em=datetime.now(UTC) + timedelta(minutes=expira_em_min),
        escopo=google.ESCOPO,
    )
    return membro


async def _eventos_nativos(member_id: int) -> list[FamilyEvent]:
    async with Sessao() as s:
        res = await s.execute(
            select(FamilyEvent).where(FamilyEvent.criado_por == member_id)
        )
        return list(res.scalars())


async def _evento_nativo(membro, titulo: str, *, dias: int) -> None:
    async with Sessao() as s:
        s.add(
            FamilyEvent(
                titulo=titulo,
                inicio_utc=datetime.now(UTC) + timedelta(days=dias),
                criado_por=membro.id,
            )
        )
        await s.commit()


async def _tool_results(turn: str) -> list[ProductEvent]:
    async with Sessao() as s:
        res = await s.execute(
            select(ProductEvent)
            .where(ProductEvent.turn_id == turn, ProductEvent.tipo == "tool_result")
            .order_by(ProductEvent.id)
        )
        return list(res.scalars())


def _amanha(hora: int) -> str:
    return f"amanhã às {hora}h"


async def _criar(membro, turn, **campos) -> str:
    argumentos = {"titulo": TITULO, "quando": _amanha(12), "ate": None, "local": None}
    argumentos.update(campos)
    return await criar_evento.ainvoke(argumentos, cfg_de(membro, turn))


# ── o incidente ──────────────────────────────────────────────────────────


async def test_membro_conectado_cria_no_google_e_nao_na_agenda_nativa(monkeypatch):
    """O teste central: pedido conversacional de quem tem Google conectado vai
    para /calendars/primary/events e NÃO deixa linha em family_events."""
    with google_configurado():
        membro = await _conectado("AgendaGoogleCentral")
        handler = _gravador()
        _injetar(monkeypatch, handler)
        resposta = await _criar(membro, "t-ag-1", ate=_amanha(16))

    posts = [c for c in handler.chamadas if c.method == "POST"]
    assert posts, "o pedido tem que chegar ao Google"
    assert "/calendars/primary/events" in str(posts[0].url)
    assert posts[0].headers["Authorization"] == "Bearer access-valido"
    assert await _eventos_nativos(membro.id) == [], "nada de FamilyEvent para quem tem Google"
    assert "Google Agenda" in resposta


async def test_termino_dito_na_fala_chega_ao_google(monkeypatch):
    """'das 12h às 16h': o fim às 16h é dado do usuário, não pode evaporar."""
    with google_configurado():
        membro = await _conectado("AgendaGoogleFim")
        handler = _gravador()
        _injetar(monkeypatch, handler)
        await _criar(membro, "t-ag-2", ate=_amanha(16))

    corpo = json.loads(handler.chamadas[0].content)
    inicio = datetime.fromisoformat(corpo["start"]["dateTime"]).astimezone(SP)
    fim = datetime.fromisoformat(corpo["end"]["dateTime"]).astimezone(SP)
    assert (inicio.hour, inicio.minute) == (12, 0)
    assert (fim.hour, fim.minute) == (16, 0)
    assert fim.date() == inicio.date()
    assert corpo["start"]["timeZone"] == "America/Sao_Paulo"
    assert corpo["end"]["timeZone"] == "America/Sao_Paulo"


async def test_termino_so_com_hora_vale_no_dia_do_inicio(monkeypatch):
    """O LLM pode mandar só "16h" — o dia é o do início, nunca o de hoje."""
    with google_configurado():
        membro = await _conectado("AgendaGoogleHoraSolta")
        handler = _gravador()
        _injetar(monkeypatch, handler)
        await _criar(membro, "t-ag-3", ate="16h")

    corpo = json.loads(handler.chamadas[0].content)
    inicio = datetime.fromisoformat(corpo["start"]["dateTime"]).astimezone(SP)
    fim = datetime.fromisoformat(corpo["end"]["dateTime"]).astimezone(SP)
    assert fim.date() == inicio.date() and fim.hour == 16


async def test_sem_termino_usa_a_duracao_padrao(monkeypatch):
    from mordomo.tools.agenda import DURACAO_PADRAO_MINUTOS

    with google_configurado():
        membro = await _conectado("AgendaGoogleSemFim")
        handler = _gravador()
        _injetar(monkeypatch, handler)
        await _criar(membro, "t-ag-4")

    corpo = json.loads(handler.chamadas[0].content)
    inicio = datetime.fromisoformat(corpo["start"]["dateTime"])
    fim = datetime.fromisoformat(corpo["end"]["dateTime"])
    assert fim - inicio == timedelta(minutes=DURACAO_PADRAO_MINUTOS)


async def test_termino_antes_do_inicio_pergunta_e_nao_cria_nada(monkeypatch):
    """Fim <= início é fala mal entendida: perguntar é melhor que inventar."""
    with google_configurado():
        membro = await _conectado("AgendaGoogleFimInvalido")
        handler = _gravador()
        _injetar(monkeypatch, handler)
        resposta = await _criar(membro, "t-ag-5", ate=_amanha(10))

    assert handler.chamadas == [], "nada pode ir para o Google"
    assert await _eventos_nativos(membro.id) == []
    assert "criado" not in resposta.lower()
    assert (await _tool_results("t-ag-5"))[-1].payload["motivo"] == "termino_antes_do_inicio"


# ── nunca fingir sucesso, nunca cair calado para a agenda nativa ─────────


async def test_google_fora_do_ar_nao_vira_evento_nativo(monkeypatch):
    with google_configurado():
        membro = await _conectado("AgendaGoogleForaDoAr")
        handler = _gravador([lambda r: httpx.Response(503, text="manutenção")])
        _injetar(monkeypatch, handler)
        resposta = await _criar(membro, "t-ag-6", ate=_amanha(16))

    assert await _eventos_nativos(membro.id) == [], "sem fallback silencioso"
    assert "criado" not in resposta.lower()
    resultado = (await _tool_results("t-ag-6"))[-1]
    assert resultado.payload["ok"] is False
    assert resultado.payload["motivo"] == "calendario_recusou"


async def test_credencial_revogada_pede_reconexao_sem_fingir_sucesso(monkeypatch):
    with google_configurado():
        membro = await _conectado("AgendaGoogleRevogado", expira_em_min=-5)
        handler = _gravador([lambda r: httpx.Response(400, json={"error": "invalid_grant"})])
        _injetar(monkeypatch, handler)
        resposta = await _criar(membro, "t-ag-7", ate=_amanha(16))

    assert await _eventos_nativos(membro.id) == []
    assert "/google" in resposta, "o caminho de volta tem que estar na resposta"
    assert (await _tool_results("t-ag-7"))[-1].payload["motivo"] == "reconectar"


async def test_listar_com_google_fora_do_ar_nao_mostra_a_agenda_nativa(monkeypatch):
    """Misturar as duas agendas numa falha é pior que dizer 'não consegui':
    o usuário leria a lista curta como 'meu dia está livre'."""
    with google_configurado():
        membro = await _conectado("AgendaGoogleListaFalha")
        # Longe de hoje de propósito: o banco de teste é compartilhado pela
        # sessão e o briefing matinal lê a agenda da família DO DIA.
        await _evento_nativo(membro, "dentista secreto", dias=30)
        handler = _gravador([lambda r: httpx.Response(503, text="manutenção")])
        _injetar(monkeypatch, handler)
        resposta = await listar_agenda.ainvoke({"dias": 60}, cfg_de(membro, "t-ag-8"))

    assert "dentista secreto" not in resposta
    assert "livre" not in resposta.lower(), "não afirmar agenda livre quando não sabemos"
    assert (await _tool_results("t-ag-8"))[-1].payload["ok"] is False


# ── idempotência por turno ───────────────────────────────────────────────


async def test_retry_do_mesmo_turno_nao_duplica_no_google(monkeypatch):
    """O pipeline repete o turno quando o LLM trava; o mesmo id de evento faz o
    Google recusar com 409 — que aqui é sucesso, não falha."""
    with google_configurado():
        membro = await _conectado("AgendaGoogleRetry")
        handler = _gravador([
            lambda r: httpx.Response(200, json={"id": "evt-1", "htmlLink": LINK}),
            lambda r: httpx.Response(409, json={"error": "duplicate"}),
        ])
        _injetar(monkeypatch, handler)
        primeira = await _criar(membro, "t-ag-9", ate=_amanha(16))
        segunda = await _criar(membro, "t-ag-9", ate=_amanha(16))

    ids = [json.loads(c.content)["id"] for c in handler.chamadas]
    assert ids[0] == ids[1], "mesmo turno, mesmo id"
    assert "Google Agenda" in primeira and "Google Agenda" in segunda
    assert await _eventos_nativos(membro.id) == []


async def test_turno_novo_pode_criar_outro_evento(monkeypatch):
    """Pedir de novo depois é um pedido NOVO — a idempotência não pode engolir."""
    with google_configurado():
        membro = await _conectado("AgendaGoogleTurnoNovo")
        handler = _gravador()
        _injetar(monkeypatch, handler)
        await _criar(membro, "t-ag-10a", ate=_amanha(16))
        await _criar(membro, "t-ag-10b", ate=_amanha(16))

    ids = [json.loads(c.content)["id"] for c in handler.chamadas]
    assert ids[0] != ids[1]


async def test_id_do_evento_e_aceito_pelo_google(monkeypatch):
    """O Google exige base32hex no id (só a-v e 0-9) e recusa o resto com 400."""
    with google_configurado():
        membro = await _conectado("AgendaGoogleId")
        handler = _gravador()
        _injetar(monkeypatch, handler)
        await _criar(membro, "t-ag-11", ate=_amanha(16))

    identificador = json.loads(handler.chamadas[0].content)["id"]
    assert 5 <= len(identificador) <= 1024
    assert set(identificador) <= set("abcdefghijklmnopqrstuv0123456789")


# ── leitura ──────────────────────────────────────────────────────────────


def _evento_google(inicio_utc: str, fim_utc: str, titulo: str, local: str | None = None) -> dict:
    evento = {
        "summary": titulo,
        "start": {"dateTime": inicio_utc},
        "end": {"dateTime": fim_utc},
    }
    if local:
        evento["location"] = local
    return evento


async def test_listar_le_do_google_e_formata_no_fuso_da_familia(monkeypatch):
    corpo = {
        "items": [
            _evento_google("2026-08-17T15:00:00Z", "2026-08-17T19:00:00Z", TITULO, "FGV")
        ]
    }
    with google_configurado():
        membro = await _conectado("AgendaGoogleLista")
        handler = _gravador([lambda r: httpx.Response(200, json=corpo)])
        _injetar(monkeypatch, handler)
        resposta = await listar_agenda.ainvoke({"dias": 7}, cfg_de(membro, "t-ag-12"))

    pedido = handler.chamadas[0]
    assert pedido.method == "GET"
    assert "/calendars/primary/events" in str(pedido.url)
    assert TITULO in resposta
    assert "12:00" in resposta and "16:00" in resposta, "15:00Z é 12:00 em São Paulo"
    assert "Google Agenda" in resposta


@pytest.mark.parametrize("dias,esperado", [(7, 7), (1, 1), (0, 1), (-3, 1), (999, 30)])
async def test_janela_da_listagem_google_respeita_dias_normalizado(monkeypatch, dias, esperado):
    with google_configurado():
        membro = await _conectado(f"AgendaGoogleJanela{dias}")
        handler = _gravador()
        _injetar(monkeypatch, handler)
        await listar_agenda.ainvoke({"dias": dias}, cfg_de(membro, f"t-ag-13-{dias}"))

    consulta = parse_qs(urlparse(str(handler.chamadas[0].url)).query)
    inicio = datetime.fromisoformat(consulta["timeMin"][0])
    fim = datetime.fromisoformat(consulta["timeMax"][0])
    assert fim - inicio == timedelta(days=esperado)
    assert consulta["singleEvents"] == ["true"], "recorrente tem que virar ocorrência"


async def test_evento_de_dia_inteiro_do_google_nao_quebra_a_listagem(monkeypatch):
    corpo = {
        "items": [
            {"summary": "feriado", "start": {"date": "2026-08-17"}, "end": {"date": "2026-08-18"}}
        ]
    }
    with google_configurado():
        membro = await _conectado("AgendaGoogleDiaInteiro")
        handler = _gravador([lambda r: httpx.Response(200, json=corpo)])
        _injetar(monkeypatch, handler)
        resposta = await listar_agenda.ainvoke({"dias": 7}, cfg_de(membro, "t-ag-14"))

    assert "feriado" in resposta
    assert "dia inteiro" in resposta.lower()


# ── agenda nativa (quem não conectou) ────────────────────────────────────


async def test_sem_google_usa_a_agenda_nativa_e_diz_isso(monkeypatch):
    with google_configurado():
        membro = await _membro("AgendaNativaCria")
        handler = _gravador()
        _injetar(monkeypatch, handler)
        resposta = await _criar(membro, "t-ag-15", ate=_amanha(16))

    assert handler.chamadas == [], "sem conexão não se chama o Google"
    eventos = await _eventos_nativos(membro.id)
    assert len(eventos) == 1
    assert "agenda compartilhada do Mordomo" in resposta
    assert "Google" not in resposta


async def test_agenda_nativa_guarda_o_termino(monkeypatch):
    """FamilyEvent só tinha inicio_utc — foi assim que as 16h se perderam."""
    with google_configurado():
        membro = await _membro("AgendaNativaFim")
        _injetar(monkeypatch, _gravador())
        await _criar(membro, "t-ag-16", ate=_amanha(16))

    evento = (await _eventos_nativos(membro.id))[0]
    assert evento.fim_utc is not None
    assert evento.fim_utc.astimezone(SP).hour == 16
    assert evento.fim_utc > evento.inicio_utc


async def test_listar_sem_google_diz_que_a_agenda_e_a_do_mordomo(monkeypatch):
    with google_configurado():
        membro = await _membro("AgendaNativaLista")
        await _evento_nativo(membro, "reunião da escola", dias=30)
        _injetar(monkeypatch, _gravador())
        resposta = await listar_agenda.ainvoke({"dias": 60}, cfg_de(membro, "t-ag-17"))

    assert "reunião da escola" in resposta
    assert "agenda compartilhada do Mordomo" in resposta


async def test_integracao_desligada_mantem_a_agenda_nativa(monkeypatch):
    """Sem GOOGLE_CLIENT_ID configurado a família continua com a agenda de sempre."""
    with google_configurado(client_id=""):
        membro = await _membro("AgendaDesligada")
        handler = _gravador()
        _injetar(monkeypatch, handler)
        resposta = await _criar(membro, "t-ag-18", ate=_amanha(16))

    assert handler.chamadas == []
    assert len(await _eventos_nativos(membro.id)) == 1
    assert "agenda compartilhada do Mordomo" in resposta


async def test_conexao_existente_sem_config_nao_cai_na_agenda_nativa(monkeypatch):
    """Configuração temporariamente ausente não apaga o fato de que a pessoa
    escolheu Google. Cair no banco local repetiria silenciosamente o incidente."""
    with google_configurado():
        membro = await _conectado("AgendaGoogleConfigAusente")
    with google_configurado(client_id=""):
        _injetar(monkeypatch, _gravador())
        resposta = await _criar(membro, "t-ag-config-ausente", ate=_amanha(16))

    assert await _eventos_nativos(membro.id) == []
    assert "criado" not in resposta.lower()
    assert "Google Agenda" in resposta


async def test_clientes_http_criados_pela_integracao_sao_fechados(monkeypatch):
    """Criação/listagem são frequentes; deixar um AsyncClient por chamada aberto
    esgota conexões e descritores no processo de longa duração."""

    class ApiFechavel:
        def __init__(self):
            self.fechado = False

        async def criar_evento(self, access_token, evento):
            return {"id": "evt-fechado", "htmlLink": LINK}

        async def listar_eventos(self, access_token, *, inicio, fim, maximo):
            return []

        async def fechar(self):
            self.fechado = True

    instancias = []

    def fabrica():
        api = ApiFechavel()
        instancias.append(api)
        return api

    with google_configurado():
        membro = await _conectado("AgendaGoogleFechaHttp")
        monkeypatch.setattr(google, "GoogleAPI", fabrica)
        inicio = datetime.now(UTC) + timedelta(days=1)
        await google.criar_evento_na_agenda(
            membro.id,
            titulo="evento",
            inicio=inicio,
            fim=inicio + timedelta(hours=1),
            turn_id="t-fechar-criar",
        )
        await google.listar_eventos_da_agenda(
            membro.id,
            inicio=inicio,
            fim=inicio + timedelta(days=1),
        )

    assert len(instancias) == 2
    assert all(api.fechado for api in instancias)


# ── privacidade (ADR-005) ────────────────────────────────────────────────


async def test_analytics_da_agenda_nao_leva_titulo_local_link_nem_token(monkeypatch):
    with google_configurado():
        membro = await _conectado("AgendaGooglePrivacidade")
        handler = _gravador([
            lambda r: httpx.Response(200, json={"id": "evt-1", "htmlLink": LINK})
        ])
        _injetar(monkeypatch, handler)
        await _criar(membro, "t-ag-19", ate=_amanha(16), local="restaurante da FGV")

    async with Sessao() as s:
        res = await s.execute(
            select(ProductEvent).where(ProductEvent.turn_id == "t-ag-19")
        )
        bruto = repr([e.payload for e in res.scalars()])

    for proibido in (TITULO, "restaurante da FGV", LINK, "access-valido", "refresh-valido"):
        assert proibido not in bruto


async def test_resposta_ao_llm_nunca_carrega_token(monkeypatch):
    with google_configurado():
        membro = await _conectado("AgendaGoogleSemToken")
        handler = _gravador()
        _injetar(monkeypatch, handler)
        criada = await _criar(membro, "t-ag-20", ate=_amanha(16))
        lista = await listar_agenda.ainvoke({"dias": 1}, cfg_de(membro, "t-ag-21"))

    for texto in (criada, lista):
        assert "access-valido" not in texto and "refresh-valido" not in texto


async def test_tool_called_sempre_na_entrada(monkeypatch):
    """Convenção do funil (regra nº 12): a entrada da tool é sempre registrada,
    mesmo quando o caminho termina em pergunta."""
    with google_configurado():
        membro = await _conectado("AgendaGoogleFunil")
        _injetar(monkeypatch, _gravador())
        await _criar(membro, "t-ag-22", quando="quando der vontade")

    async with Sessao() as s:
        res = await s.execute(
            select(func.count())
            .select_from(ProductEvent)
            .where(ProductEvent.turn_id == "t-ag-22", ProductEvent.tipo == "tool_called")
        )
        assert res.scalar_one() == 1
