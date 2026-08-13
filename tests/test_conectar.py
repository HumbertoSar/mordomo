"""/conectar — anexar um canal novo ao membro que JÁ existe.

Por que este arquivo existe: sem ele, quem usa o mordomo no Telegram e se
"vincula" no WhatsApp vira DUAS pessoas para o sistema — lembretes, cofre,
agenda e histórico de conversa ficam com o membro velho, e o novo canal fala
com um estranho de mesmo nome. É o buraco entre a promessa do ADR-003
("thread = membro, a memória sobrevive à troca de canal") e o código, que só
sabia criar membro.

O que se prova aqui:
  - conectar NÃO cria membro novo (o teste que justifica a feature)
  - o mesmo código não serve duas vezes, nem para duas pessoas ao mesmo tempo
  - código de conexão expira em minutos, não em dias
  - o convite normal (/vincular) continua criando membro, como sempre
"""

import asyncio
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from sqlalchemy import func, select

from apoio import criar_membro
from mordomo import convites
from mordomo.channels import comandos
from mordomo.db.models import ChannelIdentity, InviteCode, Member, ProductEvent
from mordomo.db.session import Sessao


def _wa() -> str:
    """external_id único por execução (banco compartilhado na sessão)."""
    return "5521" + uuid4().int.__str__()[:9]


async def _total_membros() -> int:
    async with Sessao() as s:
        return (await s.execute(select(func.count()).select_from(Member))).scalar_one()


async def _identidades(member_id: int) -> set[str]:
    async with Sessao() as s:
        res = await s.execute(
            select(ChannelIdentity.canal).where(ChannelIdentity.member_id == member_id)
        )
        return set(res.scalars())


async def test_conectar_anexa_canal_sem_criar_membro_novo():
    """O TESTE que justifica a feature inteira."""
    membro = await criar_membro("ConectaMesmo")
    async with Sessao() as s:
        s.add(ChannelIdentity(member_id=membro.id, canal="telegram", external_id=f"tg{membro.id}"))
        await s.commit()

    antes = await _total_membros()
    codigo = await convites.criar_codigo_de_conexao(membro)
    conectado, motivo = await convites.usar_convite(codigo, "whatsapp", _wa())

    assert motivo == "conectado"
    assert conectado.id == membro.id                 # MESMO membro, não um clone
    assert await _total_membros() == antes           # nenhum membro criado
    assert await _identidades(membro.id) == {"telegram", "whatsapp"}


async def test_vincular_comum_continua_criando_membro():
    """A feature nova não pode ter quebrado o onboarding de quem chega."""
    adulto = await criar_membro("ConectaAnfitriao")
    antes = await _total_membros()
    codigo = await convites.criar_convite(adulto, "Gente Nova", "adulto")
    novo, motivo = await convites.usar_convite(codigo, "whatsapp", _wa())

    assert motivo == "ok"
    assert novo.id != adulto.id
    assert await _total_membros() == antes + 1


async def test_codigo_de_conexao_e_de_uso_unico():
    membro = await criar_membro("ConectaUmaVez")
    codigo = await convites.criar_codigo_de_conexao(membro)

    _, primeiro = await convites.usar_convite(codigo, "whatsapp", _wa())
    _, segundo = await convites.usar_convite(codigo, "whatsapp", _wa())

    assert primeiro == "conectado"
    assert segundo == "ja_usado"


async def test_duas_pessoas_com_o_mesmo_codigo_ao_mesmo_tempo():
    """Corrida: o código dá acesso à conta — só um pode vencer.

    O UPDATE condicional de `usar_convite` é a garantia; aqui provamos que ela
    vale também no caminho de CONEXÃO, que não passa por criar Member."""
    membro = await criar_membro("ConectaCorrida")
    codigo = await convites.criar_codigo_de_conexao(membro)

    resultados = await asyncio.gather(
        convites.usar_convite(codigo, "whatsapp", _wa()),
        convites.usar_convite(codigo, "whatsapp", _wa()),
    )
    motivos = sorted(m for _, m in resultados)
    assert motivos == ["conectado", "ja_usado"]
    assert len(await _identidades(membro.id)) == 1     # uma identidade só entrou


