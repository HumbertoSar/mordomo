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
    assert cfg["metadata"]["langfuse_session_id"] == c["session_id"]


def test_config_privada_continua_por_membro():
    cfg = config_invocacao(4, "Davi", "adulto", "t-g2")
    assert cfg["configurable"]["thread_id"] == "membro-4"


def test_inbound_sem_grupo_por_padrao():
    from datetime import UTC, datetime

    m = InboundMessage(member_id=1, canal="telegram", texto="oi", message_id="1",
                       timestamp=datetime.now(UTC))
    assert m.grupo_id is None
