"""Tools do subagente Agenda.

DESTINO (quem decide é a tool, nunca o LLM — ADR-010):
  - membro com Google Agenda conectado → o evento é criado NO Google Calendar
    (`primary`) e a listagem é lida DE LÁ. Sem conexão não há como o Mordomo
    ser útil na agenda que a pessoa já usa no celular.
  - membro sem conexão → agenda COMPARTILHADA do Mordomo (family_events),
    mantida por compatibilidade e sempre nomeada assim na resposta.

Nunca há fallback silencioso: se o Google falhar para quem está conectado, a
tool diz que NÃO conseguiu e devolve o caminho (tentar de novo / reconectar).
Gravar na agenda local nesse caso seria repetir o incidente de 16/08/2026 —
"evento criado", nada no calendário da pessoa."""

import re
import uuid
from datetime import UTC, datetime, timedelta
from functools import partial
from zoneinfo import ZoneInfo

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import tool
from sqlalchemy import select

from .. import propostas
from ..analytics import emitir_de, evento_de
from ..config import settings
from ..db.models import FamilyEvent
from ..db.session import Sessao
from ..integracoes import google
from ._comum import DIAS_DA_SEMANA, fmt_data, resolver_ou_instruir
from .datas import resolver_data, resolver_intervalo
from .periodos import resolver_janela

_fmt = partial(fmt_data, dia_semana=True)  # agenda mostra o dia da semana

# Quanto dura um compromisso quando ninguém disse até que horas. Uma hora é o
# padrão do próprio Google Calendar — o valor é arbitrário, então o que importa
# é ser previsível e estar escrito (aqui e no prompt do subagente).
DURACAO_PADRAO_MINUTOS = 60

# Teto TOTAL de eventos de uma consulta (não de página — a leitura pagina até
# fechar a janela). Alcançar o teto não é "acabou": a resposta declara que
# ficou incompleta, senão "não encontrei" vira mentira.
MAX_EVENTOS = 100
MAX_DIAS = 30

# Quantos eventos cabem numa resposta de chat antes de virar parede de texto.
# O que passar disso é contado, não listado.
MAX_LINHAS = 15


def _agora() -> datetime:
    """O relógio da agenda em UM lugar — é o que os testes congelam."""
    return datetime.now(UTC)


AGENDA_NATIVA = "agenda compartilhada do Mordomo"
AGENDA_GOOGLE = "Google Agenda"


# ── término (o "das 12h às 16h") ─────────────────────────────────────────


def _resolver_fim(ate: str, inicio: datetime) -> tuple[datetime | None, str]:
    """Expressão de término → datetime. Devolve (fim, motivo).

    Duas leituras, nesta ordem:
      1. expressão completa ("amanhã às 16h") — resolvida como qualquer data;
      2. expressão só de hora ("16h", "às 16h") — o dia é o do INÍCIO.

    A releitura (2) só vale se cair no MESMO dia do início. Sem essa trava,
    "amanhã às 10h" com início amanhã ao meio-dia viraria depois de amanhã às
    10h — um evento de 22 horas em vez do erro que ele é."""
    fim = resolver_data(ate)
    if fim is not None and fim > inicio:
        return fim, "ok"
    releitura = resolver_data(ate, base=inicio)
    if releitura is not None and releitura > inicio and _mesmo_dia(releitura, inicio):
        return releitura, "ok"
    if fim is None and releitura is None:
        return None, "termino_nao_entendido"
    return None, "termino_antes_do_inicio"


def _mesmo_dia(a: datetime, b: datetime) -> bool:
    fuso = ZoneInfo(settings.tz_familia)
    return a.astimezone(fuso).date() == b.astimezone(fuso).date()


_INSTRUCAO_DE_TERMINO = {
    "termino_nao_entendido": (
        "NÃO ENTENDI até que horas vai ({ate}). Pergunte ao usuário o horário "
        "de término (não invente!)."
    ),
    "termino_antes_do_inicio": (
        "O término ({ate}) ficou ANTES do início. Confirme com o usuário a que "
        "horas o compromisso termina."
    ),
}


def _intervalo(inicio: datetime, fim: datetime | None) -> str:
    """"Mon 17/08 às 12:00 até 16:00" — e com a data repetida se virar o dia."""
    if fim is None:
        return _fmt(inicio)
    if _mesmo_dia(fim, inicio):
        hora = fim.astimezone(ZoneInfo(settings.tz_familia)).strftime("%H:%M")
        return f"{_fmt(inicio)} até {hora}"
    return f"{_fmt(inicio)} até {_fmt(fim)}"


# ── preparar (resolve tudo, NÃO grava em agenda nenhuma) ─────────────────


