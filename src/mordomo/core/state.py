"""Estado do grafo. Thread do checkpointer = membro (ADR-003):
a memória pertence à PESSOA, não ao canal — migrar de Telegram para
WhatsApp preserva histórico e contexto."""

from langgraph.graph import MessagesState


class EstadoMordomo(MessagesState):
    member_id: int
    member_nome: str
    member_papel: str  # "adulto" | "crianca" — base das permissões
