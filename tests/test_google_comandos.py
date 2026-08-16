"""Comandos /google, /google_teste e /google_desconectar.

Canal-agnósticos por desenho (ADR-001): a regra mora em `channels/comandos.py`
e o teste fala com ela direto — se um dia entrar um terceiro canal, isto
continua valendo sem uma linha nova.

O que se prova aqui é a EXPERIÊNCIA: nunca em grupo, nunca um traceback,
sempre um caminho de volta. O miolo (OAuth, calendário) tem arquivo próprio."""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import httpx

from apoio import criar_membro, google_configurado
from mordomo.channels import comandos
from mordomo.db.models import ChannelIdentity
from mordomo.db.session import Sessao
from mordomo.integracoes import google
from mordomo.integracoes.google_api import GoogleAPI

CANAL = "telegram"
LINK = "https://calendar.google.com/event?eid=abc"


def _externo() -> str:
    """external_id único por execução (o banco de teste é da sessão inteira)."""
    return "tg" + uuid4().hex[:12]


async def _membro_com_canal(nome: str):
    membro = await criar_membro(nome)
    externo = _externo()
    async with Sessao() as s:
        s.add(ChannelIdentity(member_id=membro.id, canal=CANAL, external_id=externo))
        await s.commit()
    return membro, externo


def _api_ok() -> GoogleAPI:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"id": "evt-1", "htmlLink": LINK})

    return GoogleAPI(cliente=httpx.AsyncClient(transport=httpx.MockTransport(handler)))


# ── /google: nunca em grupo ──────────────────────────────────────────────


async def test_google_em_grupo_e_recusado_sem_gerar_url():
    """O link de consentimento é chave da conta Google de UMA pessoa: dito no
    grupo da família, qualquer um clica e conecta o próprio calendário ao
    cadastro de outro (mesma lógica do /convidar, ADR-008)."""
    _, externo = await _membro_com_canal("GoogleGrupo")
    with google_configurado():
        resposta = await comandos.responder(CANAL, externo, "/google", privado=False)
    assert resposta is not None
    assert "privado" in resposta.lower()
    assert "accounts.google.com" not in resposta


async def test_google_teste_e_desconectar_tambem_so_no_privado():
    _, externo = await _membro_com_canal("GoogleGrupoOutros")
    with google_configurado():
        for comando in ("/google_teste", "/google_desconectar"):
            resposta = await comandos.responder(CANAL, externo, comando, privado=False)
            assert resposta is not None and "privado" in resposta.lower()


# ── /google: integração indisponível ─────────────────────────────────────


async def test_google_sem_configuracao_responde_curto_e_util():
    """Sem as variáveis o comando existe e responde — não some, nem estoura."""
    _, externo = await _membro_com_canal("GoogleSemConfig")
    with google_configurado(client_id=""):
        resposta = await comandos.responder(CANAL, externo, "/google")
    assert resposta is not None
    assert "accounts.google.com" not in resposta
    # nada de vazamento de implementação na cara da família
    for feio in ("Traceback", "Exception", "None", "google_client_id"):
        assert feio not in resposta


async def test_google_de_quem_nao_e_da_familia_e_recusado():
    with google_configurado():
        resposta = await comandos.responder(CANAL, _externo(), "/google")
    assert resposta is not None
    assert "accounts.google.com" not in resposta


# ── /google desconectado: a URL e o que ela promete ──────────────────────


async def test_google_desconectado_devolve_url_e_explica_permissoes():
    _, externo = await _membro_com_canal("GoogleDesconectado")
    with google_configurado():
        resposta = await comandos.responder(CANAL, externo, "/google")

    assert "https://accounts.google.com/o/oauth2/v2/auth" in resposta
    minusculo = resposta.lower()
    # o que vamos poder fazer, em português de gente
    assert "agenda" in minusculo or "calendár" in minusculo or "calendar" in minusculo
    # e a garantia que faz a pessoa clicar sem medo
    assert "senha" in minusculo and "google" in minusculo


async def test_url_do_comando_e_de_uso_unico_e_do_membro_certo():
    """O link entregue no chat tem que valer para ESTE membro, uma vez só."""
    membro, externo = await _membro_com_canal("GoogleUrlUnica")
    with google_configurado():
        resposta = await comandos.responder(CANAL, externo, "/google")
        state = resposta.split("state=")[1].split("&")[0].split()[0].strip()
        assert await google.consumir_state(state) == (membro.id, "ok")
        assert (await google.consumir_state(state))[1] == "state_usado"


# ── /google conectado ────────────────────────────────────────────────────


async def test_google_conectado_nao_repete_a_url_e_orienta_os_proximos():
    membro, externo = await _membro_com_canal("GoogleConectado")
    with google_configurado():
        await google.salvar_conexao(membro.id, access_token="a1", refresh_token="r1")
        resposta = await comandos.responder(CANAL, externo, "/google")

    assert "accounts.google.com" not in resposta, "conectado não pede consentimento de novo"
    assert "/google_teste" in resposta
    assert "/google_desconectar" in resposta


# ── /google_teste ────────────────────────────────────────────────────────


async def test_google_teste_desconectado_manda_de_volta_para_google():
    _, externo = await _membro_com_canal("TesteDesconectado")
    with google_configurado():
        resposta = await comandos.responder(CANAL, externo, "/google_teste")
    assert "/google" in resposta


async def test_google_teste_conectado_confirma_e_mostra_o_link():
    membro, externo = await _membro_com_canal("TesteConectado")
    with google_configurado():
        await google.salvar_conexao(
            membro.id,
            access_token="a1",
            refresh_token="r1",
            expira_em=datetime.now(UTC) + timedelta(hours=1),
        )
        resposta = await comandos.responder(
            CANAL, externo, "/google_teste", api=_api_ok()
        )

    minusculo = resposta.lower()
    assert "5 minutos" in minusculo or "5 min" in minusculo
    assert LINK in resposta


