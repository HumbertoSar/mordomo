"""Ciclo de vida da proposta de compromisso (preparar → confirmar → executar).

Mora fora de `tools/` pelo mesmo motivo de `convites.py`: é regra de domínio
com banco, e as tools só a chamam. Nada aqui fala com o Google nem com o LLM.

A invariável que este módulo existe para segurar: **uma proposta vira no
máximo UM evento**. Quem executa precisa reivindicar a proposta antes, e a
reivindicação é um UPDATE condicional (`usado_em IS NULL`) — a mesma técnica do
`/vincular` e do `state` do OAuth. Dois "sim" seguidos, um duplo toque ou o
retry do pipeline (ADR-006) encontram a proposta já tomada e não criam o
segundo evento."""

from __future__ import annotations

import secrets
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, select, update

from .db.models import EventProposal
from .db.session import Sessao

# Quanto tempo o "sim" continua valendo. É escala de conversa, não de sessão:
# confirmar de manhã o que foi combinado ontem à noite quase sempre significa
# que a pessoa perdeu o fio — e aí o certo é remontar, não executar.
VALIDADE_MINUTOS = 30


async def guardar(
    member_id: int,
    *,
    titulo: str,
    inicio: datetime,
    fim: datetime,
    local: str | None,
    convidados: list[str],
    com_meet: bool,
    destino: str,
    turn_id: str | None = None,
    journey_id: str | None = None,
    agora: datetime | None = None,
) -> EventProposal:
    """Grava a proposta resolvida e devolve-a (já com `codigo`)."""
    agora = agora or datetime.now(UTC)
    proposta = EventProposal(
        codigo=secrets.token_urlsafe(12),
        member_id=member_id,
        turn_id=turn_id,
        journey_id=journey_id,
        titulo=titulo,
        inicio_utc=inicio.astimezone(UTC),
        fim_utc=fim.astimezone(UTC),
        local=local,
        convidados=list(convidados),
        com_meet=com_meet,
        destino=destino,
        criado_em=agora,
        expira_em=agora + timedelta(minutes=VALIDADE_MINUTOS),
    )
    async with Sessao() as s:
        s.add(proposta)
        await s.commit()
        await s.refresh(proposta)
    return proposta


async def reivindicar(
    member_id: int, codigo: str | None = None, agora: datetime | None = None
) -> tuple[EventProposal | None, str]:
    """Toma a proposta para execução. Devolve (proposta, motivo).

    Motivos: ok · ausente · ambigua · expirada · de_outro_membro · ja_usada.
    Em `ja_usada` a proposta VOLTA junto (com o link do evento que já existe):
    é o caminho do "sim" repetido, que tem que responder a mesma coisa em vez
    de criar um segundo compromisso.

    A busca é sempre ancorada no `member_id` de quem está falando — que vem do
    `configurable`, nunca do texto (ADR-003). Um código de outra pessoa é
    recusado explicitamente em vez de simplesmente "não encontrado", porque as
    duas situações pedem respostas diferentes."""
    agora = agora or datetime.now(UTC)
    async with Sessao() as s:
        alvo = (
            EventProposal.codigo == codigo
            if codigo
            else EventProposal.member_id == member_id
        )
        res = await s.execute(
            select(EventProposal).where(alvo).order_by(EventProposal.id.desc())
        )
        todas = list(res.scalars())

    if codigo and todas and todas[0].member_id != member_id:
        return None, "de_outro_membro"
    if not todas:
        return None, "ausente"

    vivas = [p for p in todas if p.usado_em is None and p.expira_em > agora]
    if len(vivas) > 1:
        # Executar "a mais recente" seria adivinhar qual compromisso a pessoa
        # quis — e adivinhar aqui grava na agenda de alguém.
        return None, "ambigua"
    if not vivas:
        if usada := next((p for p in todas if p.usado_em is not None), None):
            return usada, "ja_usada"
        return None, "expirada"

    proposta = vivas[0]
    async with Sessao() as s:
        tomada = await s.execute(
            update(EventProposal)
            .where(EventProposal.id == proposta.id, EventProposal.usado_em.is_(None))
            .values(usado_em=agora)
        )
        await s.commit()
    if tomada.rowcount != 1:
        # Perdeu a corrida para um "sim" simultâneo: quem venceu já está
        # criando (ou criou) o evento. Não é erro — é a trava funcionando.
        return proposta, "ja_usada"
    return proposta, "ok"


async def devolver(proposta_id: int) -> None:
    """Desfaz a reivindicação — o evento NÃO foi criado (Google fora do ar,
    credencial vencida). Sem isto o usuário ficaria preso: a proposta queimada
    e nenhum compromisso na agenda."""
    async with Sessao() as s:
        await s.execute(
            update(EventProposal)
            .where(EventProposal.id == proposta_id)
            .values(usado_em=None)
        )
        await s.commit()


async def concluir(
    proposta_id: int, *, link: str | None, agora: datetime | None = None
) -> None:
    """Marca execução concluída e guarda o link, quando o destino devolveu um.

    `usado_em` sozinho não basta: ele é adquirido antes da chamada externa e,
    durante essa janela, outro "sim" não pode ouvir que o evento já existe."""
    agora = agora or datetime.now(UTC)
    async with Sessao() as s:
        await s.execute(
            update(EventProposal)
            .where(EventProposal.id == proposta_id)
            .values(link=link, concluido_em=agora)
        )
        await s.commit()


async def pendentes(member_id: int, agora: datetime | None = None) -> list[EventProposal]:
    """As propostas do membro que ainda esperam um sim (nem usadas, nem vencidas).

    Quem pergunta é o atalho de desistência: "não" só pode virar descarte se
    houver algo pendente — do contrário a resposta afirmaria ter descartado o
    que nunca existiu."""
    agora = agora or datetime.now(UTC)
    async with Sessao() as s:
        res = await s.execute(
            select(EventProposal).where(
                EventProposal.member_id == member_id,
                EventProposal.usado_em.is_(None),
                EventProposal.expira_em > agora,
            )
        )
        return list(res.scalars())


async def descartar(member_id: int, agora: datetime | None = None) -> int:
    """Apaga os pendentes do membro. Devolve quantos foram descartados.

    DELETE, não "marcar como usado": desistir não é ter criado, e deixar a
    linha para trás faria o próximo "sim" tropeçar nela."""
    agora = agora or datetime.now(UTC)
    async with Sessao() as s:
        res = await s.execute(
            delete(EventProposal).where(
                EventProposal.member_id == member_id,
                EventProposal.usado_em.is_(None),
                EventProposal.expira_em > agora,
            )
        )
        await s.commit()
        return res.rowcount
