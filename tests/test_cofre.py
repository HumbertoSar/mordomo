"""Cofre: guardar/buscar/apagar, visibilidade, e as garantias do ADR-005 —
valor mascarado no trace e ausente do analytics.

Nota de API: o RunnableConfig injetado vai como SEGUNDO argumento do ainvoke
(`tool.ainvoke(entrada, config)`), nunca como chave da entrada."""

from sqlalchemy import select

from apoio import cfg_de as _cfg
from apoio import criar_membro as _membro
from mordomo import privacidade
from mordomo.db.models import ProductEvent, VaultItem
from mordomo.db.session import Sessao
from mordomo.tools.cofre import apagar_info, buscar_info, guardar_info, listar_cofre


async def test_guardar_e_buscar():
    m = await _membro("CofreDono")
    r = await guardar_info.ainvoke(
        {"chave": "CEP de casa", "valor": "22041-011"}, _cfg(m, "t-c1")
    )
    assert "guardada" in r
    r = await buscar_info.ainvoke({"termo": "cep"}, _cfg(m, "t-c2"))
    assert "22041-011" in r


async def test_atualizar_mesma_chave_nao_duplica():
    m = await _membro("CofreAtualiza")
    cfg = _cfg(m, "t-c3")
    await guardar_info.ainvoke({"chave": "placa do carro", "valor": "ABC1D23"}, cfg)
    await guardar_info.ainvoke({"chave": "placa do carro", "valor": "XYZ9K88"}, cfg)
    async with Sessao() as s:
        res = await s.execute(select(VaultItem).where(VaultItem.dono == m.id))
        itens = list(res.scalars())
    assert len(itens) == 1 and itens[0].valor == "XYZ9K88"


async def test_item_privado_nao_aparece_para_outro_membro():
    dona = await _membro("CofrePrivada")
    outro = await _membro("CofreOutro")
    await guardar_info.ainvoke(
        {"chave": "senha do wifi da sala", "valor": "familia123x", "so_para_mim": True},
        _cfg(dona, "t-c4"),
    )
    r = await buscar_info.ainvoke({"termo": "wifi"}, _cfg(outro, "t-c5"))
    assert "familia123x" not in r
    r = await buscar_info.ainvoke({"termo": "wifi"}, _cfg(dona, "t-c6"))
    assert "familia123x" in r


async def test_listar_mostra_chaves_mas_nunca_valores():
    m = await _membro("CofreListagem")
    await guardar_info.ainvoke(
        {"chave": "RG da Ana", "valor": "12.345.678-9"}, _cfg(m, "t-c7")
    )
    r = await listar_cofre.ainvoke({}, _cfg(m, "t-c8"))
    assert "RG da Ana" in r and "12.345.678-9" not in r


async def test_crianca_nao_apaga_nem_descobre_item_alheio():
    """Oráculo fechado: para a criança, item alheio "não existe" — a resposta
    não pode confirmar que "cartao do plano" está no cofre."""
    adulto = await _membro("CofreAdultoDono")
    crianca = await _membro("CofreCrianca", papel="crianca")
    await guardar_info.ainvoke(
        {"chave": "cartao do plano", "valor": "987654321000"}, _cfg(adulto, "t-c9")
    )
    r = await apagar_info.ainvoke({"chave": "cartao do plano"}, _cfg(crianca, "t-c10"))
    assert "não achei" in r.lower()
    r = await apagar_info.ainvoke({"chave": "cartao do plano"}, _cfg(adulto, "t-c11"))
    assert "apagado" in r


async def test_apagar_chave_repetida_prefere_o_proprio_e_avisa_ambiguidade():
    """Duas pessoas com a MESMA chave: apagar o seu não pode nem estourar
    (MultipleResultsFound de antes) nem apagar o do outro por engano."""
    ana = await _membro("CofreAna")
    beto = await _membro("CofreBeto")
    carla = await _membro("CofreCarla")
    await guardar_info.ainvoke({"chave": "cep do trabalho", "valor": "11111-111"}, _cfg(ana, "t-c15"))
    await guardar_info.ainvoke({"chave": "cep do trabalho", "valor": "22222-222"}, _cfg(beto, "t-c16"))

    # Dona apaga o SEU; o do Beto sobrevive
    r = await apagar_info.ainvoke({"chave": "cep do trabalho"}, _cfg(ana, "t-c17"))
    assert "apagado" in r
    async with Sessao() as s:
        res = await s.execute(select(VaultItem).where(VaultItem.chave == "cep do trabalho"))
        restantes = list(res.scalars())
    assert len(restantes) == 1 and restantes[0].dono == beto.id

    # Recria o da Ana; Carla (sem item próprio) encontra DOIS → ambiguidade
    await guardar_info.ainvoke({"chave": "cep do trabalho", "valor": "11111-111"}, _cfg(ana, "t-c18"))
    r = await apagar_info.ainvoke({"chave": "cep do trabalho"}, _cfg(carla, "t-c19"))
    assert "mais de um" in r.lower()


async def test_curinga_do_usuario_nao_vira_wildcard():
    """"%" literal não pode casar o cofre inteiro (nem no buscar, nem no apagar)."""
    m = await _membro("CofreCuringa")
    await guardar_info.ainvoke({"chave": "meta de poupança", "valor": "20%"}, _cfg(m, "t-c20"))
    await guardar_info.ainvoke({"chave": "pin do cadeado", "valor": "4321"}, _cfg(m, "t-c21"))

    r = await buscar_info.ainvoke({"termo": "%"}, _cfg(m, "t-c22"))
    assert "4321" not in r  # "%" não é curinga: só acharia chave com % literal

    r = await apagar_info.ainvoke({"chave": "%"}, _cfg(m, "t-c23"))
    assert "não achei" in r.lower()  # e não apaga nada por engano
    async with Sessao() as s:
        res = await s.execute(select(VaultItem).where(VaultItem.dono == m.id))
        assert len(list(res.scalars())) == 2


async def test_valores_sao_registrados_e_mascarados():
    """ADR-005 item 2: o que passa pelo cofre some de qualquer trace."""
    m = await _membro("CofreMascarado")
    await guardar_info.ainvoke(
        {"chave": "carteirinha do plano", "valor": "556677889900"}, _cfg(m, "t-c12")
    )
    trace_falso = {
        "output": "A carteirinha do plano é 556677889900.",
        "aninhado": ["numero 556677889900", {"x": "556677889900"}],
    }
    mascarado = privacidade.mascarar(trace_falso)
    assert "556677889900" not in str(mascarado)
    assert "«cofre»" in mascarado["output"]


async def test_analytics_do_cofre_nao_carrega_valor():
    """ADR-005: payloads de product_events levam chave/id, nunca o valor."""
    m = await _membro("CofreAnalytics")
    await guardar_info.ainvoke(
        {"chave": "documento secreto", "valor": "SEGREDO-99887766"}, _cfg(m, "t-c13")
    )
    await buscar_info.ainvoke({"termo": "documento secreto"}, _cfg(m, "t-c14"))
    async with Sessao() as s:
        res = await s.execute(select(ProductEvent).where(ProductEvent.member_id == m.id))
        eventos = list(res.scalars())
    assert eventos, "tools do cofre devem emitir analytics"
    assert "SEGREDO-99887766" not in str([e.payload for e in eventos])
