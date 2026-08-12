"""Guardar arquivo no cofre — parte comum a todos os canais.

Guardar documento é ação de ADAPTER (ADR-005): os bytes vão direto do canal
para o banco, sem passar pelo LLM nem pelos traces. O que muda de canal para
canal é só COMO se baixa o arquivo; a regra (nome vem da legenda, mesmo nome do
mesmo dono = atualiza) é uma só e mora aqui.
"""

from __future__ import annotations

import re

from sqlalchemy import select

from ..analytics import emitir
from ..db.models import Document, Member
from ..db.session import Sessao
from ..observability import session_id_de

# A legenda vira o NOME do documento, mas gente escreve instrução: "Guardar essa
# imagem como Carteirinha do Plano" (caso real, 11/08). Tiramos o verbo e as
# palavras de ligação; se sobrar nada, ficamos com a legenda original.
_RGX_INSTRUCAO_LEGENDA = re.compile(
    r"^(?:por favor[,\s]+)?(?:pode(?:ria)?\s+)?"
    r"(?:guardar?|salvar?|anotar?|arquivar?)\s*"
    r"(?:ess[ae]|est[ae]|o|a)?\s*(?:imagem|foto|arquivo|documento)?\s*"
    r"(?:como|de|:)?\s*",
    re.IGNORECASE,
)


def nome_da_legenda(legenda: str) -> str:
    nome = _RGX_INSTRUCAO_LEGENDA.sub("", legenda.strip(), count=1).strip()
    return nome or legenda.strip()


async def guardar(
    membro: Member, nome: str, dados: bytes, mime: str, telegram_file_id: str | None = None
) -> str:
    """Grava (ou atualiza) e devolve "guardado"/"atualizado" para a resposta.

    Chave natural = (nome, dono): reenviar o RG do Davi ATUALIZA o documento em
    vez de criar um segundo com o mesmo nome — senão "me manda o RG do Davi"
    viraria uma escolha entre duplicatas."""
    async with Sessao() as s:
        res = await s.execute(
            select(Document).where(Document.nome.ilike(nome), Document.dono == membro.id)
        )
        doc = res.scalar_one_or_none()
        if doc is not None:
            doc.dados, doc.mime, doc.tamanho = dados, mime, len(dados)
            if telegram_file_id:
                doc.telegram_file_id = telegram_file_id
            acao = "atualizado"
        else:
            doc = Document(
                nome=nome[:120], dono=membro.id, mime=mime, tamanho=len(dados),
                dados=dados, telegram_file_id=telegram_file_id,
            )
            s.add(doc)
            acao = "guardado"
        await s.commit()
    await emitir(
        "document_stored", membro.id, session_id_de(membro.id),
        nome=nome, mime=mime, tamanho=len(dados),
    )
    return acao
