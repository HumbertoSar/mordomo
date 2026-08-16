# Piloto Google Calendar — configuração e teste

Esta é a parte administrativa do piloto. A família não fará estes passos: cada
pessoa apenas usará `/google` e autorizará a própria conta quando o piloto for
liberado.

## Escopo do piloto

- somente Google Calendar;
- somente eventos;
- calendário principal (`primary`);
- sem Gmail, Drive, Tasks ou Contacts;
- a aplicação continua funcionando normalmente se a integração estiver vazia.

## 1. Criar o cliente OAuth no Google Cloud

1. Abra <https://console.cloud.google.com/projectselector2/home/dashboard> e
   crie ou selecione o projeto do Mordomo.
2. Em **APIs e serviços → Biblioteca**, habilite **Google Calendar API**.
3. Configure a tela de consentimento em
   <https://console.cloud.google.com/auth/overview>.
4. Durante o piloto, adicione somente a conta de Humberto em **Público-alvo →
   Usuários de teste**.
5. Em <https://console.cloud.google.com/apis/credentials>, crie um cliente
   **OAuth 2.0 → Aplicativo da Web**.
6. Cadastre exatamente este URI autorizado:

```text
https://mordomo.mvpsardenberg.cloud/integracoes/google/callback
```

Copie o **Client ID** e o **Client Secret** para o gerenciador de senhas. Não os
cole em documentação, issue, chat ou commit.

## 2. Gerar a chave dos tokens

Na VPS, dentro de `/opt/mordomo`, o comando abaixo gera uma chave Fernet:

```bash
uv run python -m mordomo.integracoes.cripto
```

Guarde-a no gerenciador de senhas. Perder essa chave torna os tokens existentes
ilegíveis; trocar a chave exige que as pessoas conectem novamente.

## 3. Variáveis da VPS

Adicionar ao `.env` de produção, sem espaços e sem aspas extras:

```dotenv
GOOGLE_CLIENT_ID=
GOOGLE_CLIENT_SECRET=
GOOGLE_REDIRECT_URI=https://mordomo.mvpsardenberg.cloud/integracoes/google/callback
GOOGLE_TOKEN_KEY=
```

As quatro variáveis trabalham juntas. Se alguma estiver vazia, os comandos
respondem que a integração está indisponível, mas o Mordomo continua subindo.

## 4. Liberar somente o callback no Caddy

No bloco `mordomo.mvpsardenberg.cloud`, ampliar o matcher existente sem expor o
restante do FastAPI:

```caddyfile
@mordomo path /whatsapp/webhook /healthz /integracoes/google/callback
handle @mordomo {
    reverse_proxy 127.0.0.1:8090
}
```

Validar e recarregar o Caddy conforme o procedimento normal da VPS. Não use um
segundo servidor ou uma nova porta pública.

## 5. Aplicar e testar

Depois da revisão, merge e deploy normais, o boot aplicará a migração Alembic.
O canário de Humberto é:

1. enviar `/google` no privado;
2. abrir o link e autorizar no Google;
3. voltar à conversa e enviar `/google_teste`;
4. confirmar no Google Agenda o evento **Teste do Mordomo da Família**;
5. repetir `/google_teste` e confirmar que não nasce outro evento;
6. enviar `/google_desconectar` e conferir a resposta;
7. opcionalmente revogar também em <https://myaccount.google.com/permissions>.

## Evidências esperadas

Sem guardar conteúdo privado, `product_events` deve registrar:

- `google_connection_started`;
- `google_connection_succeeded` ou `google_connection_failed` com motivo
  categórico;
- `google_test_event_created` ou `google_test_event_failed`;
- `google_disconnected`.

Code, state, tokens, link, e-mail e título do evento não podem aparecer em logs
ou analytics.
