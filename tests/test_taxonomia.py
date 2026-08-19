"""Taxonomia: projetar os eventos que já existem em dimensões estáveis.

A alternativa seria reescrever as 19 tools para carimbar capacidade/operação em
cada payload. Esta camada é de LEITURA: o histórico inteiro ganha as dimensões
sem migração e sem tocar na conversa."""

from mordomo.reporting import taxonomia


def _todas_as_tools():
    from mordomo.tools.agenda import TOOLS_AGENDA
    from mordomo.tools.cofre import TOOLS_COFRE
    from mordomo.tools.lembretes import TOOLS_LEMBRETES
    from mordomo.tools.tarefas import TOOLS_TAREFAS

    return [*TOOLS_AGENDA, *TOOLS_COFRE, *TOOLS_LEMBRETES, *TOOLS_TAREFAS]


def test_toda_tool_do_sistema_tem_classificacao():
    """Tool nova sem classificação quebra AQUI — e não em silêncio no painel,
    onde ela simplesmente não apareceria em capacidade nenhuma."""
    nomes = {t.name for t in _todas_as_tools()}

    sem_classificacao = nomes - set(taxonomia.TOOLS)
    assert sem_classificacao == set(), f"tools sem capacidade/operação: {sem_classificacao}"


def test_toda_tool_classificada_projeta_capacidade_e_operacao():
    for tool in _todas_as_tools():
        p = taxonomia.projetar("tool_result", {"tool": tool.name, "ok": True})
        assert p is not None, tool.name
        assert p.capability and p.operation
        assert p.kind in {"read", "write", "delivery", "system"}


def test_capacidades_cobrem_o_sistema_inteiro():
    capacidades = {op.capability for op in taxonomia.TOOLS.values()}
    assert {"agenda", "tarefas", "lembretes", "cofre", "documentos"} <= capacidades


def test_agenda_separa_preparar_confirmar_descartar_e_leitura():
    def op(tool):
        return taxonomia.projetar("tool_called", {"tool": tool})

    assert op("preparar_evento").operation == "preparar_criar"
    assert op("confirmar_evento").operation == "confirmar_criar"
    assert op("descartar_evento").operation == "descartar"
    assert op("listar_agenda").kind == "read"
    assert op("consultar_agenda").kind == "read"
    # preparar e descartar gravam (a proposta), mas nenhum dos dois é resultado
    # para a família: quem entrega valor é a confirmação.
    assert op("preparar_evento").prova is None
    assert op("descartar_evento").prova is None
    assert op("confirmar_evento").prova == "efeito_externo"
    assert op("listar_agenda").prova == "leitura_entregue"


def test_dependencia_da_agenda_vem_do_destino_comprovado():
    google = taxonomia.projetar("tool_result", {"tool": "confirmar_evento", "ok": True,
                                                "destino": "google"})
    nativo = taxonomia.projetar("tool_result", {"tool": "confirmar_evento", "ok": True,
                                                "destino": "nativo"})
    sem_destino = taxonomia.projetar("tool_result", {"tool": "confirmar_evento", "ok": True})

    assert google.dependency == "google_calendar"
    assert nativo.dependency == "postgres"
    # "já criei" (sim repetido) não diz onde: dependência não comprovada é None,
    # nunca um chute.
    assert sem_destino.dependency is None


def test_evidencia_distingue_tentativa_sucesso_e_falha():
    assert taxonomia.projetar("tool_called", {"tool": "criar_tarefa"}).evidence == "attempted"
    assert taxonomia.projetar(
        "tool_result", {"tool": "criar_tarefa", "ok": True}
    ).evidence == "succeeded"
    assert taxonomia.projetar(
        "tool_result", {"tool": "criar_tarefa", "ok": False, "motivo": "titulo_vazio"}
    ).evidence == "failed"


