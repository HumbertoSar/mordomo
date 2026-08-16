"""Criptografia em repouso das credenciais de integração.

POR QUE UMA DEPENDÊNCIA NOVA: a stdlib do Python não tem cifra autenticada
(hashlib/hmac fazem integridade, não confidencialidade). Inventar "AES na mão"
seria pior que não cifrar — por isso `cryptography`, que é a biblioteca de
referência do ecossistema Python (mantida pela PyCA, usada por Django,
Ansible e requests/urllib3), e dentro dela a receita PRONTA `Fernet`:
AES-128-CBC + HMAC-SHA256, com IV aleatório e timestamp, sem escolha de
parâmetro nossa. Nada aqui é criptografia caseira.

A chave vive só em variável de ambiente (`GOOGLE_TOKEN_KEY`), NUNCA no banco:
quem tem só o dump do Postgres não abre o calendário de ninguém. Rotacionar a
chave invalida as conexões existentes — a família refaz `/google` (custo
aceitável no piloto; ver a ADR).
"""

from __future__ import annotations

from cryptography.fernet import Fernet, InvalidToken


class CriptoErro(RuntimeError):
    """Chave ausente, malformada, trocada, ou dado adulterado.

    Tipo único de propósito: quem chama trata sempre igual (pedir reconexão) e
    nunca precisa distinguir — distinguir em mensagem de erro é oráculo."""


def gerar_chave() -> str:
    """Chave nova para o .env. Use `python -m mordomo.integracoes.cripto`."""
    return Fernet.generate_key().decode()


def _cofre(chave: str) -> Fernet:
    if not chave:
        raise CriptoErro("chave de criptografia ausente")
    try:
        return Fernet(chave.encode() if isinstance(chave, str) else chave)
    except Exception as erro:  # chave com tamanho/base64 errado
        raise CriptoErro("chave de criptografia malformada") from erro


def cifrar(texto: str, chave: str) -> str:
    """Texto claro → token Fernet (str, cabe em coluna de texto)."""
    return _cofre(chave).encrypt(texto.encode()).decode()


def decifrar(cifrado: str, chave: str) -> str:
    try:
        return _cofre(chave).decrypt(cifrado.encode()).decode()
    except InvalidToken as erro:
        raise CriptoErro("não consegui decifrar (chave trocada ou dado alterado)") from erro


if __name__ == "__main__":  # pragma: no cover — utilitário de operação
    print(gerar_chave())
