"""Consulta confiável da agenda (fatia A do Google Calendar).

Conversa real de 17/08/2026 expôs três buracos na leitura:
  - a janela só olhava para a FRENTE (a partir de agora), então "o que eu tinha
    no dia 7?" não tinha como ser respondido;
  - `maxResults=20` era o teto de UMA página, e a resposta tratava página
    parcial como busca completa — "não encontrei" sem ter procurado tudo;
  - o dia da semana saía do LLM, que errou ("segunda, 24/08" para uma data que
    o modelo calculou de cabeça).

Todo o Google é falso aqui (httpx.MockTransport): sem rede, sem chave (regra
nº 7)."""

from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlparse
from zoneinfo import ZoneInfo

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
from mordomo.db.models import ProductEvent
from mordomo.db.session import Sessao
from mordomo.tools._comum import fmt_data

SP = ZoneInfo("America/Sao_Paulo")

# A conversa real aconteceu numa segunda, 17/08/2026.
AGORA = datetime(2026, 8, 17, 10, 0, tzinfo=SP)


# ── dia da semana determinístico ─────────────────────────────────────────


def test_dia_da_semana_sai_em_ptbr_sem_depender_de_locale():
    """`%a` do strftime segue o locale do processo — na VPS sai "Mon". O dia da
    semana é dado que o usuário confere de relance; ele tem que ser nosso."""
    segunda = datetime(2026, 8, 24, 9, 0, tzinfo=SP)
    assert fmt_data(segunda, dia_semana=True) == "seg 24/08 às 09:00"


def test_dia_da_semana_cobre_a_semana_inteira():
    esperado = ["seg", "ter", "qua", "qui", "sex", "sáb", "dom"]
    for offset, sigla in enumerate(esperado):
        dia = datetime(2026, 8, 17 + offset, 9, 0, tzinfo=SP)
        assert fmt_data(dia, dia_semana=True).startswith(sigla)


def test_dia_da_semana_e_calculado_no_fuso_da_familia():
    """23h em São Paulo é o dia seguinte em UTC: converter tarde demais faria o
    compromisso de domingo à noite virar segunda na resposta."""
    domingo_tarde_da_noite = datetime(2026, 8, 23, 23, 30, tzinfo=SP).astimezone(UTC)
    assert fmt_data(domingo_tarde_da_noite, dia_semana=True) == "dom 23/08 às 23:30"


# ── janela de consulta (passado, futuro, dia solto e intervalo) ──────────


def _janela(inicio: str, fim: str | None = None):
    from mordomo.tools.periodos import resolver_janela

    return resolver_janela(inicio, fim, agora=AGORA)


def test_dia_no_passado_vira_o_dia_inteiro_daquele_dia():
    """"o que eu tinha no dia 7 de agosto?" — a leitura antiga começava em
    `agora` e nunca alcançaria o passado. E `resolver_data` (que serve à CRIAÇÃO,
    onde o futuro é a leitura certa) responderia 07/08/2027."""
    janela = _janela("7 de agosto")
    assert janela.motivo == "ok"
    assert janela.inicio.astimezone(SP) == datetime(2026, 8, 7, 0, 0, tzinfo=SP)
    assert janela.fim.astimezone(SP) == datetime(2026, 8, 8, 0, 0, tzinfo=SP)


def test_dia_no_futuro_tambem_vira_o_dia_inteiro():
    janela = _janela("24/08")
    assert janela.motivo == "ok"
    assert janela.inicio.astimezone(SP) == datetime(2026, 8, 24, 0, 0, tzinfo=SP)
    assert janela.fim.astimezone(SP) == datetime(2026, 8, 25, 0, 0, tzinfo=SP)


def test_intervalo_arbitrario_cobre_o_ultimo_dia_inteiro():
    """"de 1 a 10 de agosto" tem que incluir o dia 10 até o fim — parar às 00:00
    do dia 10 esconderia o dia inteiro que o usuário pediu."""
    janela = _janela("1 de agosto", "10 de agosto")
    assert janela.inicio.astimezone(SP) == datetime(2026, 8, 1, 0, 0, tzinfo=SP)
    assert janela.fim.astimezone(SP) == datetime(2026, 8, 11, 0, 0, tzinfo=SP)


