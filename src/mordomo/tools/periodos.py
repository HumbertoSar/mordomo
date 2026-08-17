"""Janela de tempo para CONSULTAR a agenda (passado, futuro, dia ou intervalo).

Mora fora de `datas.py` porque a pergunta é outra. `resolver_data` serve a quem
MARCA: sem hora dita ela devolve None (regra nº 3) e prefere o futuro, porque
"sexta" de quem marca é a próxima sexta. Quem CONSULTA quer o oposto nas duas
pontas: "o dia 7" é um pedido legítimo pelo dia inteiro, e numa conversa de
17/08 esse 7 é o que passou — não o do ano que vem.

A janela é sempre [início, fim) — meia-aberta, igual à do Google Calendar
(`timeMin`/`timeMax`), para o último dia entrar inteiro sem contar duas vezes o
instante de virada."""

from datetime import datetime, timedelta
from typing import NamedTuple
from zoneinfo import ZoneInfo

from ..config import settings
from .datas import resolver_instante

# Teto defensivo do PERÍODO pedido. Intervalo arbitrário é o objetivo desta
# fatia; dez anos de agenda dentro do contexto do LLM, não. Um ano cobre
# "o que eu tinha no ano passado" e ainda cabe no teto de eventos.
MAX_DIAS_JANELA = 366

# Consultar não é marcar: aqui a data solta vale no período CORRENTE, e é isso
# que faz "7 de agosto" resolver para o 7 que passou.
_PREFERENCIA_DE_CONSULTA = "current_period"


class Janela(NamedTuple):
    """(início, fim, motivo). `inicio is None` = não deu para montar a janela e
    `motivo` diz por quê — o agente PERGUNTA, nunca chuta um período.

    Motivos: ok · janela_reduzida (montada, porém limitada — quem responde tem
    que dizer isso) · inicio_nao_entendido · fim_nao_entendido ·
    fim_antes_do_inicio."""

    inicio: datetime | None
    fim: datetime | None
    motivo: str


def _inicio_do_dia(quando: datetime) -> datetime:
    return quando.astimezone(ZoneInfo(settings.tz_familia)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )


def _inicio_do_dia_seguinte(quando: datetime) -> datetime:
    """Fim EXCLUSIVO de um dia inteiro. Somamos um dia à meia-noite local em vez
    de usar 23:59:59: assim o dia de mudança de horário de verão continua
    fechando no lugar certo."""
    local = _inicio_do_dia(quando)
    return (local + timedelta(days=1, hours=12)).replace(hour=0, minute=0)


def resolver_janela(
    inicio: str, fim: str | None = None, *, agora: datetime | None = None
) -> Janela:
    """Expressões pt-BR → janela [início, fim) no fuso da família.

    Sem `fim`, a janela é o DIA inteiro do início (quando ele veio sem hora) ou
    dali até o fim daquele dia (quando o usuário disse a hora de começo).
    `agora` é injetável para os testes fixarem o relógio — nunca vem do usuário.
    """
    comeco, comeco_com_hora = resolver_instante(
        inicio, base=agora, preferencia=_PREFERENCIA_DE_CONSULTA
    )
    if comeco is None:
        return Janela(None, None, "inicio_nao_entendido")
    if not comeco_com_hora:
        comeco = _inicio_do_dia(comeco)

    if fim is None:
        return Janela(comeco, _inicio_do_dia_seguinte(comeco), "ok")

    termino, termino_com_hora = resolver_instante(
        fim, base=agora, preferencia=_PREFERENCIA_DE_CONSULTA
    )
    if termino is None:
        return Janela(None, None, "fim_nao_entendido")
    if not termino_com_hora:
        # "de 1 a 10 de agosto" inclui o dia 10 INTEIRO: parar às 00:00 do dia 10
        # esconderia justamente o dia que o usuário nomeou.
        termino = _inicio_do_dia_seguinte(termino)
    if termino <= comeco:
        return Janela(None, None, "fim_antes_do_inicio")

    if termino - comeco > timedelta(days=MAX_DIAS_JANELA):
        # Limitar é aceitável; limitar CALADO não — quem responde precisa dizer
        # que olhou só um pedaço, senão "não encontrei" vira mentira.
        return Janela(comeco, comeco + timedelta(days=MAX_DIAS_JANELA), "janela_reduzida")
    return Janela(comeco, termino, "ok")