@tool
async def preparar_evento(
    titulo: str,
    quando: str,
    ate: str | None,
    local: str | None,
    convidados: list[str] | None,
    com_meet: bool,
    config: RunnableConfig,
) -> str:
    """PREPARA um compromisso e devolve o resumo para o usuário confirmar.

    NÃO cria nada: quem cria é `confirmar_evento`, depois do "sim". Use esta
    tool mesmo quando a frase já vier completa.

    Args:
        titulo: ex. "consulta pediatra do João".
        quando: quando COMEÇA — expressão de tempo em português, exatamente
            como o usuário disse (ex. "amanhã às 12h").
        ate: quando TERMINA, se o usuário disse ("amanhã às 16h", ou só "16h").
            Use null quando ele não disse: o compromisso dura 1 hora.
        local: opcional (ex. "clínica do centro").
        convidados: e-mails de quem deve ser convidado, se o usuário deu algum.
            Só endereços de e-mail completos; use null quando não houver.
        com_meet: true só se o usuário pediu link de videochamada/Google Meet.
    """
    member_id = config["configurable"]["member_id"]
    await emitir_de(
        config,
        "tool_called",
        tool="preparar_evento",
        quando=quando,
        com_termino=bool(ate),
        n_convidados=len(convidados or []),
        com_meet=bool(com_meet),
    )
    # Quando o fim está dentro de `quando`, valide o intervalo antes do resolvedor
    # genérico. Assim "das 20h às 19h" recebe a orientação sobre o TÉRMINO, sem
    # transformar o começo num instante válido para outros consumidores.
    intervalo = resolver_intervalo(quando) if not ate else None
    if intervalo is not None and intervalo.motivo in _INSTRUCAO_DE_TERMINO:
        await emitir_de(
            config,
            "tool_result",
            tool="preparar_evento",
            ok=False,
            motivo=intervalo.motivo,
        )
        return _INSTRUCAO_DE_TERMINO[intervalo.motivo].format(ate=quando)

    if intervalo is not None and intervalo.motivo == "ok":
        inicio, fim = intervalo.inicio, intervalo.fim
    else:
        inicio, instrucao = await resolver_ou_instruir(quando, config, "preparar_evento")
        if instrucao:
            return instrucao
        assert inicio is not None  # sucesso do contrato de resolver_ou_instruir
        fim, motivo = _resolver_fim(ate, inicio) if ate else (None, "sem_termino")
        if fim is None:
            if motivo in _INSTRUCAO_DE_TERMINO:
                await emitir_de(
                    config, "tool_result", tool="preparar_evento", ok=False, motivo=motivo
                )
                return _INSTRUCAO_DE_TERMINO[motivo].format(ate=ate)
            fim = inicio + timedelta(minutes=DURACAO_PADRAO_MINUTOS)

    assert inicio is not None and fim is not None
    convidados = [c.strip() for c in (convidados or []) if c and c.strip()]
    if invalidos := [c for c in convidados if not _EMAIL.fullmatch(c)]:
        await emitir_de(
            config,
            "tool_result",
            tool="preparar_evento",
            ok=False,
            motivo="convidado_sem_email",
        )
        return (
            f"NÃO consegui usar {len(invalidos)} convidado(s): o Google convida por "
            "E-MAIL, e o que veio não é um endereço. Peça ao usuário o e-mail de "
            "quem ele quer convidar (ou confirme sem convidados)."
        )

    conectado = await _conexao_google(member_id) is not None
    destino = "google" if conectado else "nativo"
    conflitos, incerto = (
        await _conflitos_no_google(member_id, inicio, fim) if conectado else ([], False)
    )

    # A JORNADA de criar um compromisso (ADR-010): ela nasce aqui e só fecha no
    # "sim", que vem em OUTRO turno — às vezes horas depois. O id fica gravado
    # na proposta porque é ela o único elo entre os dois turnos.
    configuravel = config.get("configurable", {})
    journey_id = configuravel.get("journey_id") or uuid.uuid4().hex
    # Carga logística só quando há outra pessoa envolvida: convidar é
    # coordenação, marcar sozinho é só memória.
    cargas = ["mental", "logistics"] if convidados else ["mental"]
    proposta = await propostas.guardar(
        member_id,
        titulo=titulo,
        inicio=inicio,
        fim=fim,
        local=local,
        convidados=convidados,
        com_meet=bool(com_meet),
        destino=destino,
        turn_id=configuravel.get("turn_id"),
        journey_id=journey_id,
        # Mesma transação da proposta: jornada iniciada sem proposta (ou o
        # contrário) deixaria o funil descrevendo algo que não existe. Quando a
        # jornada já veio do configurable, não começamos uma segunda.
        eventos=(
            []
            if configuravel.get("journey_id")
            else [
                evento_de(
                    config,
                    "journey_started",
                    journey_id=journey_id,
                    journey_type="calendar_create",
                    loads=cargas,
                )
            ]
        ),
    )
    await emitir_de(
        config,
        "tool_result",
        journey_id=journey_id,
        tool="preparar_evento",
        ok=True,
        destino=destino,
        duracao_min=int((fim - inicio).total_seconds() // 60),
        n_convidados=len(convidados),
        com_meet=bool(com_meet),
        n_conflitos=len(conflitos),
        conflito_incerto=incerto,
    )
    return _resumo_para_confirmar(proposta, conflitos, incerto)


# E-mail "bom o bastante": o que o Google aceita como convidado. Não validamos
# domínio nem existência — só evitamos mandar "o Xiao" no `attendees`, que faz
# a criação inteira falhar DEPOIS do sim.
_EMAIL = re.compile(r"[^@\s]+@[^@\s.]+\.[^@\s]+")


def _resumo_para_confirmar(proposta, conflitos: list[dict], incerto: bool) -> str:
    """O texto que o usuário confere antes de dizer sim.

    Mostra TUDO o que vai ser executado — inclusive o que NÃO vai (a agenda do
    Mordomo não convida ninguém). Prometer no resumo o que a execução não faz é
    exatamente o defeito de 17/08/2026, só que mais cedo."""
    onde = AGENDA_GOOGLE if proposta.destino == "google" else AGENDA_NATIVA
    linhas = [
        f"Confira antes de eu marcar (na {onde}):",
        f"• {proposta.titulo}",
        f"• {_intervalo(proposta.inicio_utc, proposta.fim_utc)}",
    ]
    if proposta.local:
        linhas.append(f"• Local: {proposta.local}")
    if proposta.convidados:
        linhas.append(f"• Convidados: {', '.join(proposta.convidados)}")
    if proposta.com_meet:
        linhas.append("• Com link do Google Meet")
    if proposta.destino == "nativo" and (proposta.convidados or proposta.com_meet):
        linhas.append(
            "ATENÇÃO: esta pessoa NÃO tem Google Agenda conectado — a agenda "
            "compartilhada do Mordomo não manda convite nem cria Meet. Diga isso "
            "com todas as letras e ofereça /google para conectar."
        )
    if conflitos:
        linhas.append(f"ATENÇÃO, já há {len(conflitos)} compromisso(s) nesse horário:")
        linhas += [f"  - {_linha_de_evento(e)}" for e in conflitos[:3]]
        linhas.append("Mostre o conflito e pergunte se marca mesmo assim (não decida sozinho).")
    elif incerto:
        linhas.append(
            "ATENÇÃO: NÃO consegui conferir se esse horário está livre. Diga isso "
            "ao usuário — não afirme que a agenda está livre."
        )
    linhas.append(
        "Peça a confirmação do usuário. Quando ele confirmar, chame "
        "`confirmar_evento`; se desistir, chame `descartar_evento`."
    )
    return "\n".join(linhas)


# ── conflitos (leitura determinística, antes do sim) ─────────────────────


async def _conflitos_no_google(
    member_id: int, inicio: datetime, fim: datetime
) -> tuple[list[dict], bool]:
    """(compromissos que se sobrepõem, "a leitura foi incompleta?").

    A checagem é do CÓDIGO, não do LLM: o modelo não tem como saber o que há na
    agenda e, quando chuta, chuta "está livre". Leitura falha ou truncada volta
    como incerteza declarada — nunca como agenda vazia."""
    resultado = await google.listar_eventos_da_agenda(
        member_id, inicio=inicio, fim=fim, teto=MAX_EVENTOS
    )
    if not resultado["ok"]:
        return [], True
    conflitos = [e for e in resultado["eventos"] if _sobrepoe(e, inicio, fim)]
    return sorted(conflitos, key=lambda e: e["inicio"]), bool(resultado["truncado"])


def _sobrepoe(evento: dict, inicio: datetime, fim: datetime) -> bool:
    """Sobreposição REAL de intervalos meio-abertos: encostar não é conflito.

    Um evento que termina 09:00 não atrapalha outro que começa 09:00 — tratar
    isso como conflito encheria a preparação de alarme falso. Evento de dia
    inteiro entra normalmente: o `end.date` do Google já o descreve como
    intervalo (e é ele que cobre o dia)."""
    comeco = evento["inicio"]
    termino = evento["fim"] or comeco
    return comeco < fim and termino > inicio


async def _conexao_google(member_id: int):
    """A conexão do membro, exista ou não configuração Google neste processo.

    Linha EXISTENTE porém ilegível (chave rotacionada) NÃO vira None de
    propósito: quem conectou tem que ouvir "reconecte", não ver o compromisso
    aparecer calado noutra agenda. O mesmo vale para configuração temporariamente
    ausente: a operação falha declaradamente, sem cair na agenda nativa."""
    return await google.conexao_de(member_id)


# ── confirmar (o único caminho que grava) ────────────────────────────────


@tool
async def confirmar_evento(codigo: str | None, config: RunnableConfig) -> str:
    """CONFIRMA o compromisso preparado e o cria de verdade.

    Chame só depois de o usuário confirmar o resumo de `preparar_evento`.

    Args:
        codigo: deixe null. Só preencha se eu tiver perguntado QUAL dos
            compromissos preparados o usuário quer confirmar.
    """
    member_id = config["configurable"]["member_id"]
    await emitir_de(config, "tool_called", tool="confirmar_evento", com_codigo=bool(codigo))

    proposta, motivo = await propostas.reivindicar(member_id, codigo)
    if motivo == "ja_usada":
        if proposta is not None and proposta.concluido_em is None:
            # `usado_em` é adquirido antes da chamada externa. Nesse intervalo,
            # a trava prova apenas que há outro trabalhador — não que já criou.
            await emitir_de(
                config,
                "tool_result",
                tool="confirmar_evento",
                ok=False,
                motivo="em_processamento",
            )
            return (
                "Esse compromisso AINDA ESTÁ sendo processado. Não vou duplicar; "
                "peça ao usuário para aguardar um instante e consultar a agenda."
            )
        # O "sim" repetido depois da conclusão — o evento já existe. Isto é
        # sucesso: recriar seria a duplicata que a trava existe para impedir.
        await emitir_de(
            config, "tool_result", tool="confirmar_evento", ok=True, motivo="ja_criado", novo=False
        )
        ver = f"\nVer: {proposta.link}" if proposta and proposta.link else ""
        return (
            "Esse compromisso eu JÁ criei — é o mesmo, não vou duplicar. "
            f"Diga isso ao usuário.{ver}"
        )
    if proposta is None:
        await emitir_de(
            config, "tool_result", tool="confirmar_evento", ok=False, motivo=motivo
        )
        return _INSTRUCAO_SEM_PROPOSTA[motivo]

    if proposta.destino == "nativo":
        return await _gravar_na_agenda_nativa(config, proposta)
    return await _gravar_no_google(config, proposta)


_INSTRUCAO_SEM_PROPOSTA = {
    "ausente": (
        "NÃO há nenhum compromisso preparado para confirmar. NÃO invente que "
        "marcou nada: pergunte ao usuário o que ele quer marcar e prepare de novo."
    ),
    "expirada": (
        "O compromisso preparado EXPIROU (faz tempo demais). NÃO afirme que "
        "marcou: confirme os dados com o usuário e prepare de novo."
    ),
    "ambigua": (
        "Há MAIS DE UM compromisso preparado e eu não sei qual foi confirmado. "
        "Pergunte ao usuário qual deles ele quer marcar — não escolha por ele."
    ),
    "de_outro_membro": (
        "Esse compromisso preparado NÃO é desta pessoa e eu não vou criá-lo. "
        "Pergunte o que ELA quer marcar e prepare de novo."
    ),
}


async def _gravar_no_google(config, proposta) -> str:
    resultado = await google.criar_evento_na_agenda(
        proposta.member_id,
        titulo=proposta.titulo,
        inicio=proposta.inicio_utc,
        fim=proposta.fim_utc,
        local=proposta.local,
        convidados=list(proposta.convidados or []),
        com_meet=bool(proposta.com_meet),
        # A trava contra a duplicata: o id do evento nasce do CÓDIGO da
        # proposta, que é o mesmo em todo retry — e muda a cada pedido novo.
        chave=proposta.codigo,
    )
    if not resultado["ok"]:
        # A proposta volta a valer: o evento não existe, e queimá-la deixaria o
        # usuário sem compromisso e sem como repetir o "sim".
        assert proposta.usado_em is not None
        await propostas.devolver(proposta.id, reivindicada_em=proposta.usado_em)
        await emitir_de(
            config,
            "tool_result",
            tool="confirmar_evento",
            ok=False,
            destino="google",
            motivo=resultado["motivo"],
        )
        return _FALHA_AO_CRIAR.get(resultado["motivo"], _FALHA_GENERICA_AO_CRIAR)

    # O efeito externo está confirmado: só AGORA a jornada pode fechar.
    concluida = await propostas.concluir(
        proposta.id, link=resultado.get("link"), eventos=_resolucao(config, proposta)
    )
    if not concluida:
        await emitir_de(
            config,
            "tool_result",
            journey_id=proposta.journey_id,
            tool="confirmar_evento",
            ok=True,
            motivo="ja_criado",
            novo=False,
        )
        return "Esse compromisso eu JÁ criei — não vou duplicar. Diga isso ao usuário."
    convidados_aceitos = resultado.get("convidados_aceitos") or []
    meet = resultado.get("meet_link")
    await emitir_de(
        config,
        "tool_result",
        journey_id=proposta.journey_id,
        tool="confirmar_evento",
        ok=True,
        destino="google",
        novo=resultado["novo"],
        duracao_min=_duracao_min(proposta),
        n_convidados_pedidos=len(proposta.convidados or []),
        n_convidados_aceitos=len(convidados_aceitos),
        meet_pedido=bool(proposta.com_meet),
        meet_criado=bool(meet),
    )
    repetido = "" if resultado["novo"] else " (já estava lá — não dupliquei)"
    return (
        f"Evento criado no {AGENDA_GOOGLE}: {proposta.titulo} — "
        f"{_intervalo(proposta.inicio_utc, proposta.fim_utc)}"
        f"{_sufixo_local(proposta.local)}{repetido}"
        + _relato_de_convidados(proposta, convidados_aceitos)
        + _relato_do_meet(proposta, meet)
        + (f"\nVer: {resultado['link']}" if resultado.get("link") else "")
    )


def _relato_de_convidados(proposta, aceitos: list[str]) -> str:
    """O que dizer sobre o convite — e só o que o Google confirmou.

    O incidente: "convidei fulano" com o evento nascendo sem attendee nenhum.
    A resposta do Calendar traz os convidados que ele registrou; o que não
    estiver lá, não foi convidado."""
    pedidos = list(proposta.convidados or [])
    if not pedidos:
        return ""
    if not aceitos:
        return (
            "\nATENÇÃO: o Google NÃO registrou nenhum convidado. Diga que o "
            "compromisso está marcado mas o convite NÃO foi enviado — não afirme "
            "que convidou ninguém."
        )
    faltando = [e for e in pedidos if e not in aceitos]
    if faltando:
        return (
            f"\nATENÇÃO: só {len(aceitos)} de {len(pedidos)} convidados entraram. "
            f"NÃO ficaram: {', '.join(faltando)}. Conte isso ao usuário."
        )
    return f"\nConvidados no evento: {', '.join(aceitos)}."


def _relato_do_meet(proposta, meet: str | None) -> str:
    if not proposta.com_meet:
        return ""
    if not meet:
        return (
            "\nATENÇÃO: o Google NÃO criou o link do Meet. Diga que o compromisso "
            "está marcado mas SEM videochamada — não invente um link."
        )
    return f"\nGoogle Meet: {meet}"


async def _gravar_na_agenda_nativa(config, proposta) -> str:
    async with Sessao() as s:
        ev = FamilyEvent(
            titulo=proposta.titulo,
            inicio_utc=proposta.inicio_utc,
            fim_utc=proposta.fim_utc,
            local=proposta.local,
            criado_por=proposta.member_id,
        )
        s.add(ev)
        await s.flush()
        evento_id = ev.id
        concluida = await propostas.concluir_na_sessao(
            s,
            proposta.id,
            link=f"nativo:{evento_id}",
            agora=datetime.now(UTC),
            eventos=_resolucao(config, proposta),
        )
        if not concluida:
            await s.rollback()
        else:
            await s.commit()
    if not concluida:
        await emitir_de(
            config,
            "tool_result",
            journey_id=proposta.journey_id,
            tool="confirmar_evento",
            ok=True,
            motivo="ja_criado",
            novo=False,
        )
        return "Esse compromisso eu JÁ criei — não vou duplicar. Diga isso ao usuário."
    await emitir_de(
        config,
        "tool_result",
        journey_id=proposta.journey_id,
        tool="confirmar_evento",
        ok=True,
        destino="nativo",
        novo=True,
        evento_id=evento_id,
        duracao_min=_duracao_min(proposta),
    )
    aviso = (
        "\nATENÇÃO: a agenda do Mordomo NÃO manda convite nem cria Meet — diga "
        "isso ao usuário."
        if (proposta.convidados or proposta.com_meet)
        else ""
    )
    return (
        f"Evento criado na {AGENDA_NATIVA}: {proposta.titulo} — "
        f"{_intervalo(proposta.inicio_utc, proposta.fim_utc)}"
        f"{_sufixo_local(proposta.local)}{aviso}"
    )


def _duracao_min(proposta) -> int:
    return int((proposta.fim_utc - proposta.inicio_utc).total_seconds() // 60)


def _resolucao(config, proposta) -> list:
    """O `journey_resolved` da proposta — vazio quando não há jornada.

    Proposta antiga (de antes desta fatia) não tem `journey_id`: ela continua
    criando o compromisso normalmente, só não fecha jornada nenhuma. Nada é
    retrocriado para o histórico."""
    if not proposta.journey_id:
        return []
    return [
        evento_de(
            config,
            "journey_resolved",
            journey_id=proposta.journey_id,
            journey_type="calendar_create",
        )
    ]


@tool
async def descartar_evento(config: RunnableConfig) -> str:
    """DESCARTA o compromisso preparado que ainda não foi confirmado.

    Use quando o usuário desistir ou quiser refazer os dados.
    """
    member_id = config["configurable"]["member_id"]
    await emitir_de(config, "tool_called", tool="descartar_evento")

    def abandonos(jornadas: list) -> list:
        """Uma jornada abandonada por proposta REALMENTE apagada — na mesma
        transação do DELETE. Motivo categórico, nunca texto da conversa."""
        return [
            evento_de(
                config,
                "journey_abandoned",
                journey_id=j,
                journey_type="calendar_create",
                reason="user_discarded",
            )
            for j in jornadas
            if j
        ]

    descartadas = await propostas.descartar(member_id, eventos_por_jornada=abandonos)
    quantos = len(descartadas)
    await emitir_de(
        config, "tool_result", tool="descartar_evento", ok=True, n_descartados=quantos
    )
    if not quantos:
        return "Não havia nada preparado esperando confirmação — nada foi criado."
    return (
        f"Descartei {quantos} compromisso(s) que estavam esperando confirmação. "
        "NÃO foi criado nada."
    )


# ── desistência: executar antes de afirmar ───────────────────────────────
# Canário real de 18/08/2026: com um compromisso preparado, o usuário disse
# "não" e o modelo respondeu "descartado" SEM chamar `descartar_evento`. A
# proposta ficou de pé, e um "sim" posterior — sobre outro assunto — a
# encontraria esperando.
#
# A trava tem DUAS condições, e é a conjunção que a mantém estreita:
#   1. existe proposta pendente deste membro (fato do banco, não do texto);
#   2. a fala é SÓ desistência — todas as palavras saem de um vocabulário
#      fechado. "não precisa avisar o Davi" tem pedido dentro e não passa.
_DESISTENCIA = {
    "não", "nao", "desisti", "desisto", "descarta", "descarte", "descartar",
    "esquece", "esqueça", "esqueca", "cancela", "cancele", "deixa",
}
_ACESSORIAS = {
    "marca", "marque", "marcar", "criar", "cria", "crie", "precisa", "pra", "para",
    "lá", "la", "isso", "agora", "melhor", "nem", "mais", "obrigado", "obrigada",
    "por", "favor", "o", "a", "os", "as", "esse", "essa", "compromisso", "evento",
    "aí", "ai", "todos", "todas", "ambos", "ambas", "dois", "duas",
}
_MAX_PALAVRAS_DE_DESISTENCIA = 5


def _e_desistencia(texto: str) -> bool:
    palavras = re.findall(r"[\wÀ-ÿ]+", texto.lower())
    if not palavras or len(palavras) > _MAX_PALAVRAS_DE_DESISTENCIA:
        return False
    return bool(_DESISTENCIA & set(palavras)) and all(
        p in _DESISTENCIA or p in _ACESSORIAS for p in palavras
    )


async def descarte_deliberado(texto: str, config) -> str | None:
    """Desistência inequívoca com proposta pendente → DESCARTA e devolve o que
    dizer. None significa "isto é conversa" — quem responde é o LLM."""
    if not _e_desistencia(texto):
        return None
    esperando = await propostas.pendentes(config["configurable"]["member_id"])
    if not esperando:
        return None
    palavras = set(re.findall(r"[\wÀ-ÿ]+", texto.lower()))
    pediu_todos = bool(palavras & {"todos", "todas"}) or (
        len(esperando) == 2
        and bool(palavras & {"ambos", "ambas", "dois", "duas"})
    )
    if len(esperando) > 1 and not pediu_todos:
        return (
            f"Há {len(esperando)} compromissos esperando confirmação. "
            "Qual deles você quer descartar? Se quiser apagar todos, diga “descarte todos”."
        )
    # Quem apaga é a tool: é ela que emite o funil (`tool_called`/`tool_result`)
    # e é a chamada dela que prova o efeito.
    await descartar_evento.ainvoke({}, config)
    if len(esperando) == 1:
        return "Descartei o compromisso que estava esperando confirmação — não marquei nada."
    # O plural só chega aqui quando o usuário pediu todos explicitamente.
    return (
        f"Descartei os {len(esperando)} compromissos que estavam esperando "
        "confirmação — não marquei nada."
    )


def _sufixo_local(local: str | None) -> str:
    return f" ({local})" if local else ""


# Motivo categórico → instrução para o subagente. Todas dizem a mesma coisa de
# formas diferentes: NÃO afirme que o compromisso existe.
_RECONECTAR = (
    "NÃO consegui salvar: perdi a autorização do Google Agenda desta pessoa. "
    "Diga que ela precisa mandar /google para autorizar de novo. NÃO afirme que "
    "o compromisso foi marcado."
)
_FALHA_AO_CRIAR = {
    "reconectar": _RECONECTAR,
    "permissao_negada": _RECONECTAR,
    "desconectado": _RECONECTAR,
    "rede_indisponivel": (
        "NÃO consegui salvar: o Google não respondeu agora. Peça para tentar de "
        "novo em instantes. NÃO afirme que o compromisso foi marcado."
    ),
}
_FALHA_GENERICA_AO_CRIAR = (
    "NÃO consegui salvar no Google Agenda agora. Peça para tentar de novo daqui "
    "a pouco — se insistir, mande /google para reconectar. NÃO afirme que o "
    "compromisso foi marcado."
)


# ── listar ───────────────────────────────────────────────────────────────


@tool
async def listar_agenda(dias: int, config: RunnableConfig) -> str:
    """Lista os compromissos dos próximos N dias (use 1 para hoje, 7 para a semana).

    Só olha para a FRENTE, a partir de agora. Para um dia específico, um dia que
    já passou ou um intervalo qualquer, use `consultar_agenda`.
    """
    dias = max(1, min(dias, MAX_DIAS))  # janela curta: protege API e contexto do LLM
    await emitir_de(config, "tool_called", tool="listar_agenda", dias=dias)
    agora = _agora()
    return await _responder_agenda(
        config,
        tool="listar_agenda",
        inicio=agora,
        fim=agora + timedelta(days=dias),
        periodo=f"nos próximos {dias} dia(s)",
    )


@tool
async def consultar_agenda(
    inicio: str, fim: str | None, busca: str | None, config: RunnableConfig
) -> str:
    """Consulta a agenda num período qualquer — inclusive no PASSADO.

    Args:
        inicio: começo do período, em português, como o usuário disse
            ("7 de agosto", "24/08", "ontem", "semana passada", "24/08 às 9h").
            Sem hora, vale o dia inteiro.
        fim: fim do período, quando o usuário deu um intervalo ("10 de agosto").
            Use null para consultar um dia só.
        busca: palavra a procurar no evento ("reunião", "dentista"). Use null
            para trazer tudo do período.
    """
    await emitir_de(
        config,
        "tool_called",
        tool="consultar_agenda",
        # Só o FATO de ter havido filtro — o texto buscado é conteúdo de
        # conversa e não entra em analytics (ADR-005).
        com_busca=bool(busca),
        com_fim=bool(fim),
    )
    janela = resolver_janela(inicio, fim, agora=_agora())
    if janela.inicio is None:
        await emitir_de(
            config, "tool_result", tool="consultar_agenda", ok=False, motivo=janela.motivo
        )
        return _INSTRUCAO_DE_PERIODO[janela.motivo].format(inicio=inicio, fim=fim)

    return await _responder_agenda(
        config,
        tool="consultar_agenda",
        inicio=janela.inicio,
        fim=janela.fim,
        periodo=_periodo_por_extenso(janela.inicio, janela.fim),
        busca=busca,
        # Janela cortada no teto de dias já é resposta incompleta, antes mesmo
        # de o Google responder: dizer "não encontrei" aqui seria falso.
        incompleta=janela.motivo == "janela_reduzida",
    )


_INSTRUCAO_DE_PERIODO = {
    "inicio_nao_entendido": (
        "NÃO ENTENDI o período '{inicio}'. Pergunte ao usuário o dia (ou o "
        "intervalo de dias) exato — não invente."
    ),
    "fim_nao_entendido": (
        "NÃO ENTENDI até quando vai o período ('{fim}'). Pergunte ao usuário o "
        "último dia — não invente."
    ),
    "fim_antes_do_inicio": (
        "O fim do período ('{fim}') ficou ANTES do começo ('{inicio}'). "
        "Pergunte ao usuário qual é o período certo."
    ),
}


def _periodo_por_extenso(inicio: datetime, fim: datetime) -> str:
    """"no dia sex 07/08" ou "de 01/08 a 10/08" — a janela devolvida em texto.

    Sai daqui, e não do LLM: foi o modelo calculando dia da semana de cabeça que
    escreveu "segunda, 24/08" numa conversa real."""
    fuso = ZoneInfo(settings.tz_familia)
    primeiro = inicio.astimezone(fuso)
    # O fim é EXCLUSIVO: o último dia coberto é o instante anterior a ele.
    ultimo = (fim - timedelta(seconds=1)).astimezone(fuso)
    if primeiro.date() == ultimo.date():
        return f"no dia {DIAS_DA_SEMANA[primeiro.weekday()]} {primeiro:%d/%m}"
    return f"de {primeiro:%d/%m} a {ultimo:%d/%m}"


async def _responder_agenda(
    config,
    *,
    tool: str,
    inicio: datetime,
    fim: datetime,
    periodo: str,
    busca: str | None = None,
    incompleta: bool = False,
) -> str:
    """O caminho comum de `listar_agenda` e `consultar_agenda`: ler a janela do
    destino certo, emitir o funil e montar o texto — inclusive a confissão de
    que a leitura ficou incompleta."""
    member_id = config["configurable"]["member_id"]
    if await _conexao_google(member_id) is None:
        return await _listar_agenda_nativa(config, tool, inicio, fim, periodo, busca, incompleta)
    return await _listar_do_google(
        config, tool, member_id, inicio, fim, periodo, busca, incompleta
    )


async def _listar_do_google(
    config, tool: str, member_id: int, inicio, fim, periodo, busca, incompleta
) -> str:
    resultado = await google.listar_eventos_da_agenda(
        member_id, inicio=inicio, fim=fim, texto=busca, teto=MAX_EVENTOS
    )
    if not resultado["ok"]:
        await emitir_de(
            config,
            "tool_result",
            tool=tool,
            ok=False,
            destino="google",
            motivo=resultado["motivo"],
        )
        return _FALHA_AO_LISTAR.get(resultado["motivo"], _FALHA_GENERICA_AO_LISTAR)
    eventos = sorted(resultado["eventos"], key=lambda e: e["inicio"])
    incompleta = incompleta or resultado["truncado"]
    await emitir_de(
        config,
        "tool_result",
        tool=tool,
        ok=True,
        destino="google",
        n=len(eventos),
        incompleta=incompleta,
    )
    return _texto_da_agenda(AGENDA_GOOGLE, eventos, periodo, busca, incompleta)


async def _listar_agenda_nativa(config, tool: str, inicio, fim, periodo, busca, incompleta) -> str:
    # Sem member_id no filtro de propósito: a agenda do Mordomo é
    # COMPARTILHADA (ADR-003), e quem pediu já entra no evento via emitir_de.
    filtros = [FamilyEvent.inicio_utc >= inicio, FamilyEvent.inicio_utc < fim]
    if busca:
        # `escape` explícito: sem ele um "%" digitado pelo usuário viraria
        # curinga e traria a agenda inteira (a mesma trava do cofre).
        alvo = busca.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        filtros.append(FamilyEvent.titulo.ilike(f"%{alvo}%", escape="\\"))
    async with Sessao() as s:
        res = await s.execute(
            select(FamilyEvent).where(*filtros).order_by(FamilyEvent.inicio_utc).limit(MAX_EVENTOS)
        )
        linhas = list(res.scalars())
    eventos = [
        {
            "titulo": e.titulo,
            "inicio": e.inicio_utc,
            "fim": e.fim_utc,
            "dia_inteiro": False,
            "local": e.local,
        }
        for e in linhas
    ]
    truncada = incompleta or len(eventos) >= MAX_EVENTOS
    await emitir_de(
        config,
        "tool_result",
        tool=tool,
        ok=True,
        destino="nativo",
        n=len(eventos),
        incompleta=truncada,
    )
    return _texto_da_agenda(AGENDA_NATIVA, eventos, periodo, busca, truncada)


AVISO_INCOMPLETO = (
    "ATENÇÃO: não consegui ver a janela inteira (é longa demais ou tem eventos "
    "demais). Diga ao usuário que o que veio pode não ser tudo e ofereça olhar "
    "um período menor. NÃO afirme que não existe nada nem que a agenda está livre."
)


def _texto_da_agenda(
    onde: str, eventos: list[dict], periodo: str, busca: str | None, incompleta: bool
) -> str:
    """O texto que volta ao subagente. `incompleta` muda o SENTIDO da lista
    vazia: sem ela é "não há"; com ela é "não sei" — e as duas frases não podem
    se parecer."""
    if not eventos:
        if incompleta:
            return AVISO_INCOMPLETO
        procurado = f' com "{busca}"' if busca else ""
        return f"Não encontrei nada{procurado} {periodo} na {onde}."

    mostrados = eventos[:MAX_LINHAS]
    linhas = "\n".join(f"• {_linha_de_evento(e)}" for e in mostrados)
    resto = len(eventos) - len(mostrados)
    sobra = f"\n(e mais {resto} — peça um período menor para ver todos)" if resto else ""
    aviso = f"\n{AVISO_INCOMPLETO}" if incompleta else ""
    return f"Na {onde}, {periodo}:\n{linhas}{sobra}{aviso}"


def _linha_de_evento(evento: dict) -> str:
    quando = (
        f"{_fmt(evento['inicio'])} (dia inteiro)"
        if evento["dia_inteiro"]
        else _intervalo(evento["inicio"], evento["fim"])
    )
    return f"{quando} — {evento['titulo']}{_sufixo_local(evento['local'])}"


_FALHA_AO_LISTAR = {
    "reconectar": (
        "NÃO consegui ler o Google Agenda: perdi a autorização desta pessoa. "
        "Diga que ela precisa mandar /google para autorizar de novo. NÃO diga "
        "que a agenda está vazia — eu simplesmente não consegui ver."
    ),
    "permissao_negada": (
        "NÃO consegui ler o Google Agenda: perdi a autorização desta pessoa. "
        "Diga que ela precisa mandar /google para autorizar de novo. NÃO diga "
        "que a agenda está vazia."
    ),
    "rede_indisponivel": (
        "NÃO consegui ler o Google Agenda agora: o Google não respondeu. Peça "
        "para tentar de novo em instantes e NÃO diga que a agenda está vazia."
    ),
}
_FALHA_GENERICA_AO_LISTAR = (
    "NÃO consegui ler o Google Agenda agora. Peça para tentar de novo daqui a "
    "pouco — se insistir, mande /google para reconectar. NÃO diga que a agenda "
    "está vazia: eu não consegui ver."
)


# Tool nova que GRAVA algo? Adicione em core/efeitos.py::TOOLS_MUTANTES,
# senão o retry do pipeline pode executá-la duas vezes.
TOOLS_AGENDA = [
    preparar_evento,
    confirmar_evento,
    descartar_evento,
    listar_agenda,
    consultar_agenda,
]
