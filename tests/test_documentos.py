"""Documentos: contrato de anexo, registro por turno e a tool buscar_documento."""

from sqlalchemy import select

from apoio import cfg_de as _cfg
from apoio import criar_membro as _membro
from mordomo.channels.contract import TELEGRAM_CAPS, WHATSAPP_CAPS, Anexo, OutboundMessage
from mordomo.core.anexos import coletar, registrar
from mordomo.db.models import Document, ProductEvent
from mordomo.db.session import Sessao
from mordomo.tools.cofre import buscar_documento, listar_documentos


async def _doc(nome: str, dono: int, dados: bytes = b"jpegfake", compartilhado: bool = True) -> Document:
    async with Sessao() as s:
        d = Document(nome=nome, dono=dono, dados=dados, tamanho=len(dados),
                     compartilhado=compartilhado)
        s.add(d)
        await s.commit()
        await s.refresh(d)
        return d


def test_legenda_com_instrucao_vira_nome_limpo():
    """Caso real (11/08): legenda-instrução poluía o nome do documento."""
    from mordomo.channels.telegram import _nome_da_legenda

    assert (_nome_da_legenda("Guardar essa imagem como Carteirinha do Plano de Saúde Davi")
            == "Carteirinha do Plano de Saúde Davi")
    assert _nome_da_legenda("salva como RG do Davi") == "RG do Davi"
    assert _nome_da_legenda("RG do Davi") == "RG do Davi"          # sem instrução, intacta
    assert _nome_da_legenda("Guardar") == "Guardar"                # só o verbo → mantém


def test_canais_suportam_midia_no_contrato():
    """Golden do ADR-001: anexo tem plano válido nos dois canais."""
    assert TELEGRAM_CAPS.supports_media and WHATSAPP_CAPS.supports_media
    msg = OutboundMessage(texto="segue", anexos=[Anexo(documento_id=1, nome="RG")])
    assert msg.anexos[0].nome == "RG"


def test_registro_de_anexo_e_por_turno():
    cfg = {"configurable": {"turn_id": "t-anexo-1"}}
    registrar(cfg, Anexo(documento_id=7, nome="RG do Davi"))
    registrar(cfg, Anexo(documento_id=8, nome="carteirinha"))
    registrar({"configurable": {"turn_id": "t-anexo-OUTRO"}}, Anexo(documento_id=9, nome="x"))

    anexos = coletar("t-anexo-1")
    assert [a.documento_id for a in anexos] == [7, 8]
    assert coletar("t-anexo-1") == []          # coletar esvazia
    assert len(coletar("t-anexo-OUTRO")) == 1  # turnos não se misturam


async def test_buscar_documento_registra_anexo_e_nao_vaza_bytes():
    m = await _membro("DocDono")
    await _doc("RG do Davi", m.id, dados=b"BYTESSECRETOS")
    r = await buscar_documento.ainvoke({"termo": "rg do davi"}, _cfg(m, "t-doc-1"))
    assert "anexado" in r.lower()
    assert "BYTESSECRETOS" not in r            # bytes nunca voltam ao LLM

    anexos = coletar("t-doc-1")
    assert len(anexos) == 1 and anexos[0].nome == "RG do Davi"

    async with Sessao() as s:
        res = await s.execute(select(ProductEvent).where(ProductEvent.turn_id == "t-doc-1"))
        payloads = str([e.payload for e in res.scalars()])
    assert "BYTESSECRETOS" not in payloads     # nem ao analytics


async def test_documento_privado_de_outro_nao_aparece():
    dona = await _membro("DocPrivada")
    outro = await _membro("DocOutro")
    await _doc("exame particular", dona.id, compartilhado=False)
    r = await buscar_documento.ainvoke({"termo": "exame"}, _cfg(outro, "t-doc-2"))
    assert "Nenhum documento" in r
    assert coletar("t-doc-2") == []


async def test_listar_documentos_mostra_nomes():
    m = await _membro("DocListagem")
    await _doc("comprovante de endereço", m.id)
    r = await listar_documentos.ainvoke({}, _cfg(m, "t-doc-3"))
    assert "comprovante de endereço" in r