async def test_codigo_de_conexao_expira_em_minutos():
    """Vale minutos, não os 7 dias do convite: quem tem o código VIRA você."""
    membro = await criar_membro("ConectaExpira")
    codigo = await convites.criar_codigo_de_conexao(membro)

    async with Sessao() as s:
        res = await s.execute(select(InviteCode).where(InviteCode.codigo == codigo))
        convite = res.scalar_one()
        # a validade nasce curta…
        assert convite.expira_em - datetime.now(UTC) < timedelta(hours=1)
        # …e vencida, o código não vale
        convite.expira_em = datetime.now(UTC) - timedelta(minutes=1)
        await s.commit()

    _, motivo = await convites.usar_convite(codigo, "whatsapp", _wa())
    assert motivo == "expirado"


async def test_conectar_no_canal_onde_ja_e_conhecido_nao_desperdica_codigo():
    membro = await criar_membro("ConectaJaConhecido")
    wa_id = _wa()
    async with Sessao() as s:
        s.add(ChannelIdentity(member_id=membro.id, canal="whatsapp", external_id=wa_id))
        await s.commit()

    codigo = await convites.criar_codigo_de_conexao(membro)
    encontrado, motivo = await convites.usar_convite(codigo, "whatsapp", wa_id)
    assert motivo == "ja_cadastrado"
    assert encontrado.id == membro.id


# ── O comando, do jeito que a família usa ────────────────────────────────


async def test_comando_conectar_gera_codigo_e_o_outro_canal_consome():
    membro = await criar_membro("ConectaFluxo")
    tg = f"tg{uuid4().hex[:8]}"
    async with Sessao() as s:
        s.add(ChannelIdentity(member_id=membro.id, canal="telegram", external_id=tg))
        await s.commit()

    # 1) no canal onde já é conhecido: /conectar sem argumento gera o código
    resposta = await comandos.responder("telegram", tg, "/conectar")
    assert "/conectar " in resposta
    codigo = resposta.split("/conectar ")[1].split()[0]

    # 2) no canal novo, onde é um desconhecido: o código o reencontra
    wa_id = _wa()
    resposta = await comandos.responder("whatsapp", wa_id, f"/conectar {codigo}")
    assert "Reencontrei você" in resposta
    assert await _identidades(membro.id) == {"telegram", "whatsapp"}


async def test_conectar_sem_ser_membro_explica_o_caminho():
    resposta = await comandos.responder("whatsapp", _wa(), "/conectar")
    assert "canal em que já falo com você" in resposta


async def test_conectar_em_grupo_e_recusado():
    """O código é chave da conta — no grupo, qualquer um copiaria (ADR-008)."""
    membro = await criar_membro("ConectaGrupo")
    tg = f"tg{uuid4().hex[:8]}"
    async with Sessao() as s:
        s.add(ChannelIdentity(member_id=membro.id, canal="telegram", external_id=tg))
        await s.commit()

    resposta = await comandos.responder("telegram", tg, "/conectar", privado=False)
    assert "privado" in resposta.lower()


async def test_conexao_emite_analytics_sem_virar_orfao():
    from mordomo.reporting import queries

    membro = await criar_membro("ConectaAnalytics")
    desde = datetime.now(UTC) - timedelta(minutes=5)
    orfaos_antes = await queries.orfaos(desde)

    codigo = await convites.criar_codigo_de_conexao(membro)
    await convites.usar_convite(codigo, "whatsapp", _wa())

    async with Sessao() as s:
        res = await s.execute(
            select(ProductEvent.tipo).where(
                ProductEvent.member_id == membro.id,
                ProductEvent.tipo.in_(("connect_created", "connect_used")),
            )
        )
        tipos = set(res.scalars())
    assert tipos == {"connect_created", "connect_used"}
    # nascem fora de turno POR DESENHO — não podem inflar o KPI de órfãos
    assert await queries.orfaos(desde) == orfaos_antes
