"""Tools do subagente Cofre — o dado sensível de verdade do projeto.

Regras que NÃO se repetem nos outros módulos:
  - Todo valor lido ou gravado passa por `privacidade.registrar_segredo` ANTES
    de voltar ao LLM: é isso que garante o mascaramento no trace (ADR-005).
  - Analytics leva CHAVE e id, NUNCA o valor (coberto por teste).
  - Busca é por chave (ilike); quem decide o texto da chave é o usuário —
    "RG do Davi" é uma chave tão boa quanto qualquer taxonomia nossa."""

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import tool
from sqlalchemy import or_, select

from ..analytics import emitir_de
from ..db.models import VaultItem
from ..db.session import Sessao
from ..privacidade import registrar_segredo


def _visiveis(member_id: int):
    """Itens que este membro pode ler: compartilhados + os próprios."""
    return or_(VaultItem.compartilhado.is_(True), VaultItem.dono == member_id)


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
    registrar_segredo(valor)
    async with Sessao() as s:
        res = await s.execute(
            select(VaultItem).where(VaultItem.chave.ilike(chave), VaultItem.dono == member_id)
        )
        item = res.scalar_one_or_none()
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
    await emitir_de(config, "tool_called", tool="guardar_info", chave=chave)
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
            .where(VaultItem.chave.ilike(f"%{termo.strip()}%"), _visiveis(member_id))
            .order_by(VaultItem.chave)
            .limit(10)
        )
        itens = list(res.scalars())
    await emitir_de(config, "tool_result", tool="buscar_info", ok=True, n=len(itens))
    if not itens:
        return f"Nada no cofre parecido com '{termo}'. NÃO invente valor — diga que não achou."
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
            select(VaultItem.chave).where(_visiveis(member_id)).order_by(VaultItem.chave)
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
        res = await s.execute(select(VaultItem).where(VaultItem.chave.ilike(chave.strip())))
        item = res.scalar_one_or_none()
        if item is None:
            await emitir_de(config, "tool_result", tool="apagar_info", ok=False, motivo="nao_achou")
            return f"Não achei '{chave}' no cofre."
        if item.dono != member_id and papel != "adulto":
            await emitir_de(config, "tool_result", tool="apagar_info", ok=False, motivo="sem_permissao")
            return "Esse item não é seu — peça a um adulto para apagar."
        await s.delete(item)
        await s.commit()
    await emitir_de(config, "tool_result", tool="apagar_info", ok=True)
    return f"'{chave}' apagado do cofre."


TOOLS_COFRE = [guardar_info, buscar_info, listar_cofre, apagar_info]
