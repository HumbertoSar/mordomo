"""Conexão Google por membro: disponibilidade, guarda cifrada e desconexão.

Sem rede e sem chave real (regra nº 7): a configuração é ligada só dentro do
teste, com uma chave Fernet gerada na hora."""

from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from apoio import criar_membro as _membro
from apoio import google_configurado
from mordomo.db.models import GoogleConnection
from mordomo.db.session import Sessao
from mordomo.integracoes import google


async def test_sem_configuracao_a_integracao_fica_indisponivel():
    """Regra do projeto: o bot SOBE igual sem as variáveis (padrão Langfuse)."""
    with google_configurado(client_id="", client_secret="", redirect_uri="", token_key=""):
        assert google.disponivel() is False


async def test_configuracao_incompleta_tambem_e_indisponivel():
    """Meia configuração é pior que nenhuma: geraria URL que não volta."""
    with google_configurado(redirect_uri=""):
        assert google.disponivel() is False
    with google_configurado(token_key=""):
        # Sem chave de criptografia guardaríamos token em claro — não fazemos.
        assert google.disponivel() is False


async def test_configuracao_completa_habilita():
    with google_configurado():
        assert google.disponivel() is True


async def test_token_fica_cifrado_na_coluna():
    """ADR-005 levado ao repouso: o dump do banco não pode conter o token."""
    membro = await _membro("GoogleCifra")
    with google_configurado():
        await google.salvar_conexao(
            membro.id,
            access_token="ya29.access-em-claro",
            refresh_token="1//refresh-em-claro",
            expira_em=datetime.now(UTC) + timedelta(hours=1),
        )
    async with Sessao() as s:
        res = await s.execute(
            select(GoogleConnection).where(GoogleConnection.member_id == membro.id)
        )
        linha = res.scalar_one()
    assert "ya29.access-em-claro" not in linha.access_token_cifrado
    assert "1//refresh-em-claro" not in (linha.refresh_token_cifrado or "")


async def test_le_de_volta_os_tokens_decifrados():
    membro = await _membro("GoogleLeitura")
    with google_configurado():
        await google.salvar_conexao(
            membro.id, access_token="acesso-1", refresh_token="refresh-1"
        )
        conexao = await google.conexao_de(membro.id)
        assert conexao is not None
        assert google.access_token_de(conexao) == "acesso-1"
        assert google.refresh_token_de(conexao) == "refresh-1"


async def test_sem_conexao_devolve_none():
    membro = await _membro("GoogleSemConexao")
    with google_configurado():
        assert await google.conexao_de(membro.id) is None


async def test_salvar_de_novo_atualiza_a_mesma_conexao():
    """Um membro tem UMA conexão — reconectar não pode empilhar linhas."""
    membro = await _membro("GoogleReconecta")
    with google_configurado():
        await google.salvar_conexao(membro.id, access_token="a1", refresh_token="r1")
        await google.salvar_conexao(membro.id, access_token="a2", refresh_token="r2")
        conexao = await google.conexao_de(membro.id)
        assert google.access_token_de(conexao) == "a2"
    async with Sessao() as s:
        res = await s.execute(
            select(GoogleConnection).where(GoogleConnection.member_id == membro.id)
        )
        assert len(res.scalars().all()) == 1


async def test_refresh_sem_novo_refresh_token_preserva_o_antigo():
    """O Google só devolve refresh_token no PRIMEIRO consentimento. Um refresh
    normal traz apenas access_token — sobrescrever com None mataria a conexão
    silenciosamente, e a falha só apareceria uma hora depois."""
    membro = await _membro("GooglePreserva")
    with google_configurado():
        await google.salvar_conexao(membro.id, access_token="a1", refresh_token="r1")
        await google.salvar_conexao(membro.id, access_token="a2", refresh_token=None)
        conexao = await google.conexao_de(membro.id)
        assert google.access_token_de(conexao) == "a2"
        assert google.refresh_token_de(conexao) == "r1"


async def test_desconectar_remove_a_conexao():
    membro = await _membro("GoogleDesconecta")
    with google_configurado():
        await google.salvar_conexao(membro.id, access_token="a1", refresh_token="r1")
        assert await google.desconectar(membro.id) is True
        assert await google.conexao_de(membro.id) is None


async def test_desconectar_e_idempotente():
    """Chamar duas vezes não explode nem mente: a segunda diz 'não havia nada'."""
    membro = await _membro("GoogleDesconectaDuas")
    with google_configurado():
        await google.salvar_conexao(membro.id, access_token="a1", refresh_token="r1")
        assert await google.desconectar(membro.id) is True
        assert await google.desconectar(membro.id) is False


async def test_chave_trocada_derruba_a_conexao_sem_vazar():
    """Rotação de chave: o token vira ilegível. A resposta certa é 'reconecte',
    nunca um stacktrace de criptografia no meio da conversa."""
    membro = await _membro("GoogleChaveTrocada")
    with google_configurado():
        await google.salvar_conexao(membro.id, access_token="a1", refresh_token="r1")
    with google_configurado():  # chave NOVA (gerada por fixture a cada uso)
        conexao = await google.conexao_de(membro.id)
        assert google.access_token_de(conexao) is None
