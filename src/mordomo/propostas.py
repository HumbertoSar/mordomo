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

# Quanto tempo uma reivindicação sem conclusão é considerada trabalho ativo.
# Depois disso outro worker pode retomar com a MESMA proposta/chave Google.
REIVINDICACAO_LEASE_MINUTOS = 2


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
    eventos: list | None = None,
) -> EventProposal:
    """Grava a proposta resolvida e devolve-a (já com `codigo`).

    `eventos` entram na MESMA transação (é por onde passa o `journey_started`):
    não pode existir jornada iniciada sem proposta persistida, nem proposta
    apontando para uma jornada que nunca começou. Quem monta os eventos é o
    chamador — este módulo é domínio, não analytics."""
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
        if eventos:
            s.add_all(eventos)
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
        usada = next((p for p in todas if p.usado_em is not None), None)
        if usada is None:
            return None, "expirada"
        if usada.concluido_em is None:
            limite = agora - timedelta(minutes=REIVINDICACAO_LEASE_MINUTOS)
            async with Sessao() as s:
                retomada = await s.execute(
                    update(EventProposal)
                    .where(
                        EventProposal.id == usada.id,
                        EventProposal.concluido_em.is_(None),
                        EventProposal.usado_em <= limite,
                    )
                    .values(usado_em=agora)
                    .returning(EventProposal.id)
                )
                retomou = retomada.scalar_one_or_none() is not None
                if retomou:
                    await s.commit()
                else:
                    await s.rollback()
            if retomou:
                usada.usado_em = agora
                return usada, "ok"
        return usada, "ja_usada"

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
    proposta.usado_em = agora
    return proposta, "ok"


async def devolver(proposta_id: int, *, reivindicada_em: datetime) -> bool:
    """Desfaz SOMENTE a reivindicação deste worker quando o efeito não ocorreu.

    A comparação do horário é a propriedade da lease: um worker antigo não pode
    limpar a retomada de outro que já está executando com a mesma proposta."""
    async with Sessao() as s:
        devolvida = await s.execute(
            update(EventProposal)
            .where(
                EventProposal.id == proposta_id,
                EventProposal.usado_em == reivindicada_em,
                EventProposal.concluido_em.is_(None),
            )
            .values(usado_em=None)
            .returning(EventProposal.id)
        )
        ok = devolvida.scalar_one_or_none() is not None
        if ok:
            await s.commit()
        else:
            await s.rollback()
        return ok


async def concluir_na_sessao(
    s,
    proposta_id: int,
    *,
    link: str | None,
    agora: datetime,
    eventos: list | None = None,
) -> bool:
    """Conclui dentro da transação do chamador e exige uma reivindicação viva."""
    concluida = await s.execute(
        update(EventProposal)
        .where(
            EventProposal.id == proposta_id,
            EventProposal.usado_em.is_not(None),
            EventProposal.concluido_em.is_(None),
        )
        .values(link=link, concluido_em=agora)
        .returning(EventProposal.id)
    )
    if concluida.scalar_one_or_none() is None:
        return False
    if eventos:
        s.add_all(eventos)
    return True


async def concluir(
    proposta_id: int,
    *,
    link: str | None,
    agora: datetime | None = None,
    eventos: list | None = None,
) -> bool:
    """Marca execução concluída e guarda o link, quando o destino devolveu um.

    `usado_em` sozinho não basta: ele é adquirido antes da chamada externa e,
    durante essa janela, outro "sim" não pode ouvir que o evento já existe.

    `eventos` (o `journey_resolved`) entra na mesma transação da conclusão: a
    resolução da jornada e o registro de que a execução terminou são o mesmo
    fato. O que NÃO dá para tornar atômico é a chamada externa — o Google não
    participa da transação. Por isso a ordem é: efeito confirmado lá fora →
    conclusão + resolução aqui, juntas. Uma queda entre as duas deixa evento
    criado e jornada aberta (o erro seguro), nunca o contrário."""
    agora = agora or datetime.now(UTC)
    async with Sessao() as s:
        ok = await concluir_na_sessao(
            s,
            proposta_id,
            link=link,
            agora=agora,
            eventos=eventos,
        )
        if not ok:
            await s.rollback()
            return False
        await s.commit()
        return True


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


async def descartar(
    member_id: int,
    agora: datetime | None = None,
    eventos_por_jornada=None,
) -> list[str | None]:
    """Apaga os pendentes do membro. Devolve as jornadas realmente descartadas.

    DELETE, não "marcar como usado": desistir não é ter criado, e deixar a
    linha para trás faria o próximo "sim" tropeçar nela.

    Devolve a LISTA (uma entrada por proposta apagada, `None` para proposta
    antiga sem jornada) em vez do antigo contador: quem chama precisa abandonar
    exatamente as jornadas que sumiram — nem uma a mais. Proposta expirada não
    entra no filtro e portanto não vira abandono: ela não foi descartada, ela
    venceu.

    `eventos_por_jornada` recebe essas jornadas e devolve os eventos a gravar na
    MESMA transação do DELETE — abandono sem descarte (ou o inverso) seria
    exatamente a inconsistência que o resto deste módulo existe para evitar."""
    agora = agora or datetime.now(UTC)
    pendentes_agora = (
        EventProposal.member_id == member_id,
        EventProposal.usado_em.is_(None),
        EventProposal.expira_em > agora,
    )
    async with Sessao() as s:
        removidas = await s.execute(
            delete(EventProposal)
            .where(*pendentes_agora)
            .returning(EventProposal.journey_id)
        )
        jornadas = list(removidas.scalars())
        if not jornadas:
            return []
        if eventos_por_jornada is not None and (eventos := eventos_por_jornada(jornadas)):
            s.add_all(eventos)
        await s.commit()
        return jornadas
