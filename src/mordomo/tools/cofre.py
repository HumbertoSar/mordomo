"""Tools do subagente Cofre — o dado sensível de verdade do projeto.

Regras que NÃO se repetem nos outros módulos:
  - Todo valor lido ou gravado passa por `privacidade.registrar_segredo` ANTES
    de voltar ao LLM: é isso que garante o mascaramento no trace (ADR-005).
  - Analytics leva CHAVE e id, NUNCA o valor (coberto por teste).
  - Busca é por chave (ilike); quem decide o texto da chave é o usuário —
    "RG do Davi" é uma chave tão boa quanto qualquer taxonomia nossa."""

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import tool
from sqlalchemy import and_, or_, select

from ..analytics import emitir_de
from ..db.models import VaultItem
from ..db.session import Sessao
from ..privacidade import registrar_segredo


def _visiveis(member_id: int, em_grupo: bool = False):
    """Itens que este membro pode ler.

    No privado: compartilhados + os próprios. Em GRUPO (decisão de produto,
    11/08): só o compartilhado — o grupo da família VÊ o cofre da família,
    mas item "só pra mim" não aparece num chat coletivo; para ele, o privado.
    `em_grupo` vem do configurable (grupo_id), nunca do texto do LLM."""
    if em_grupo:
        return VaultItem.compartilhado.is_(True)
    return or_(VaultItem.compartilhado.is_(True), VaultItem.dono == member_id)


def _em_grupo(config) -> bool:
    return bool(config["configurable"].get("grupo_id"))


_STOPWORDS = {"de", "do", "da", "dos", "das", "e", "o", "a", "os", "as", "meu", "minha"}


def _sem_curingas(termo: str) -> str:
    """Escapa %/_ vindos do usuário: "100%" ou "meu_wifi" são texto literal,
    não curinga de LIKE (um % solto casaria o cofre inteiro)."""
    return termo.replace("\\", "\\\\").replace("%", r"\%").replace("_", r"\_")


def _por_palavras(coluna, termo: str):
    """Busca por PALAVRAS, não por substring: "carteirinha de saúde do Davi"
    tem que achar "Carteirinha do Plano de Saúde Davi" (caso real, 11/08 — o
    ilike do termo inteiro exigia as palavras na MESMA ordem e vizinhança).
    Cada palavra significativa vira um ILIKE; o AND exige todas, em qualquer ordem."""
    palavras = [p for p in termo.strip().split() if p.lower() not in _STOPWORDS]
    if not palavras:
        return coluna.ilike(f"%{_sem_curingas(termo.strip())}%", escape="\\")
    return and_(*[coluna.ilike(f"%{_sem_curingas(p)}%", escape="\\") for p in palavras])


@tool
async def guardar_info(chave: str, valor: str, config: RunnableConfig, so_para_mim: bool = False) -> str:
    """Guarda (ou atualiza) uma informação no cofre da família.

    Args:
        chave: nome curto e natural, como o usuário se referiria depois
            (ex.: "CEP de casa", "carteirinha do plano do Davi").
        valor: a informação em si, exatamente como fornecida.
        so_para_mim: True se o usuário pedir privacidade ("só pra mim");
            padrão é compartilhado com a família.
    """
    member_id = config["configurable"]["member_id"]
    chave = chave.strip()
    # tool_called na ENTRADA, como todas as outras tools: se o commit falhar,
    # o funil precisa mostrar que a chamada aconteceu (senão ela "nunca houve").
    await emitir_de(config, "tool_called", tool="guardar_info", chave=chave)
    registrar_segredo(valor)
    async with Sessao() as s:
        res = await s.execute(
            select(VaultItem)
            .where(VaultItem.chave.ilike(_sem_curingas(chave), escape="\\"),
                   VaultItem.dono == member_id)
            .order_by(VaultItem.id)
            .limit(1)
        )
        item = res.scalars().first()
        if item is not None:
            item.valor = valor
            item.compartilhado = not so_para_mim
            acao = "atualizada"
        else:
            item = VaultItem(
                chave=chave, valor=valor, dono=member_id, compartilhado=not so_para_mim
            )
            s.add(item)
            acao = "guardada"
        await s.commit()
        await s.refresh(item)
    await emitir_de(config, "tool_result", tool="guardar_info", ok=True, item_id=item.id)
    visibilidade = "só para você" if so_para_mim else "compartilhada com a família"
    return f"Informação '{chave}' {acao} no cofre ({visibilidade})."


@tool
async def buscar_info(termo: str, config: RunnableConfig) -> str:
    """Busca informações no cofre pelo nome (ex.: "CEP", "carteirinha do Davi")."""
    member_id = config["configurable"]["member_id"]
    await emitir_de(config, "tool_called", tool="buscar_info", termo=termo)
    async with Sessao() as s:
        res = await s.execute(
            select(VaultItem)
            .where(_por_palavras(VaultItem.chave, termo), _visiveis(member_id, _em_grupo(config)))
            .order_by(VaultItem.chave)
            .limit(10)
        )
        itens = list(res.scalars())
    # Convenção do funil (igual buscar_documento): não achar = ok=False com
    # motivo — é isso que alimenta o ranking de falhas e a curadoria.
    if not itens:
        await emitir_de(config, "tool_result", tool="buscar_info", ok=False, motivo="nao_encontrado")
        return f"Nada no cofre parecido com '{termo}'. NÃO invente valor — diga que não achou."
    await emitir_de(config, "tool_result", tool="buscar_info", ok=True, n=len(itens))
    for item in itens:
        registrar_segredo(item.valor)  # antes de voltar ao LLM → mascarado no trace
    return "\n".join(f"{i.chave}: {i.valor}" for i in itens)


