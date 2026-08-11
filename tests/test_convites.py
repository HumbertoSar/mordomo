"""Convites (/convidar → /vincular): permissão por papel, uso único, validade
e as corridas do mundo real (aiogram processa updates em paralelo)."""

import asyncio
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from apoio import criar_membro as _membro
from mordomo.convites import criar_convite, usar_convite
from mordomo.db.models import ChannelIdentity, InviteCode, Member
from mordomo.db.session import Sessao


async def test_fluxo_completo_adulto_convida_novo_vincula():
    admin = await _membro("AdminConvite", "adulto")
    codigo = await criar_convite(admin, "Esposa", "adulto")
    assert codigo is not None and len(codigo) == 6

    membro, motivo = await usar_convite(codigo, "telegram", "555000111")
    assert motivo == "ok"
    assert membro is not None and membro.nome == "Esposa" and membro.papel == "adulto"

    async with Sessao() as s:
        res = await s.execute(
            select(ChannelIdentity).where(ChannelIdentity.external_id == "555000111")
        )
        identidade = res.scalar_one()
        assert identidade.member_id == membro.id


async def test_crianca_nao_convida():
    crianca = await _membro("FilhoConvite", "crianca")
    assert await criar_convite(crianca, "Amigo", "adulto") is None


async def test_convidado_nao_escolhe_papel():
    """Quem convida decide: convite de criança cadastra criança."""
    admin = await _membro("AdminPapel", "adulto")
    codigo = await criar_convite(admin, "Sobrinho", "crianca")
    membro, motivo = await usar_convite(codigo, "telegram", "555000222")
    assert motivo == "ok" and membro.papel == "crianca"


async def test_codigo_invalido():
    membro, motivo = await usar_convite("XXXXXX", "telegram", "555000333")
    assert membro is None and motivo == "codigo_invalido"


async def test_codigo_nao_reutiliza():
    admin = await _membro("AdminReuso", "adulto")
    codigo = await criar_convite(admin, "Primo", "adulto")
    _, motivo1 = await usar_convite(codigo, "telegram", "555000444")
    assert motivo1 == "ok"
    membro, motivo2 = await usar_convite(codigo, "telegram", "555000555")
    assert membro is None and motivo2 == "ja_usado"


async def test_codigo_expirado():
    admin = await _membro("AdminExpira", "adulto")
    codigo = await criar_convite(admin, "Atrasado", "adulto")
    async with Sessao() as s:
        res = await s.execute(select(InviteCode).where(InviteCode.codigo == codigo))
        convite = res.scalar_one()
        convite.expira_em = datetime.now(UTC) - timedelta(days=1)
        await s.commit()
    membro, motivo = await usar_convite(codigo, "telegram", "555000666")
    assert membro is None and motivo == "expirado"


async def test_ja_cadastrado_nao_queima_o_codigo():
    admin = await _membro("AdminDuplo", "adulto")
    codigo = await criar_convite(admin, "Duplicado", "adulto")
    await usar_convite(codigo, "telegram", "555000777")

    codigo2 = await criar_convite(admin, "Outro", "adulto")
    membro, motivo = await usar_convite(codigo2, "telegram", "555000777")
    assert motivo == "ja_cadastrado"
    assert membro is not None and membro.nome == "Duplicado"  # o cadastro original

    async with Sessao() as s:
        res = await s.execute(select(InviteCode).where(InviteCode.codigo == codigo2))
        assert res.scalar_one().usado_por is None  # código segue disponível


async def test_corrida_mesmo_codigo_so_um_vincula():
    """Código vazou para duas pessoas: exatamente UMA entra; o membro criado
    pelo perdedor não pode sobrar no banco (rollback do claim atômico).

    Ids únicos por execução: o SQLite de teste pode sobreviver entre rodadas
    (OneDrive segura o arquivo no Windows) e external_id fixo colidiria."""
    sufixo = uuid.uuid4().hex[:8]
    admin = await _membro(f"AdminCorrida-{sufixo}", "adulto")
    codigo = await criar_convite(admin, f"Gemeo-{sufixo}", "adulto")

    r1, r2 = await asyncio.gather(
        usar_convite(codigo, "telegram", f"corrida-a-{sufixo}"),
        usar_convite(codigo, "telegram", f"corrida-b-{sufixo}"),
    )
    motivos = sorted([r1[1], r2[1]])
    assert motivos.count("ok") == 1  # exatamente um vencedor
    assert motivos[0] == "ja_usado"

    async with Sessao() as s:
        res = await s.execute(select(Member).where(Member.nome == f"Gemeo-{sufixo}"))
        assert len(list(res.scalars())) == 1  # zero membro-fantasma


async def test_toque_duplo_da_mesma_pessoa_nao_estoura_nem_duplica():
    """A mesma pessoa manda /vincular duas vezes ao mesmo tempo. O contrato
    duro: nenhuma exceção vaza (IntegrityError vira resposta), só UM membro é
    criado e exatamente uma chamada devolve "ok". O motivo do perdedor varia
    com o timing do commit do vencedor (ja_cadastrado ou ja_usado) — ambos
    são recusas educadas; não fixamos qual."""
    sufixo = uuid.uuid4().hex[:8]
    admin = await _membro(f"AdminDuplo-{sufixo}", "adulto")
    codigo = await criar_convite(admin, f"Apressado-{sufixo}", "adulto")

    r1, r2 = await asyncio.gather(
        usar_convite(codigo, "telegram", f"duplo-{sufixo}"),
        usar_convite(codigo, "telegram", f"duplo-{sufixo}"),
    )
    motivos = sorted([r1[1], r2[1]])
    assert motivos.count("ok") == 1
    assert motivos[0] in ("ja_cadastrado", "ja_usado")

    async with Sessao() as s:
        res = await s.execute(select(Member).where(Member.nome == f"Apressado-{sufixo}"))
        assert len(list(res.scalars())) == 1  # toque duplo NÃO cria dois membros