async def test_google_teste_repetido_nao_promete_dois_eventos():
    """Comando repetido responde de novo, mas dizendo que o evento é o MESMO."""
    membro, externo = await _membro_com_canal("TesteRepetido")
    with google_configurado():
        await google.salvar_conexao(
            membro.id,
            access_token="a1",
            refresh_token="r1",
            expira_em=datetime.now(UTC) + timedelta(hours=1),
        )
        primeira = await comandos.responder(CANAL, externo, "/google_teste", api=_api_ok())
        segunda = await comandos.responder(CANAL, externo, "/google_teste", api=_api_ok())

    assert LINK in primeira and LINK in segunda
    assert primeira != segunda, "a segunda resposta tem que dizer que já existia"
    assert "já" in segunda.lower()


async def test_google_teste_com_consentimento_revogado_manda_reconectar():
    membro, externo = await _membro_com_canal("TesteRevogado")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"error": "invalid_grant"})

    with google_configurado():
        await google.salvar_conexao(
            membro.id,
            access_token="a1",
            refresh_token="r1",
            expira_em=datetime.now(UTC) - timedelta(minutes=5),
        )
        resposta = await comandos.responder(
            CANAL,
            externo,
            "/google_teste",
            api=GoogleAPI(cliente=httpx.AsyncClient(transport=httpx.MockTransport(handler))),
        )

    assert "/google" in resposta
    for feio in ("Traceback", "invalid_grant", "401", "400"):
        assert feio not in resposta


async def test_depois_de_perder_a_autorizacao_o_google_volta_a_oferecer_o_link():
    """O deadlock que não pode existir: /google_teste manda usar /google e o
    /google responde 'já conectado', porque a linha morta continua no banco.
    Credencial definitivamente inválida tem que devolver o caminho de volta."""
    membro, externo = await _membro_com_canal("TesteDeadlock")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"error": "invalid_grant"})

    with google_configurado():
        await google.salvar_conexao(
            membro.id,
            access_token="a1",
            refresh_token="r1",
            expira_em=datetime.now(UTC) - timedelta(minutes=5),
        )
        teste = await comandos.responder(
            CANAL,
            externo,
            "/google_teste",
            api=GoogleAPI(cliente=httpx.AsyncClient(transport=httpx.MockTransport(handler))),
        )
        assert "/google" in teste
        depois = await comandos.responder(CANAL, externo, "/google")

    assert "accounts.google.com" in depois, "a pessoa tem que conseguir autorizar de novo"
    assert "já está conectado" not in depois.lower()


async def test_chave_de_criptografia_trocada_nao_prende_no_ja_conectado():
    """Rotação de GOOGLE_TOKEN_KEY torna o token guardado ilegível. Isso é
    'não tenho credencial' — e /google tem que oferecer o link, não fingir
    que está tudo conectado."""
    membro, externo = await _membro_com_canal("TesteChaveRotacionada")
    with google_configurado():
        await google.salvar_conexao(membro.id, access_token="a1", refresh_token="r1")
    with google_configurado():  # chave Fernet NOVA (a fixture gera uma por uso)
        resposta = await comandos.responder(CANAL, externo, "/google")

    assert "accounts.google.com" in resposta
    assert "já está conectado" not in resposta.lower()


# ── /google_desconectar ──────────────────────────────────────────────────


async def test_desconectar_remove_e_confirma():
    membro, externo = await _membro_com_canal("DescConfirma")
    with google_configurado():
        await google.salvar_conexao(membro.id, access_token="a1", refresh_token="r1")
        resposta = await comandos.responder(CANAL, externo, "/google_desconectar")
        assert await google.conexao_de(membro.id) is None
    assert resposta is not None and len(resposta) > 10


async def test_desconectar_duas_vezes_nao_reclama():
    """Idempotente do ponto de vista de quem usa: a segunda vez não é erro."""
    membro, externo = await _membro_com_canal("DescDuasVezes")
    with google_configurado():
        await google.salvar_conexao(membro.id, access_token="a1", refresh_token="r1")
        await comandos.responder(CANAL, externo, "/google_desconectar")
        segunda = await comandos.responder(CANAL, externo, "/google_desconectar")
    assert segunda is not None
    for feio in ("Traceback", "erro", "Erro"):
        assert feio not in segunda


async def test_desconectar_avisa_que_o_acesso_some_no_painel_do_google():
    """Não chamamos o endpoint de revogação nesta fatia (ADR-010) — então o
    texto TEM que dizer onde a pessoa corta o acesso de vez."""
    membro, externo = await _membro_com_canal("DescPainel")
    with google_configurado():
        await google.salvar_conexao(membro.id, access_token="a1", refresh_token="r1")
        resposta = await comandos.responder(CANAL, externo, "/google_desconectar")
    assert "myaccount.google.com" in resposta


# ── o resto do mordomo não pode mudar ────────────────────────────────────


async def test_comando_desconhecido_continua_devolvendo_none():
    """`responder` devolve None para o adapter seguir com o fluxo normal —
    /google não pode ter engolido esse contrato."""
    _, externo = await _membro_com_canal("ComandoDesconhecido")
    with google_configurado():
        assert await comandos.responder(CANAL, externo, "/inexistente") is None
        assert await comandos.responder(CANAL, externo, "bom dia") is None


async def test_google_nao_confunde_com_prefixo_parecido():
    """/googlezinho não é /google (o `partition` do comando corta no espaço)."""
    _, externo = await _membro_com_canal("PrefixoParecido")
    with google_configurado():
        assert await comandos.responder(CANAL, externo, "/googlezinho") is None
