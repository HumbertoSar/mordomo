"""Replay: como as respostas REAIS ficariam no WhatsApp (passo 0 da fase 3).

    uv run python scripts/replay_whatsapp.py                  # lê o checkpointer
    uv run python scripts/replay_whatsapp.py --exemplo        # sem banco, casos-teste
    uv run python scripts/replay_whatsapp.py --limite 50 --saida docs/replay-whatsapp.md

Para que existe (previsto no ADR-001 desde o dia 1): antes de pôr um humano no
canal novo, pegar as saídas que o mordomo JÁ produziu e re-renderizá-las com
WHATSAPP_CAPS. O que este replay procura é o que só apareceria com a família
olhando:

  - resposta que passa de 1024 caracteres e vira 2, 3, 4 mensagens picadas
  - markdown que o Telegram desenha e o WhatsApp mostra cru (**, ##, tabelas)
  - lista longa que precisaria virar texto numerado

A fonte é o CHECKPOINTER (o histórico de conversa que já está no Postgres) —
não os product_events, que por desenho não guardam texto (ADR-005). Nada sai
da máquina: o relatório é local e não passa por LLM nem por trace.
"""

import argparse
import asyncio
import re
import sys

sys.path.insert(0, "src")

from mordomo.channels.contract import WHATSAPP_CAPS, fatiar_texto  # noqa: E402
from mordomo.config import settings  # noqa: E402
from mordomo.plataforma import preparar  # noqa: E402

# O WhatsApp entende *negrito*, _itálico_ e ~riscado~ — e MAIS NADA. Tudo aqui
# aparece como caractere solto na tela da família (regra nº 6 do projeto).
PADROES_RUINS = [
    (re.compile(r"\*\*[^*]+\*\*"), "negrito de markdown (**) — no WhatsApp é *um* asterisco"),
    (re.compile(r"^#{1,6}\s", re.MULTILINE), "cabeçalho markdown (#) — não existe no WhatsApp"),
    (re.compile(r"^\s*\|.*\|\s*$", re.MULTILINE), "tabela — vira sopa de barras verticais"),
    (re.compile(r"\[[^\]]+\]\([^)]+\)"), "link markdown — o WhatsApp mostra os colchetes"),
    (re.compile(r"^\s*```", re.MULTILINE), "bloco de código — sem monoespaçado no WhatsApp"),
]

EXEMPLOS = [
    "Anotado! Lembrete criado para amanhã às 8h: pagar o boleto da escola. 🤵",
    "**Seus lembretes:**\n\n| Quando | O quê |\n|---|---|\n| amanhã 8h | boleto |",
    "## Agenda de sábado\n\n1. Aniversário da Duda às 15h\n2. Jantar 20h",
    "Seus lembretes:\n" + "\n".join(f"• lembrete número {i} da lista" for i in range(60)),
    "O CEP de casa é 22222-000. Precisa de mais alguma coisa? 🤵",
]


async def respostas_do_checkpointer(limite: int) -> list[str]:
    """Últimas mensagens do MORDOMO nas threads do checkpointer."""
    if not settings.database_url.startswith("postgresql"):
        print("DATABASE_URL não é Postgres — rode com --exemplo ou aponte para o banco real.")
        return []
    from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
    from sqlalchemy import select

    from mordomo.db.models import Member
    from mordomo.db.session import Sessao

    async with Sessao() as s:
        membros = list((await s.execute(select(Member.id))).scalars())

    textos: list[str] = []
    async with AsyncPostgresSaver.from_conn_string(settings.checkpointer_conn_string) as saver:
        for member_id in membros:
            cfg = {"configurable": {"thread_id": f"membro-{member_id}"}}
            tupla = await saver.aget_tuple(cfg)
            if tupla is None:
                continue
            mensagens = (tupla.checkpoint.get("channel_values") or {}).get("messages") or []
            for m in mensagens:
                # Só o que o mordomo DISSE (AIMessage com texto e sem tool call)
                if type(m).__name__ != "AIMessage":
                    continue
                conteudo = m.content if isinstance(m.content, str) else ""
                if conteudo.strip():
                    textos.append(conteudo)
    return textos[-limite:]


def analisar(texto: str) -> dict:
    partes = fatiar_texto(texto, WHATSAPP_CAPS.max_texto)
    achados = [motivo for padrao, motivo in PADROES_RUINS if padrao.search(texto)]
    return {"texto": texto, "partes": partes, "achados": achados}


def relatorio(analises: list[dict]) -> str:
    picadas = [a for a in analises if len(a["partes"]) > 1]
    sujas = [a for a in analises if a["achados"]]
    linhas = [
        "# Replay das respostas reais com os limites do WhatsApp",
        "",
        f"- respostas analisadas: **{len(analises)}**",
        (f"- viram mais de uma mensagem (>{WHATSAPP_CAPS.max_texto} chars): "
         f"**{len(picadas)}**"),
        f"- com formatação que o WhatsApp não desenha: **{len(sujas)}**",
        "",
        ("> Fatiar é seguro (o adapter faz), mas mensagem picada em 3 chega como "
         "3 notificações — vale encurtar a resposta na origem, no prompt."),
        "",
    ]
    if not analises:
        linhas.append("_Nenhuma resposta encontrada. Rode com `--exemplo` para ver o formato._")
        return "\n".join(linhas)

    linhas.append("## Casos a olhar")
    for a in picadas + [x for x in sujas if x not in picadas]:
        linhas.append("")
        linhas.append(f"### {len(a['partes'])} mensagem(ns) · {len(a['texto'])} caracteres")
        for achado in a["achados"]:
            linhas.append(f"- ⚠️ {achado}")
        primeira = a["partes"][0]
        recorte = primeira[:300] + ("…" if len(primeira) > 300 else "")
        linhas.append("")
        linhas.append("```")
        linhas.append(recorte)
        linhas.append("```")
    if not picadas and not sujas:
        linhas.append("")
        linhas.append("✨ Nada a corrigir: todas cabem numa mensagem e usam só texto.")
    return "\n".join(linhas)


async def principal() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--exemplo", action="store_true", help="usa casos sintéticos, sem banco")
    p.add_argument("--limite", type=int, default=100, help="máximo de respostas por membro")
    p.add_argument("--saida", default="", help="grava o relatório em markdown")
    args = p.parse_args()

    textos = EXEMPLOS if args.exemplo else await respostas_do_checkpointer(args.limite)
    texto = relatorio([analisar(t) for t in textos])
    if args.saida:
        import pathlib

        pathlib.Path(args.saida).write_text(texto, encoding="utf-8")
        print(f"Relatório em {args.saida}")
    else:
        print(texto)


if __name__ == "__main__":
    preparar()  # event loop + UTF-8 (Windows) — antes do asyncio.run
    asyncio.run(principal())
