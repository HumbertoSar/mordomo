"""ADR-008: detecção de menção e a troca de thread/sessão em grupo."""

from mordomo.channels.contract import InboundMessage
from mordomo.channels.telegram import _direcionado_ao_bot
from mordomo.observability import config_invocacao, session_de_grupo


def test_mencao_ativa_e_e_removida_do_texto():
    ok, texto = _direcionado_ao_bot(
        "@Alfred_Sardenberg_bot o que temos sábado?", "alfred_sardenberg_bot", False
    )
    assert ok and texto == "o que temos sábado?"


def test_sem_mencao_e_sem_reply_fica_quieto():
    ok, _ = _direcionado_ao_bot("gente, que horas é o almoço?", "alfred_sardenberg_bot", False)
    assert not ok


def test_reply_ao_bot_ativa_sem_mencao():
    ok, texto = _direcionado_ao_bot("e no domingo?", "alfred_sardenberg_bot", True)
    assert ok and texto == "e no domingo?"


def test_username_parecido_nao_ativa():
    """@alfred_sardenberg_bot2 é OUTRO bot — o \\b do regex protege."""
    ok, _ = _direcionado_ao_bot("@alfred_sardenberg_bot2 oi", "alfred_sardenberg_bot", False)
    assert not ok


def test_config_de_grupo_troca_thread_e_sessao():
    cfg = config_invocacao(4, "Davi", "adulto", "t-g1", grupo_id="777")
    c = cfg["configurable"]
    assert c["thread_id"] == "grupo-777"
    assert c["session_id"] == session_de_grupo("777")
    assert c["member_id"] == 4  # a identidade continua individual (ADR-003)
    assert c["grupo_id"] == "777"  # nós leem daqui que a conversa é coletiva
    assert cfg["metadata"]["langfuse_session_id"] == c["session_id"]


def _cfg_grupo(membro, turn: str, grupo: str = "777") -> dict:
    return {"configurable": {"member_id": membro.id, "member_papel": membro.papel,
                             "session_id": f"g{grupo}:teste", "turn_id": turn,
                             "grupo_id": grupo}}


async def test_cofre_em_grupo_mostra_so_o_compartilhado():
    """Decisão de produto (11/08): o grupo da família VÊ o cofre da família —
    mas item "só pra mim" não aparece num chat coletivo (fica pro privado).
    O filtro é determinístico nas tools (grupo_id do configurable), não prompt."""
    from apoio import cfg_de, criar_membro
    from mordomo.tools.cofre import buscar_info, guardar_info, listar_cofre

    m = await criar_membro("GrupoCofreDono")
    await guardar_info.ainvoke(
        {"chave": "cep da casa da praia", "valor": "11111-000"}, cfg_de(m, "t-gc1")
    )
    await guardar_info.ainvoke(
        {"chave": "cep do consultório secreto", "valor": "22222-000", "so_para_mim": True},
        cfg_de(m, "t-gc2"),
    )

    # No GRUPO: o compartilhado aparece; o "só pra mim" nem para o próprio dono
    r = await buscar_info.ainvoke({"termo": "cep"}, _cfg_grupo(m, "t-gc3"))
    assert "11111-000" in r
    assert "22222-000" not in r
    r = await listar_cofre.ainvoke({}, _cfg_grupo(m, "t-gc4"))
    assert "casa da praia" in r and "consultório secreto" not in r

    # No PRIVADO: o dono continua vendo os dois
    r = await buscar_info.ainvoke({"termo": "cep"}, cfg_de(m, "t-gc5"))
    assert "11111-000" in r and "22222-000" in r


async def test_documento_privado_nao_aparece_no_grupo():
    from apoio import cfg_de, criar_membro
    from mordomo.db.models import Document
    from mordomo.db.session import Sessao
    from mordomo.tools.cofre import listar_documentos

    m = await criar_membro("GrupoDocDono")
    async with Sessao() as s:
        s.add(Document(nome="exame reservado", dono=m.id, dados=b"x", tamanho=1,
                       compartilhado=False))
        s.add(Document(nome="carteirinha da família", dono=m.id, dados=b"x", tamanho=1))
        await s.commit()

    r = await listar_documentos.ainvoke({}, _cfg_grupo(m, "t-gc6"))
    assert "carteirinha da família" in r and "exame reservado" not in r
    r = await listar_documentos.ainvoke({}, cfg_de(m, "t-gc7"))
    assert "exame reservado" in r  # no privado o dono vê o dele


def test_config_privada_continua_por_membro():
    cfg = config_invocacao(4, "Davi", "adulto", "t-g2")
    assert cfg["configurable"]["thread_id"] == "membro-4"


def test_inbound_sem_grupo_por_padrao():
    from datetime import UTC, datetime

    m = InboundMessage(member_id=1, canal="telegram", texto="oi", message_id="1",
                       timestamp=datetime.now(UTC))
    assert m.grupo_id is None
