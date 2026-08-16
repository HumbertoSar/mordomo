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

from datetime import UTC, datetime, timedelta
from functools import partial
from zoneinfo import ZoneInfo

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import tool
from sqlalchemy import select

from ..analytics import emitir_de
from ..config import settings
from ..db.models import FamilyEvent
from ..db.session import Sessao
from ..integracoes import google
from ._comum import fmt_data, resolver_ou_instruir
from .datas import resolver_data

_fmt = partial(fmt_data, dia_semana=True)  # agenda mostra o dia da semana

# Quanto dura um compromisso quando ninguém disse até que horas. Uma hora é o
# padrão do próprio Google Calendar — o valor é arbitrário, então o que importa
# é ser previsível e estar escrito (aqui e no prompt do subagente).
DURACAO_PADRAO_MINUTOS = 60

# Teto de uma página do Calendar. A janela é de dias, não de anos: pedir mais
# que isso seria despejar a agenda inteira dentro do contexto do LLM.
MAX_EVENTOS = 20
MAX_DIAS = 30

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


# ── criar ────────────────────────────────────────────────────────────────


@tool
async def criar_evento(
    titulo: str, quando: str, ate: str | None, local: str | None, config: RunnableConfig
) -> str:
    """Cria um compromisso na agenda de quem está falando.

    Args:
        titulo: ex. "consulta pediatra do João".
        quando: quando COMEÇA — expressão de tempo em português, exatamente
            como o usuário disse (ex. "amanhã às 12h").
        ate: quando TERMINA, se o usuário disse ("amanhã às 16h", ou só "16h").
            Use null quando ele não disse: o compromisso dura 1 hora.
        local: opcional (ex. "clínica do centro").
    """
    member_id = config["configurable"]["member_id"]
    await emitir_de(
        config, "tool_called", tool="criar_evento", quando=quando, com_termino=bool(ate)
    )
    inicio, instrucao = await resolver_ou_instruir(quando, config, "criar_evento")
    if instrucao:
        return instrucao

    if ate:
        fim, motivo = _resolver_fim(ate, inicio)
        if fim is None:
            await emitir_de(config, "tool_result", tool="criar_evento", ok=False, motivo=motivo)
            return _INSTRUCAO_DE_TERMINO[motivo].format(ate=ate)
    else:
        fim = inicio + timedelta(minutes=DURACAO_PADRAO_MINUTOS)

    if await _conexao_google(member_id) is None:
        return await _criar_na_agenda_nativa(config, member_id, titulo, inicio, fim, local)
    return await _criar_no_google(config, member_id, titulo, inicio, fim, local)


async def _conexao_google(member_id: int):
    """A conexão do membro, exista ou não configuração Google neste processo.

    Linha EXISTENTE porém ilegível (chave rotacionada) NÃO vira None de
    propósito: quem conectou tem que ouvir "reconecte", não ver o compromisso
    aparecer calado noutra agenda. O mesmo vale para configuração temporariamente
    ausente: a operação falha declaradamente, sem cair na agenda nativa."""
    return await google.conexao_de(member_id)


