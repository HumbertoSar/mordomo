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

## Fatia 2 — a agenda da conversa passa a usar a conta conectada

**Status:** aceito · 16/08/2026 · disparado por incidente de produção

O piloto provou OAuth, armazenamento e API, mas ficou preso aos comandos: com a
conta já conectada, um pedido por áudio ("almoço com a Manuzinha da FGV amanhã
das 12h às 16h") respondeu "evento criado", gravou `family_events` e não criou
nada no Google. O fim às 16h também se perdeu: a tabela só tinha `inicio_utc`.

Decisão:

- **quem decide o destino é a tool, nunca o LLM**: existe conexão do membro →
  Google Calendar `primary`; não existe → agenda compartilhada do Mordomo;
- **a resposta nomeia o destino** ("Google Agenda" ou "agenda compartilhada do
  Mordomo"). Ambiguidade aqui é o que faz alguém procurar no celular um evento
  que está noutro lugar;
- **sem fallback silencioso**: Google indisponível para quem está conectado é
  falha declarada, com caminho (tentar de novo / `/google`). Gravar na agenda
  local seria repetir o incidente com outro nome;
- **credencial ilegível não vira agenda local**: quem conectou ouve "reconecte";
- **término opcional** na tool e no prompt (`ate`), com releitura de expressão
  só de hora ("16h") no dia do início e validação de fim > início. Sem término
  dito, **1 hora** — o mesmo padrão do Google Calendar. `family_events` ganha
  `fim_utc` (nulo no histórico: ninguém disse aquele término);
- **idempotência por turno**: o id do evento é determinístico a partir de
  `member_id + turn_id + início + fim + digest do título`, então o retry do
  pipeline (ADR-006) toma 409 do Google em vez de duplicar. Pedido novo é outro
  `turn_id` — e pode virar outro evento, que é o certo;
- **leitura em janela curta** (`dias` normalizado, `singleEvents=true`, teto de
  página) formatada em `America/Sao_Paulo`;
- **analytics categórico** (`destino`, `motivo`, `novo`, `duracao_min`): título,
  local, link e token continuam fora de `product_events` (ADR-005). As funções
  da conversa não emitem nada por conta própria — quem emite é a tool, dentro
  do turno, senão o evento nasceria órfão de `turn_id`.

Continua fora desta fatia: escolher calendário, convidar participantes, editar
ou cancelar evento pelo chat, e a agenda de um membro aparecer para outro.

## Próximo passo condicionado a evidência

Seleção de calendário, tarefas e permissões adicionais só entram quando houver
necessidade observada — não por antecipação.
