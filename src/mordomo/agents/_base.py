"""Fábrica dos nós de subagente — o padrão que era copiado em 3 arquivos.

Todo subagente ReAct do grafo é a mesma coisa: singleton preguiçoso do agente
(criado no 1º uso, para o import não exigir chave de API), janela de contexto
(ADR-007 — o histórico completo fica no checkpointer; ao LLM vai só o recente),
contagem de uso APENAS das mensagens novas (as antigas já carregam o
usage_metadata do turno em que nasceram e seriam contadas duas vezes) e
devolver a última mensagem ao grafo pai. O que muda é prompt, tools e o nome
no `llm_usage` — viraram parâmetros. A fase 2 (Tarefas, Curador, Mensageiro)
cria subagente novo com 3 linhas em vez de um arquivo copiado.

Testes injetam um agente falso pelo atributo `agente` da instância
(ex.: `no_lembretes.agente = _AgenteFalso(...)` — ver tests/test_grafo.py)."""

from ..analytics import emitir_de, uso_de
from ..config import settings
from ..core.contexto import janela
from ..core.llm import chat_model, criar_agente
from ..core.state import EstadoMordomo


class NoSubagente:
    def __init__(self, nome: str, tools: list, prompt: str) -> None:
        self.nome = nome
        self.tools = tools
        self.prompt = prompt
        self.agente = None  # preenchido no 1º uso; testes injetam um fake aqui

    def _agente_real(self):
        if self.agente is None:
            self.agente = criar_agente(chat_model(settings.model_agente), self.tools, self.prompt)
        return self.agente

    async def __call__(self, state: EstadoMordomo, config) -> dict:
        entrada = janela(state["messages"])
        anteriores = len(entrada)
        resultado = await self._agente_real().ainvoke({"messages": entrada}, config)

        novas = resultado["messages"][anteriores:] or resultado["messages"][-1:]
        if (uso := uso_de(novas)) is not None:
            await emitir_de(config, "llm_usage", no=self.nome, **uso)

        return {"messages": [resultado["messages"][-1]]}