def test_hora_explicita_e_respeitada_nas_duas_pontas():
    janela = _janela("24/08 às 9h", "24/08 às 18h")
    assert janela.inicio.astimezone(SP) == datetime(2026, 8, 24, 9, 0, tzinfo=SP)
    assert janela.fim.astimezone(SP) == datetime(2026, 8, 24, 18, 0, tzinfo=SP)


def test_expressao_relativa_continua_valendo():
    assert _janela("ontem").inicio.astimezone(SP) == datetime(2026, 8, 16, 0, 0, tzinfo=SP)
    assert _janela("hoje").fim.astimezone(SP) == datetime(2026, 8, 18, 0, 0, tzinfo=SP)


def test_expressao_incompreensivel_nao_vira_janela_chutada():
    """Regra nº 3: sem entender, o agente PERGUNTA — nunca inventa período."""
    janela = _janela("quando der vontade")
    assert janela.inicio is None and janela.motivo == "inicio_nao_entendido"


def test_fim_antes_do_inicio_e_recusado():
    janela = _janela("10 de agosto", "1 de agosto")
    assert janela.inicio is None and janela.motivo == "fim_antes_do_inicio"


def test_janela_absurda_e_limitada_e_declara_que_foi_limitada():
    """Intervalo arbitrário é bem-vindo; despejar dez anos de agenda no contexto
    do LLM não. Limitar calado seria mentir sobre o que foi olhado."""
    from mordomo.tools.periodos import MAX_DIAS_JANELA

    janela = _janela("1 de janeiro de 2020", "1 de janeiro de 2030")
    assert janela.motivo == "janela_reduzida"
    assert janela.fim - janela.inicio == timedelta(days=MAX_DIAS_JANELA)


# ── paginação (nunca tratar página parcial como busca completa) ──────────