async def _criar_no_google(
    config, member_id: int, titulo: str, inicio: datetime, fim: datetime, local: str | None
) -> str:
    resultado = await google.criar_evento_na_agenda(
        member_id,
        titulo=titulo,
        inicio=inicio,
        fim=fim,
        local=local,
        # A trava contra o retry do pipeline: mesmo turno, mesmo id de evento.
        turn_id=config.get("configurable", {}).get("turn_id"),
    )
    if not resultado["ok"]:
        await emitir_de(
            config,
            "tool_result",
            tool="criar_evento",
            ok=False,
            destino="google",
            motivo=resultado["motivo"],
        )
        return _FALHA_AO_CRIAR.get(resultado["motivo"], _FALHA_GENERICA_AO_CRIAR)
    await emitir_de(
        config,
        "tool_result",
        tool="criar_evento",
        ok=True,
        destino="google",
        novo=resultado["novo"],
        duracao_min=int((fim - inicio).total_seconds() // 60),
    )
    repetido = "" if resultado["novo"] else " (já estava lá — não dupliquei)"
    link = resultado.get("link")
    return (
        f"Evento criado no {AGENDA_GOOGLE}: {titulo} — {_intervalo(inicio, fim)}"
        f"{_sufixo_local(local)}{repetido}" + (f"\nVer: {link}" if link else "")
    )


async def _criar_na_agenda_nativa(
    config, member_id: int, titulo: str, inicio: datetime, fim: datetime, local: str | None
) -> str:
    async with Sessao() as s:
        ev = FamilyEvent(
            titulo=titulo,
            inicio_utc=inicio.astimezone(UTC),
            fim_utc=fim.astimezone(UTC),
            local=local,
            criado_por=member_id,
        )
        s.add(ev)
        await s.commit()
        await s.refresh(ev)
    await emitir_de(
        config,
        "tool_result",
        tool="criar_evento",
        ok=True,
        destino="nativo",
        evento_id=ev.id,
        duracao_min=int((fim - inicio).total_seconds() // 60),
    )
    return (
        f"Evento criado na {AGENDA_NATIVA}: {titulo} — "
        f"{_intervalo(inicio, fim)}{_sufixo_local(local)}"
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
    """Lista os compromissos dos próximos N dias (use 1 para hoje, 7 para a semana)."""
    member_id = config["configurable"]["member_id"]
    dias = max(1, min(dias, MAX_DIAS))  # janela curta: protege API e contexto do LLM
    await emitir_de(config, "tool_called", tool="listar_agenda", dias=dias)
    agora = datetime.now(UTC)
    ate = agora + timedelta(days=dias)

    if await _conexao_google(member_id) is None:
        return await _listar_agenda_nativa(config, agora, ate, dias)
    return await _listar_do_google(config, member_id, agora, ate, dias)


async def _listar_do_google(config, member_id: int, agora, ate, dias: int) -> str:
    resultado = await google.listar_eventos_da_agenda(
        member_id, inicio=agora, fim=ate, maximo=MAX_EVENTOS
    )
    if not resultado["ok"]:
        await emitir_de(
            config,
            "tool_result",
            tool="listar_agenda",
            ok=False,
            destino="google",
            motivo=resultado["motivo"],
        )
        return _FALHA_AO_LISTAR.get(resultado["motivo"], _FALHA_GENERICA_AO_LISTAR)
    eventos = resultado["eventos"]
    await emitir_de(
        config, "tool_result", tool="listar_agenda", ok=True, destino="google", n=len(eventos)
    )
    if not eventos:
        return f"Nada no seu {AGENDA_GOOGLE} nos próximos {dias} dia(s)."
    linhas = "\n".join(
        f"• {_linha_do_google(e)}" for e in sorted(eventos, key=lambda e: e["inicio"])
    )
    return f"No seu {AGENDA_GOOGLE}:\n{linhas}"


def _linha_do_google(evento: dict) -> str:
    quando = (
        f"{_fmt(evento['inicio'])} (dia inteiro)"
        if evento["dia_inteiro"]
        else _intervalo(evento["inicio"], evento["fim"])
    )
    return f"{quando} — {evento['titulo']}{_sufixo_local(evento['local'])}"


async def _listar_agenda_nativa(config, agora, ate, dias: int) -> str:
    # Sem member_id no filtro de propósito: a agenda do Mordomo é
    # COMPARTILHADA (ADR-003), e quem pediu já entra no evento via emitir_de.
    async with Sessao() as s:
        res = await s.execute(
            select(FamilyEvent)
            .where(FamilyEvent.inicio_utc >= agora, FamilyEvent.inicio_utc <= ate)
            .order_by(FamilyEvent.inicio_utc)
        )
        eventos = list(res.scalars())
    await emitir_de(
        config, "tool_result", tool="listar_agenda", ok=True, destino="nativo", n=len(eventos)
    )
    if not eventos:
        return f"Nada na {AGENDA_NATIVA} nos próximos {dias} dia(s)."
    linhas = "\n".join(
        f"• {_intervalo(e.inicio_utc, e.fim_utc)} — {e.titulo}{_sufixo_local(e.local)}"
        for e in eventos
    )
    return f"Na {AGENDA_NATIVA}:\n{linhas}"


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
TOOLS_AGENDA = [criar_evento, listar_agenda]
