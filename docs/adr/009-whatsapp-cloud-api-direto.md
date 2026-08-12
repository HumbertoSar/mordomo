# ADR-009 — WhatsApp: Cloud API direto (httpx) em vez de pywa

**Status:** aceito · 08/2026 · revisita a escolha de biblioteca do doc v2 (§4.3)

## Contexto

O plano da fase 3 (doc v2 e `channels/whatsapp_stub.py`) previa **pywa** como
biblioteca do adapter: ela traz webhook, validação de assinatura, botões,
listas, templates e mídia prontos, com integração FastAPI.

Ao implementar, três fatos do NOSSO projeto pesaram contra:

1. **Testes sem rede, sem chaves, sem Docker** (regra nº 7). Um adapter que
   importa pywa no topo só roda depois de `uv sync` com a internet de pé. Todo
   o miolo que queremos testar — parsing do payload, dedupe por `wamid`,
   ordenação por timestamp, renderer com `WHATSAPP_CAPS`, janela de 24h — é
   lógica pura que não precisa de biblioteca nenhuma.
2. **O ack < 5s com fila é nosso, não da biblioteca.** A Meta reenvia por até 7
   dias; o desenho que queremos (responder 200 imediatamente, enfileirar,
   deduplicar no banco, processar com o `_lock_da_thread` do pipeline) é
   exatamente o que uma camada de conveniência esconde. Com pywa continuaríamos
   escrevendo essa parte — só que por baixo de uma abstração alheia.
3. **A superfície que usamos é pequena.** `POST /{phone_number_id}/messages`
   (texto, interativo, template, mídia), `GET /{media_id}` + download, e o
   `hmac.compare_digest` da stdlib para a assinatura. São ~150 linhas em
   `channels/whatsapp_api.py`, contra uma dependência com ciclo de release
   próprio bem no caminho crítico da família.

## Decisão

Implementar o adapter **direto sobre a Graph API** com `httpx` (já é
dependência, veio da transcrição) e `hmac`/`hashlib` da stdlib.

- `channels/whatsapp_api.py` — cliente HTTP fino. Única coisa que fala com a
  Meta; trocar de biblioteca (ou de versão da API) mexe só aqui.
- `channels/whatsapp.py` — adapter: parsing puro, dedupe, debounce, renderer,
  identidade, statuses → analytics. **Zero import de rede.**
- `channels/whatsapp_webhook.py` — FastAPI só na borda, com **import
  preguiçoso**: `make test` roda sem fastapi instalado.

`fastapi` + `uvicorn` continuam no extra `whatsapp` do `pyproject.toml`; pywa
sai. A versão da Graph API é configurável (`WHATSAPP_API_VERSION`) porque a
Meta descontinua versões a cada ~2 anos.

## Consequências

+ O miolo do canal fica coberto por teste hermético desde o primeiro commit —
  inclusive os casos que só apareceriam em produção (retry de 7 dias da Meta,
  payload fora de ordem, texto acima de 1024 chars).
+ Uma dependência a menos no caminho crítico; erros da Meta chegam crus, com o
  código de erro dela, ao invés de traduzidos.
+ O ADR-001 fica ainda mais literal: **um** módulo conhece a Meta.
− Escrevemos à mão o que pywa daria pronto: paginação de mídia, tipos de
  mensagem que ainda não usamos (localização, contatos, reações), e um dia a
  Groups API. Se a lista crescer muito, reabrir esta decisão é barato — o
  cliente está isolado.
− Precisamos acompanhar o versionamento da Graph API por conta própria
  (calendário: <https://developers.facebook.com/docs/graph-api/changelog>).

## Não decidido aqui

- **Groups API** (ADR-008, §WhatsApp): o `grupo_id` do contrato já existe e o
  parsing preserva o campo, mas criar grupo pelo bot fica para depois do
  canário — a família em conversas privadas já resolve o problema que motivou
  a migração.