def _api_falsa(paginas: list[dict]):
    """GoogleAPI falando com um MockTransport que devolve `paginas` em ordem."""
    from mordomo.integracoes.google_api import GoogleAPI

    pedidos: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        pedidos.append(request)
        corpo = paginas[len(pedidos) - 1] if len(pedidos) <= len(paginas) else {"items": []}
        return httpx.Response(200, json=corpo)

    api = GoogleAPI(cliente=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    api.pedidos = pedidos
    return api


def _evento(indice: int) -> dict:
    return {
        "summary": f"evento {indice}",
        "start": {"dateTime": "2026-08-24T12:00:00Z"},
        "end": {"dateTime": "2026-08-24T13:00:00Z"},
    }


def _consulta(pedido: httpx.Request) -> dict:
    return parse_qs(urlparse(str(pedido.url)).query)


async def test_listagem_segue_o_next_page_token_ate_o_fim():
    """O buraco da conversa real: a primeira página voltava com nextPageToken e
    a resposta dizia "não encontrei" sem nunca pedir a segunda."""
    api = _api_falsa([
        {"items": [_evento(1)], "nextPageToken": "pag2"},
        {"items": [_evento(2)]},
    ])
    resultado = await api.listar_eventos(
        "token", inicio=AGORA, fim=AGORA + timedelta(days=1)
    )

    assert len(resultado["itens"]) == 2
    assert resultado["truncado"] is False
    assert len(api.pedidos) == 2
    assert "pageToken" not in _consulta(api.pedidos[0])
    assert _consulta(api.pedidos[1])["pageToken"] == ["pag2"]


async def test_teto_defensivo_interrompe_e_declara_truncamento():
    """Agenda gigante não pode virar laço infinito nem despejo no contexto — mas
    parar calado transformaria a resposta numa mentira."""
    api = _api_falsa([
        {"items": [_evento(1), _evento(2)], "nextPageToken": "pag2"},
        {"items": [_evento(3), _evento(4)], "nextPageToken": "pag3"},
    ])
    resultado = await api.listar_eventos(
        "token", inicio=AGORA, fim=AGORA + timedelta(days=1), teto=3
    )

    assert len(resultado["itens"]) == 3
    assert resultado["truncado"] is True


async def test_filtro_textual_vai_como_q_para_o_google():
    """Filtrar no servidor é o que evita paginar a agenda inteira para achar uma
    reunião — e `q` é o parâmetro oficial de busca do Calendar."""
    api = _api_falsa([{"items": []}])
    await api.listar_eventos(
        "token", inicio=AGORA, fim=AGORA + timedelta(days=1), texto="reunião"
    )

    assert _consulta(api.pedidos[0])["q"] == ["reunião"]


async def test_sem_filtro_textual_o_q_nao_e_enviado():
    api = _api_falsa([{"items": []}])
    await api.listar_eventos("token", inicio=AGORA, fim=AGORA + timedelta(days=1))

    assert "q" not in _consulta(api.pedidos[0])


# ── a tool de consulta (janela explícita, passado e futuro) ──────────────


def _congelar(monkeypatch) -> None:
    """Prende o relógio da tool em 17/08/2026 10h — o dia da conversa real."""
    from mordomo.tools import agenda

    monkeypatch.setattr(agenda, "_agora", lambda: AGORA.astimezone(UTC))


async def _consultar(monkeypatch, membro, turn: str, **campos) -> str:
    from mordomo.tools.agenda import consultar_agenda

    argumentos = {"inicio": "hoje", "fim": None, "busca": None}
    argumentos.update(campos)
    return await consultar_agenda.ainvoke(argumentos, cfg_de(membro, turn))


def _janela_pedida(pedido: httpx.Request) -> tuple[datetime, datetime]:
    consulta = _consulta(pedido)
    return (
        datetime.fromisoformat(consulta["timeMin"][0]).astimezone(SP),
        datetime.fromisoformat(consulta["timeMax"][0]).astimezone(SP),
    )


async def test_consulta_historica_monta_a_janela_do_dia_pedido(monkeypatch):
    """"o que eu tinha no dia 7 de agosto?" — o passado era inalcançável."""
    with google_configurado():
        membro = await membro_conectado("ConsultaPassado")
        handler = gravador()
        injetar_google(monkeypatch, handler)
        _congelar(monkeypatch)
        await _consultar(monkeypatch, membro, "t-cons-1", inicio="7 de agosto")

    inicio, fim = _janela_pedida(handler.chamadas[0])
    assert inicio == datetime(2026, 8, 7, 0, 0, tzinfo=SP)
    assert fim == datetime(2026, 8, 8, 0, 0, tzinfo=SP)


async def test_consulta_futura_monta_a_janela_do_dia_pedido(monkeypatch):
    with google_configurado():
        membro = await membro_conectado("ConsultaFuturo")
        handler = gravador()
        injetar_google(monkeypatch, handler)
        _congelar(monkeypatch)
        await _consultar(monkeypatch, membro, "t-cons-2", inicio="24/08")

    inicio, fim = _janela_pedida(handler.chamadas[0])
    assert inicio == datetime(2026, 8, 24, 0, 0, tzinfo=SP)
    assert fim == datetime(2026, 8, 25, 0, 0, tzinfo=SP)


async def test_consulta_por_intervalo_cobre_as_duas_pontas(monkeypatch):
    with google_configurado():
        membro = await membro_conectado("ConsultaIntervalo")
        handler = gravador()
        injetar_google(monkeypatch, handler)
        _congelar(monkeypatch)
        await _consultar(monkeypatch, membro, "t-cons-3", inicio="1 de agosto", fim="10 de agosto")

    inicio, fim = _janela_pedida(handler.chamadas[0])
    assert inicio == datetime(2026, 8, 1, 0, 0, tzinfo=SP)
    assert fim == datetime(2026, 8, 11, 0, 0, tzinfo=SP)


async def test_consulta_mostra_o_dia_da_semana_calculado_por_nos(monkeypatch):
    corpo = {"items": [{
        "summary": "Reunião Xiao Long Mo",
        "start": {"dateTime": "2026-08-24T12:00:00Z"},
        "end": {"dateTime": "2026-08-24T13:00:00Z"},
    }]}
    with google_configurado():
        membro = await membro_conectado("ConsultaDiaSemana")
        injetar_google(monkeypatch, gravador([lambda r: httpx.Response(200, json=corpo)]))
        _congelar(monkeypatch)
        resposta = await _consultar(monkeypatch, membro, "t-cons-4", inicio="24/08")

    assert "seg 24/08 às 09:00" in resposta, "24/08/2026 é segunda — e quem conta somos nós"


async def test_busca_incompleta_nao_vira_falso_nao_encontrei(monkeypatch):
    """O pior resultado possível: dizer "não encontrei" sem ter olhado tudo.

    Página VAZIA com `nextPageToken` é resposta legal do Google (os eventos da
    página não casaram com o `q`). Uma agenda grande pode encadear muitas — e o
    teto de páginas nos faz parar no meio da janela. Parar é certo; deixar isso
    virar "não encontrei" é o defeito."""
    chamadas: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        chamadas.append(request)
        return httpx.Response(200, json={"items": [], "nextPageToken": "proxima"})

    handler.chamadas = chamadas
    with google_configurado():
        membro = await membro_conectado("ConsultaTruncada")
        injetar_google(monkeypatch, handler)
        _congelar(monkeypatch)
        resposta = await _consultar(
            monkeypatch, membro, "t-cons-5", inicio="1 de agosto", busca="reunião"
        )

    from mordomo.integracoes.google_api import MAX_PAGINAS

    assert len(handler.chamadas) == MAX_PAGINAS, "o teto de páginas tem que segurar o laço"
    assert "não encontrei" not in resposta.lower()
    assert "não consegui ver a janela inteira" in resposta.lower()


async def test_busca_textual_completa_e_vazia_pode_dizer_que_nao_achou(monkeypatch):
    with google_configurado():
        membro = await membro_conectado("ConsultaVazia")
        injetar_google(monkeypatch, gravador())
        _congelar(monkeypatch)
        resposta = await _consultar(monkeypatch, membro, "t-cons-6", inicio="24/08", busca="dentista")

    assert "não encontrei" in resposta.lower()


async def test_evento_cancelado_nao_aparece_na_consulta(monkeypatch):
    corpo = {"items": [
        {"summary": "cancelado", "status": "cancelled",
         "start": {"dateTime": "2026-08-24T12:00:00Z"},
         "end": {"dateTime": "2026-08-24T13:00:00Z"}},
        {"summary": "de pé", "start": {"dateTime": "2026-08-24T15:00:00Z"},
         "end": {"dateTime": "2026-08-24T16:00:00Z"}},
    ]}
    with google_configurado():
        membro = await membro_conectado("ConsultaCancelado")
        injetar_google(monkeypatch, gravador([lambda r: httpx.Response(200, json=corpo)]))
        _congelar(monkeypatch)
        resposta = await _consultar(monkeypatch, membro, "t-cons-7", inicio="24/08")

    assert "cancelado" not in resposta and "de pé" in resposta


async def test_periodo_nao_entendido_pergunta_e_nao_chama_o_google(monkeypatch):
    with google_configurado():
        membro = await membro_conectado("ConsultaSemPeriodo")
        handler = gravador()
        injetar_google(monkeypatch, handler)
        _congelar(monkeypatch)
        resposta = await _consultar(monkeypatch, membro, "t-cons-8", inicio="quando der")

    assert handler.chamadas == []
    assert "pergunte" in resposta.lower()


async def test_falha_do_google_na_consulta_nao_vira_agenda_livre(monkeypatch):
    with google_configurado():
        membro = await membro_conectado("ConsultaFalha")
        injetar_google(monkeypatch, gravador([lambda r: httpx.Response(503, text="manutenção")]))
        _congelar(monkeypatch)
        resposta = await _consultar(monkeypatch, membro, "t-cons-9", inicio="24/08")

    assert "livre" not in resposta.lower() and "não encontrei" not in resposta.lower()


async def test_consulta_sem_google_le_a_agenda_do_mordomo(monkeypatch):
    with google_configurado():
        membro = await criar_membro("ConsultaNativa")
        handler = gravador()
        injetar_google(monkeypatch, handler)
        _congelar(monkeypatch)
        resposta = await _consultar(monkeypatch, membro, "t-cons-10", inicio="24/08")

    assert handler.chamadas == []
    assert "agenda compartilhada do Mordomo" in resposta


async def test_listar_agenda_dos_proximos_dias_continua_funcionando(monkeypatch):
    """Compatibilidade: o briefing e o "o que temos hoje?" não podem quebrar."""
    from mordomo.tools.agenda import listar_agenda

    with google_configurado():
        membro = await membro_conectado("ConsultaCompat")
        handler = gravador()
        injetar_google(monkeypatch, handler)
        _congelar(monkeypatch)
        resposta = await listar_agenda.ainvoke({"dias": 7}, cfg_de(membro, "t-cons-11"))

    inicio, fim = _janela_pedida(handler.chamadas[0])
    assert fim - inicio == timedelta(days=7)
    assert "Google Agenda" in resposta


async def test_analytics_da_consulta_nao_leva_titulo_nem_texto_buscado(monkeypatch):
    corpo = {"items": [{
        "summary": "Reunião Xiao Long Mo - Negócios Brasil x China",
        "location": "Av. Paulista 1000",
        "start": {"dateTime": "2026-08-24T12:00:00Z"},
        "end": {"dateTime": "2026-08-24T13:00:00Z"},
    }]}
    with google_configurado():
        membro = await membro_conectado("ConsultaPrivacidade")
        injetar_google(monkeypatch, gravador([lambda r: httpx.Response(200, json=corpo)]))
        _congelar(monkeypatch)
        await _consultar(monkeypatch, membro, "t-cons-12", inicio="24/08", busca="Xiao Long Mo")

    async with Sessao() as s:
        res = await s.execute(select(ProductEvent).where(ProductEvent.turn_id == "t-cons-12"))
        bruto = repr([e.payload for e in res.scalars()])

    for proibido in ("Xiao Long Mo", "Av. Paulista 1000", "access-valido"):
        assert proibido not in bruto


# ── "procure o evento X de amanhã" (canário real de 18/08/2026) ──────────
# O evento tinha acabado de ser criado e estava no histórico da conversa; a
# resposta saiu de lá, sem ninguém consultar agenda nenhuma. Aqui a prova é a
# CHAMADA: o Google recebe a janela do dia inteiro de amanhã com o texto
# buscado, e o que volta ao usuário é o que o Google respondeu.


async def test_procurar_evento_de_amanha_consulta_o_google_de_verdade(monkeypatch):
    corpo = {"items": [{
        "summary": "Reunião com a Ana",
        "start": {"dateTime": "2026-08-18T21:00:00Z"},
        "end": {"dateTime": "2026-08-18T21:30:00Z"},
    }]}
    with google_configurado():
        membro = await membro_conectado("ConsultaProcurarAmanha")
        handler = gravador([lambda r: httpx.Response(200, json=corpo)])
        injetar_google(monkeypatch, handler)
        _congelar(monkeypatch)
        resposta = await _consultar(
            monkeypatch, membro, "t-cons-amanha", inicio="amanhã", busca="Reunião"
        )

    assert handler.chamadas, "a consulta TEM que ir ao Google — não à memória da conversa"
    inicio, fim = _janela_pedida(handler.chamadas[0])
    assert inicio == datetime(2026, 8, 18, 0, 0, tzinfo=SP)
    assert fim == datetime(2026, 8, 19, 0, 0, tzinfo=SP)
    assert _consulta(handler.chamadas[0])["q"] == ["Reunião"]
    assert "Reunião com a Ana" in resposta and "ter 18/08 às 18:00" in resposta
