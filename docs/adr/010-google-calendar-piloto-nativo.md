# ADR-010 — Piloto Google Calendar nativo e determinístico

**Status:** aceito · 08/2026 · concretiza a decisão aberta no ADR-004

## Contexto

O Mordomo já possui uma agenda local, mas a família usa contas Google. Manter
um segundo calendário aumenta a carga mental e logística: o compromisso existe
no chat, mas não aparece no aplicativo que já notifica cada pessoa.

A integração completa poderia começar por Calendar, Tasks, Gmail, Drive, MCP,
escolha de calendários e linguagem natural. Isso ampliaria o risco antes de
provarmos a jornada essencial: uma pessoa autoriza a própria conta e o Mordomo
consegue criar um evento visível nela.

## Decisão

Construir primeiro um piloto pequeno e reversível:

- OAuth individual por `member_id`, iniciado apenas em conversa privada;
- Google Calendar somente, com o escopo `calendar.events`;
- calendário `primary`, sem tela de seleção;
- REST oficial via `httpx`, sem SDK Google nem MCP;
- comandos determinísticos `/google`, `/google_teste` e
  `/google_desconectar`, sem alterar o supervisor, prompts ou agenda atual;
- callback no FastAPI já usado pelo WhatsApp, exposto pelo Caddy somente na
  rota `/integracoes/google/callback`;
- state opaco, armazenado apenas como hash, de uso único e validade curta;
- tokens cifrados com Fernet e chave exclusiva de ambiente;
- idempotência no evento de teste para uma repetição imediata não criar dois;
- analytics categórico sem e-mail, título, link, code, state ou token.

A revogação remota no Google fica fora desta fatia. `/google_desconectar`
apaga os tokens locais e orienta a pessoa a revogar também em
`myaccount.google.com/permissions`.

## Consequências

+ Prova OAuth, armazenamento seguro, API, callback e experiência real sem
  interferir nas jornadas existentes da família.
+ O teste não depende do LLM; falhas de entendimento conversacional não se
  confundem com falhas de autorização ou da API.
+ Menor dependência e menor superfície de permissão.
− Ainda não cria eventos a partir de linguagem natural.
− Usa apenas o calendário principal.
− Desconectar localmente não revoga o grant no Google nesta versão.

## Próximo passo condicionado a evidência

Somente após Humberto concluir e avaliar esta jornada, integrar a tool de agenda
à conta conectada. Seleção de calendário, tarefas e permissões adicionais só
entram quando houver necessidade observada — não por antecipação.