def test_jornada_resolvida_tem_evidencia_propria_e_capacidade():
    resolvida = taxonomia.projetar("journey_resolved", {"journey_type": "calendar_create"})
    iniciada = taxonomia.projetar("journey_started", {"journey_type": "calendar_create"})
    abandonada = taxonomia.projetar("journey_abandoned", {"journey_type": "task"})

    assert resolvida is not None and iniciada is not None and abandonada is not None
    assert resolvida.capability == "agenda" and resolvida.evidence == "resolved"
    assert iniciada.capability == "agenda" and iniciada.evidence == "attempted"
    assert abandonada.capability == "tarefas" and abandonada.evidence == "abandoned"


def test_fatos_de_dominio_de_tarefas_sao_classificados_sem_duplicar_tool():
    esperados = {
        "task_created": "criar",
        "task_completed": "concluir",
        "task_cancelled": "cancelar",
        "task_reopened": "reabrir",
    }
    for tipo, operacao in esperados.items():
        projecao = taxonomia.projetar(tipo, {"journey_type": "task"})
        assert projecao is not None, tipo
        assert (projecao.capability, projecao.operation) == ("tarefas", operacao)
        assert taxonomia.coberta_por_tool("tarefas", operacao), tipo


def test_onboarding_convite_vinculo_e_google_oauth():
    assert taxonomia.projetar("invite_created", {}).capability == "onboarding"
    assert taxonomia.projetar("invite_used", {}).evidence == "succeeded"
    assert taxonomia.projetar("connect_used", {}).operation == "conectar"
    oauth = taxonomia.projetar("google_connection_succeeded", {})
    assert oauth.capability == "onboarding" and oauth.dependency == "google_calendar"


def test_canal_projeta_envio_entrega_e_proatividade():
    envio = taxonomia.projetar("message_sent", {"canal": "whatsapp"})
    entrega = taxonomia.projetar("message_status", {"canal": "whatsapp", "status": "read"})
    falha = taxonomia.projetar("message_status", {"canal": "whatsapp", "status": "failed"})
    proativo = taxonomia.projetar("proactive_channel", {"canal": "telegram", "modo": "livre"})

    assert (envio.capability, envio.kind) == ("canal", "delivery")
    assert envio.dependency == "whatsapp"
    assert entrega.evidence == "succeeded" and falha.evidence == "failed"
    assert proativo.operation == "proatividade" and proativo.dependency == "telegram"


def test_sistema_cobre_roteamento_turno_e_erro():
    rot = taxonomia.projetar("orchestrator_decision", {"destino": "agenda"})
    parse = taxonomia.projetar("orchestrator_parse_error", {})
    erro = taxonomia.projetar("error", {"motivo": "timeout_llm"})

    assert rot.capability == "sistema" and rot.dependency == "llm"
    assert parse.evidence == "failed"
    assert erro.kind == "system" and erro.evidence == "failed"


def test_legado_criar_evento_continua_classificado():
    """`criar_evento` sumiu do código (virou preparar + confirmar), mas segue no
    histórico — sem classificação, os eventos antigos desapareceriam do placar."""
    legado = taxonomia.projetar("tool_result", {"tool": "criar_evento", "ok": True})

    assert legado.capability == "agenda"
    assert legado.prova is not None


def test_evento_desconhecido_nao_inventa_capacidade():
    assert taxonomia.projetar("tipo_que_nao_existe", {}) is None
    assert taxonomia.projetar("tool_result", {"tool": "tool_do_futuro", "ok": True}) is None


def test_projecao_nao_carrega_dado_sensivel():
    """A projeção é dimensão, não conteúdo: nada do payload atravessa (ADR-005)."""
    sensiveis = {
        "titulo": "consulta do João",
        "email": "alguem@exemplo.com",
        "local": "Av. Paulista 1000",
        "link": "https://calendar.google.com/event?eid=xyz",
        "token": "ya29.segredo",
        "valor": "senha-do-wifi",
    }
    p = taxonomia.projetar("tool_result", {"tool": "guardar_info", "ok": True, **sensiveis})

    projetado = " ".join(str(v) for v in vars(p).values())
    for proibido in sensiveis.values():
        assert proibido not in projetado
