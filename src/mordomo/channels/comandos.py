"""Comandos de texto que valem em QUALQUER canal (/start, /ajuda, /convidar,
/vincular).

Por que existir: onboarding é borda (ADR-003) e não passa pelo grafo — mas a
regra ("só adulto convida", "código é uso único", "vinculação só no privado")
não pode ser reimplementada a cada canal novo. Aqui a lógica é uma só e devolve
TEXTO; quem sabe enviar (e o que é "privado") é o adapter.

O Telegram mantém os handlers dele por enquanto (tem /dashboard com arquivo e
os filtros do aiogram); pode migrar para cá sem mudança de comportamento.
"""

from __future__ import annotations

from ..convites import VALIDADE_DIAS, criar_convite, usar_convite
from ..identity import resolver_membro

RECUSA_DESCONHECIDO = (
    "Ainda não nos conhecemos! 🤝 Peça um código de convite a quem administra "
    "o mordomo e me mande: /vincular CÓDIGO"
)

AJUDA = (
    "Sou o mordomo da família. 🤵 Posso:\n"
    "• criar lembretes — \"me lembra amanhã às 8h de pagar o boleto\"\n"
    "• cuidar da agenda — \"o que temos no sábado?\"\n"
    "• guardar o que sempre some — \"anota que o CEP de casa é 22222-000\"\n"
    "\nComandos: /convidar NOME [adulto|crianca] · /vincular CÓDIGO · /ajuda"
)


def eh_comando(texto: str) -> bool:
    return texto.strip().startswith("/")


def _partes(texto: str) -> tuple[str, str]:
    """'/convidar Maria adulto' → ('convidar', 'Maria adulto'). O sufixo
    '@bot' que o WhatsApp e o Telegram às vezes carregam é descartado."""
    bruto = texto.strip()
    cabeca, _, resto = bruto.partition(" ")
    comando = cabeca[1:].split("@", 1)[0].lower()
    return comando, resto.strip()


async def responder(canal: str, external_id: str, texto: str, privado: bool = True) -> str | None:
    """Resposta do comando, ou None quando o texto não é um comando conhecido
    (aí o adapter segue com o fluxo normal — o texto vai para o grafo)."""
    if not eh_comando(texto):
        return None
    comando, args = _partes(texto)

    if comando in ("start", "ajuda", "help"):
        membro = await resolver_membro(canal, external_id)
        if membro is None:
            return RECUSA_DESCONHECIDO
        return f"Às ordens, {membro.nome}! 🤵\n\n{AJUDA}"

    if comando == "convidar":
        # O código é o ÚNICO segredo do onboarding: dito em grupo, qualquer
        # presente se vincula antes do convidado (ADR-008).
        if not privado:
            return "Convite é assunto de privado — me chame lá. 🤫"
        membro = await resolver_membro(canal, external_id)
        if membro is None:
            return "Ainda não nos conhecemos — convites são só para a família. 🤝"
        partes = args.split()
        papel = "adulto"
        if partes and partes[-1].lower() in ("adulto", "crianca", "criança"):
            papel = "crianca" if partes.pop().lower().startswith("crian") else "adulto"
        nome = " ".join(partes).strip()
        if not nome:
            return (
                "Uso: /convidar NOME [adulto|crianca]\n"
                "Ex.: /convidar Maria adulto · /convidar João crianca"
            )
        codigo = await criar_convite(membro, nome, papel)
        if codigo is None:
            return "Convites são coisa de adulto por aqui. 😉"
        return (
            f"Convite criado para {nome} ({papel}), válido por {VALIDADE_DIAS} dias.\n"
            f"Peça para a pessoa me mandar:\n\n/vincular {codigo}"
        )

    if comando == "vincular":
        if not privado:
            return "Vinculação é assunto de privado — me chame lá. 🤫"
        codigo = args.strip()
        if not codigo:
            return "Uso: /vincular CÓDIGO (peça o código a quem administra o mordomo)."
        membro, motivo = await usar_convite(codigo, canal, external_id)
        nome = membro.nome if membro else ""
        return {
            "ok": f"Bem-vindo(a) à família, {nome}! 🤵 "
                  "Já pode me pedir lembretes e consultar a agenda.",
            "ja_cadastrado": f"Nós já nos conhecemos, {nome}! Às ordens.",
            "codigo_invalido": "Esse código não confere. Confira com quem convidou você. 🤔",
            "ja_usado": "Esse código já foi usado. Peça um novo convite.",
            "expirado": "Esse código expirou. Peça um novo convite.",
        }[motivo]

    return None