@tool
async def listar_cofre(config: RunnableConfig) -> str:
    """Lista o que EXISTE no cofre (só os nomes, nunca os valores)."""
    member_id = config["configurable"]["member_id"]
    await emitir_de(config, "tool_called", tool="listar_cofre")
    async with Sessao() as s:
        res = await s.execute(
            select(VaultItem.chave)
            .where(_visiveis(member_id, _em_grupo(config)))
            .order_by(VaultItem.chave)
        )
        chaves = [c for (c,) in res.all()]
    await emitir_de(config, "tool_result", tool="listar_cofre", ok=True, n=len(chaves))
    if not chaves:
        return "O cofre está vazio."
    return "Guardado no cofre:\n" + "\n".join(f"• {c}" for c in chaves)


@tool
async def apagar_info(chave: str, config: RunnableConfig) -> str:
    """Apaga uma informação do cofre pelo nome exato. Dono apaga o que é seu; adulto apaga qualquer item."""
    cfg = config["configurable"]
    member_id, papel = cfg["member_id"], cfg.get("member_papel", "adulto")
    await emitir_de(config, "tool_called", tool="apagar_info", chave=chave)
    async with Sessao() as s:
        res = await s.execute(
            select(VaultItem)
            .where(VaultItem.chave.ilike(_sem_curingas(chave.strip()), escape="\\"))
            .order_by(VaultItem.id)
        )
        itens = list(res.scalars())
        if _em_grupo(config):
            # No grupo só "existe" o que é compartilhado (mesma regra da busca)
            itens = [i for i in itens if i.compartilhado]
        if papel != "adulto":
            # Criança só ENXERGA os próprios itens aqui: a resposta "não achei"
            # para item alheio evita o oráculo de existência ("senha do cartão
            # existe, peça a um adulto").
            itens = [i for i in itens if i.dono == member_id]
        if not itens:
            await emitir_de(config, "tool_result", tool="apagar_info", ok=False, motivo="nao_achou")
            return f"Não achei '{chave}' no cofre."
        # Duas pessoas podem ter a MESMA chave ("CEP de casa"): o próprio item
        # tem prioridade; adulto só apaga o alheio quando não há ambiguidade.
        proprio = next((i for i in itens if i.dono == member_id), None)
        alvo = proprio or (itens[0] if len(itens) == 1 else None)
        if alvo is None:
            await emitir_de(config, "tool_result", tool="apagar_info", ok=False, motivo="ambiguo")
            return (
                f"Há mais de um item chamado '{chave}' (de pessoas diferentes). "
                "Peça ao dono para apagar o dele."
            )
        await s.delete(alvo)
        await s.commit()
    await emitir_de(config, "tool_result", tool="apagar_info", ok=True, item_id=alvo.id)
    return f"'{chave}' apagado do cofre."


@tool
async def buscar_documento(termo: str, config: RunnableConfig) -> str:
    """Busca um documento/imagem guardado (ex.: "RG do Davi", "carteirinha") e
    o PREPARA para envio junto com a resposta. Os bytes nunca passam por você:
    o sistema anexa sozinho — apenas confirme ao usuário o que está indo."""
    from ..channels.contract import Anexo
    from ..core.anexos import registrar
    from ..db.models import Document

    member_id = config["configurable"]["member_id"]
    await emitir_de(config, "tool_called", tool="buscar_documento", termo=termo)
    visivel = (
        Document.compartilhado.is_(True)
        if _em_grupo(config)  # grupo vê o da família; o "particular" fica no privado
        else or_(Document.compartilhado.is_(True), Document.dono == member_id)
    )
    async with Sessao() as s:
        res = await s.execute(
            select(Document)
            .where(_por_palavras(Document.nome, termo), visivel)
            .order_by(Document.nome)
            .limit(3)
        )
        docs = list(res.scalars())
    if not docs:
        await emitir_de(
            config, "tool_result", tool="buscar_documento", ok=False, motivo="nao_encontrado"
        )
        return (
            f"Nenhum documento parecido com '{termo}'. Diga ao usuário que não achou "
            "e que ele pode me enviar a foto com uma legenda (ex.: 'RG do Davi') para guardar."
        )
    await emitir_de(config, "tool_result", tool="buscar_documento", ok=True, n=len(docs))
    for d in docs:
        registrar(config, Anexo(documento_id=d.id, nome=d.nome, mime=d.mime))
    nomes = ", ".join(d.nome for d in docs)
    return f"Documento(s) anexado(s) à resposta: {nomes}. Confirme o envio em uma frase."


@tool
async def listar_documentos(config: RunnableConfig) -> str:
    """Lista os documentos/imagens guardados (só os nomes)."""
    from ..db.models import Document

    member_id = config["configurable"]["member_id"]
    await emitir_de(config, "tool_called", tool="listar_documentos")
    visivel = (
        Document.compartilhado.is_(True)
        if _em_grupo(config)
        else or_(Document.compartilhado.is_(True), Document.dono == member_id)
    )
    async with Sessao() as s:
        res = await s.execute(
            select(Document.nome).where(visivel).order_by(Document.nome)
        )
        nomes = [n for (n,) in res.all()]
    await emitir_de(config, "tool_result", tool="listar_documentos", ok=True, n=len(nomes))
    if not nomes:
        return "Nenhum documento guardado. O usuário pode me enviar fotos com legenda para guardar."
    return "Documentos guardados:\n" + "\n".join(f"• {n}" for n in nomes)


# Tool nova que GRAVA algo? Adicione em core/efeitos.py::TOOLS_MUTANTES,
# senão o retry do pipeline pode executá-la duas vezes.
TOOLS_COFRE = [guardar_info, buscar_info, listar_cofre, apagar_info, buscar_documento, listar_documentos]
