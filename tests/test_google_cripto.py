"""Criptografia em repouso dos tokens de integração (piloto Google).

Token do Google é credencial de longa vida: vazamento do dump do Postgres não
pode virar acesso ao calendário de ninguém. Aqui só se testa o envelope —
quem guarda é `integracoes/google.py`."""

import pytest

from mordomo.integracoes import cripto


def test_gerar_chave_serve_para_cifrar():
    chave = cripto.gerar_chave()
    assert isinstance(chave, str) and len(chave) > 30
    assert cripto.decifrar(cripto.cifrar("ya29.token-secreto", chave), chave) == (
        "ya29.token-secreto"
    )


def test_cifrado_nao_carrega_o_texto_original():
    """O que vai para a coluna não pode conter o segredo em claro."""
    chave = cripto.gerar_chave()
    cifrado = cripto.cifrar("1//refresh-do-humberto", chave)
    assert "refresh-do-humberto" not in cifrado
    assert "1//refresh-do-humberto" not in cifrado


def test_duas_cifragens_do_mesmo_texto_diferem():
    """Fernet usa IV aleatório — sem isso, dá para comparar linhas do banco."""
    chave = cripto.gerar_chave()
    assert cripto.cifrar("mesmo-token", chave) != cripto.cifrar("mesmo-token", chave)


def test_chave_errada_nao_decifra():
    """Autenticado: chave trocada FALHA, não devolve lixo silencioso."""
    cifrado = cripto.cifrar("token", cripto.gerar_chave())
    with pytest.raises(cripto.CriptoErro):
        cripto.decifrar(cifrado, cripto.gerar_chave())


def test_texto_adulterado_nao_decifra():
    """AEAD de verdade: um byte trocado no banco é detectado."""
    chave = cripto.gerar_chave()
    cifrado = cripto.cifrar("token", chave)
    adulterado = cifrado[:-2] + ("AA" if not cifrado.endswith("AA") else "BB")
    with pytest.raises(cripto.CriptoErro):
        cripto.decifrar(adulterado, chave)


def test_chave_ausente_e_erro_claro():
    with pytest.raises(cripto.CriptoErro):
        cripto.cifrar("token", "")


def test_chave_malformada_e_erro_claro():
    """Chave copiada errada do .env não pode explodir como ValueError cru."""
    with pytest.raises(cripto.CriptoErro):
        cripto.cifrar("token", "isto-nao-e-uma-chave-fernet")
