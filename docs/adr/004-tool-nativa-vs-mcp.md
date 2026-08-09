# ADR-004 — Tools: nativa à mão vs. MCP vs. plataforma gerenciada

**Status:** proposto (decidir na fase 2) · 08/2026

## Contexto
O mordomo precisa de Google Calendar, Gmail, TMDB, Todoist… Há três vias:
tool nativa escrita à mão (OAuth incluso), MCP servers prontos via
`langchain-mcp-adapters` (`MultiServerMCPClient` carrega tools de qualquer
servidor MCP direto no LangGraph), ou plataforma gerenciada (Composio/Arcade,
auth gerenciada, dependência SaaS).

## Decisão (proposta)
Escrever UMA integração à mão (Google Calendar — a dor do OAuth ensina; dica:
app OAuth "in production" mesmo sem verificação, senão refresh token expira em
~7 dias no modo testing) e trazer o resto via MCP (Google Workspace MCP cobre
Gmail/Calendar/Tasks). Comparar as duas no portfólio.

## Consequências
+ Aprende as três camadas do mercado 2026 e ganha um comparativo autêntico.
+ MVP não bloqueia: agenda roda em tabela própria até a fase 2.
− MCP server é mais um processo para operar (e observar — bônus didático).
