# Fase 3 — WhatsApp Cloud API: a burocracia da Meta, passo a passo

Este documento é a **parte que só você pode fazer**: criar o app na Meta, obter
as credenciais e apontar o webhook. Nada aqui exige código — mas o `.env` que
sai daqui é o que liga o canal.

> **Regra nº 8 do projeto:** nunca Evolution API / Baileys / WPPConnect. Elas
> automatizam o WhatsApp *pessoal* por engenharia reversa e o risco de ban
> permanente do número é real. Só Cloud API oficial.

**Tempo estimado:** 40–60 min de cliques (etapas 1 a 5) + a espera de aprovação
dos templates (etapa 7, minutos a horas). A etapa 6 (webhook) precisa do
subdomínio já apontando para a VPS — dá para fazer as etapas 1–5 antes e voltar.

---

## Panorama: o que você vai colher

| Etapa | O que sai de lá | Vira no `.env` |
|---|---|---|
| 2 | ID do número de teste | `WHATSAPP_PHONE_NUMBER_ID` |
| 2 | ID da conta WhatsApp Business | `WHATSAPP_WABA_ID` |
| 3 | Token permanente (System User) | `WHATSAPP_TOKEN` |
| 4 | Chave secreta do app | `WHATSAPP_APP_SECRET` |
| 5 | Uma senha que **você inventa** | `WHATSAPP_VERIFY_TOKEN` |
| 6 | (configuração do webhook no painel) | — |
| 7 | Nome dos templates aprovados | `WHATSAPP_TEMPLATE_LEMBRETE` |

Os quatro primeiros são **segredos**: não commitar, só `.env` (regra nº 9).

---

## Etapa 0 — Decisão: número de teste ou chip dedicado?

> 🚨 **APRENDIDO NA PRÁTICA (13/08/2026): o número de teste NÃO serve para uma
> família brasileira.** Ele é americano (+1) e a Meta bloqueia envio para o
> Brasil com o erro **130497** — *"Business account is restricted from
> messaging users in this country"*. O bloqueio vale inclusive **dentro da
> janela de 24h**: testamos com a janela recém-aberta pelo usuário e a resposta
> falhou do mesmo jeito. A entrada funciona (as mensagens chegam ao webhook);
> só a saída é barrada. Para a família, é chip brasileiro ou nada.

**Ainda assim, comece pelo número de teste** se o seu público for de outro país
ou se você só quer validar a infraestrutura: ele prova webhook, assinatura,
templates e o caminho de entrada inteiro sem custo nenhum. Ele conversa com até
**5 números destinatários cadastrados**.

| | Número de teste (grátis) | Chip dedicado |
|---|---|---|
| Custo | zero | um chip/eSIM só para o bot |
| Destinatários | até 5, cadastrados na mão | qualquer um |
| Nome/foto do perfil | genérico da Meta | seu, verificado |
| Some quando? | é permanente, mas não escala | — |

O chip dedicado só se justifica quando existir o **segundo agente** (o do grupo
de amigos), como você já tinha concluído. Para a família, teste basta.

> ⚠️ O número que você cadastrar como **destinatário** não pode ser um número
> já registrado como *remetente* de uma conta WhatsApp Business API. Use os
> celulares normais da família — é o caso comum e funciona.

---

## Etapa 1 — App na Meta for Developers

1. Acesse <https://developers.facebook.com/apps/> com sua conta Facebook
   pessoal (a Meta exige uma; não precisa ter perfil ativo).
2. **Criar app** → caso de uso: **"Outro"** → tipo: **"Empresa" / "Business"**.
   (Se a tela oferecer direto "WhatsApp", pode seguir por ela — o resultado é o
   mesmo app de tipo Business.)
3. Nome do app: `mordomo-familia`. E-mail de contato: o seu.
4. **Portfólio empresarial / Business Portfolio**: selecione um existente ou
   crie na hora. É a "empresa" dona do app e da conta WhatsApp — mesmo para uso
   familiar a Meta exige esse guarda-chuva. Pode ser seu nome.
5. No painel do app: **Adicionar produto → WhatsApp → Configurar**.

Ao final você cai na tela **WhatsApp → Introdução / API Setup**. É a tela mais
importante de todas — deixe aberta.

---

## Etapa 2 — Número de teste e destinatários

Ainda em **WhatsApp → Introdução (API Setup)**:

1. Em **"De" / "From"** já aparece o **número de teste** com um seletor. Abaixo
   dele, dois identificadores:
   - **Identificação do número de telefone / Phone number ID** → copie para
     `WHATSAPP_PHONE_NUMBER_ID` (é um número longo, ~15 dígitos, **não** é o
     telefone em si).
   - **Identificação da conta do WhatsApp Business / WABA ID** → copie para
     `WHATSAPP_WABA_ID` (usado para gerenciar templates).
2. Em **"Para" / "To"** → **Gerenciar lista de números** → adicione o seu
   celular com DDI: `+55 21 9xxxx-xxxx`. A Meta manda um código **pelo próprio
   WhatsApp** — confirme.
3. Repita para os demais celulares da família (limite de 5 no total).
4. Ainda nessa tela existe um botão **Enviar mensagem** que dispara o template
   `hello_world`. **Clique.** Se a mensagem chegar no seu WhatsApp, a metade
   Meta do caminho está provada antes de existir uma linha de código nossa.

> 📌 O `wa_id` de cada pessoa (o identificador que o nosso `channel_identities`
> vai guardar) é o telefone **só com dígitos, com DDI e sem `+`** —
> `5521987654321`. No Brasil há a pegadinha do **nono dígito**: a Meta às vezes
> devolve o número no formato antigo (sem o 9 depois do DDD). Por isso o
> vínculo da família será feito por `/vincular CÓDIGO` — quem diz o `wa_id` é o
> payload da Meta, nunca a digitação — e o código normaliza as duas formas.

---

## Etapa 3 — Token permanente (System User)

O token que aparece na tela de Introdução é **temporário (24h)**. Serve para um
teste com `curl` hoje à noite e nada mais. O token de verdade nasce de um
**usuário do sistema**:

1. <https://business.facebook.com/settings/> → escolha o portfólio empresarial
   da etapa 1.
2. Menu **Usuários → Usuários do sistema** → **Adicionar**.
   - Nome: `mordomo-bot`
   - Função: **Administrador** (mais simples; "Funcionário" funciona se você
     atribuir os ativos corretamente no passo seguinte).
3. Com o usuário do sistema selecionado → **Adicionar ativos**:
   - **Apps** → `mordomo-familia` → permissão **Gerenciar app**.
   - **Contas do WhatsApp** → a WABA da etapa 2 → **controle total**.
   Sem esses dois ativos o token nasce sem poder falar com o seu número.
4. **Gerar novo token**:
   - App: `mordomo-familia`
   - **Expiração: Nunca**
   - Permissões: `whatsapp_business_messaging` (enviar/receber) e
     `whatsapp_business_management` (templates). Se a lista oferecer
     `business_management`, marque também — ajuda no gerenciamento por API.
5. **Copie o token agora.** Ele aparece **uma única vez**. Vai para
   `WHATSAPP_TOKEN` no `.env` da VPS.

> Se perder, não tem "mostrar de novo": gere outro (e o antigo continua válido
> até você revogá-lo — revogue).

---

## Etapa 4 — Chave secreta do app (assinatura do webhook)

1. Painel do app → **Configurações do app → Básico**.
2. **Chave secreta do app / App secret** → **Mostrar** → copie para
   `WHATSAPP_APP_SECRET`.

Para que serve: a Meta assina **todo** POST do webhook com
`X-Hub-Signature-256: sha256=HMAC(app_secret, corpo_cru)`. Sem validar isso,
qualquer um que descubra a sua URL manda mensagem fingindo ser a família — e o
mordomo obedeceria. O nosso webhook **recusa 403** sem assinatura válida.

---

## Etapa 5 — Verify token (você inventa)

Não é uma credencial da Meta: é uma senha compartilhada que a Meta devolve no
GET de verificação para provar que quem responde é você.

```bash
openssl rand -hex 16
```

O resultado vai para `WHATSAPP_VERIFY_TOKEN` no `.env` **e** será digitado no
painel na etapa 6. Precisa bater exatamente.

---

## Etapa 6 — Webhook (precisa do subdomínio pronto)

**Pré-requisitos** (lado nosso, ver `docs/deploy-vps.md`):

- um subdomínio apontando para o IP da VPS — ex.: `mordomo.SEUDOMINIO.com.br`,
  registro **A** no DNS da Hostinger;
- o Caddy do host servindo esse subdomínio com `reverse_proxy` para o container
  (`127.0.0.1:8090`) — **não** suba outro proxy: a 80/443 já é do Caddy que
  serve o storyrender;
- o bot **rodando** na VPS com `WHATSAPP_VERIFY_TOKEN` preenchido (a Meta faz o
  GET de verificação no ato de salvar; se o processo estiver parado, falha).

No painel: **WhatsApp → Configuração / Configuration → Webhook → Editar**:

| Campo | Valor |
|---|---|
| URL de retorno de chamada | `https://mordomo.SEUDOMINIO.com.br/whatsapp/webhook` |
| Token de verificação | o mesmo `WHATSAPP_VERIFY_TOKEN` |

**Verificar e salvar** → a Meta faz um `GET` com `hub.challenge`; o nosso
endpoint devolve o desafio e a tela fica verde.

Depois, em **Campos do webhook / Webhook fields** → **Gerenciar** → assine:

- ✅ **`messages`** — obrigatório. Traz mensagens recebidas **e** os
  `statuses` (sent/delivered/read/failed).

Os demais campos (qualidade do template, limites da conta) são opcionais;
`message_template_status_update` é útil quando você tiver muitos templates.

> 🔁 **A Meta reenvia**: se o nosso endpoint não devolver `200` rápido, ela
> tenta de novo — por até **7 dias**. É por isso que o webhook responde `200`
> antes de processar e que existe dedupe por `wamid` no banco. Sem isso, um
> deploy demorado vira uma enxurrada de lembretes duplicados.

---

## Etapa 7 — Templates (aprovar ANTES de precisar)

Fora da janela de 24h desde a última mensagem **do usuário**, só sai template
aprovado. Como lembrete quase sempre cai fora da janela, ele **precisa** de
template — e a aprovação leva de minutos a horas. Aprove agora.

**WhatsApp Manager** (<https://business.facebook.com/wa/manage/message-templates/>)
→ **Criar modelo**:

Os dois templates do projeto (criados em 13/08/2026, categoria **Utilidade** —
não Marketing: mais barato e aprova melhor — idioma **Português (BR)**):

| Nome | Corpo |
|---|---|
| `lembrete_v1` | `Lembrete do mordomo: {{1}}. Às ordens!` |
| `briefing_v1` | `Bom dia! Seu resumo de hoje: {{1}}. Tenha um ótimo dia!` |

Regras que doem se ignoradas:

- **A variável não pode ficar no FIM do corpo** (nem no começo, nem colada em
  outra). Foi por isso que o `Lembrete: {{1}}` original foi recusado na cara:
  *"This template has too many variables for its length. Variables can't be at
  the start or end of the template."* Daí o `. Às ordens!` no fim — ele existe
  para satisfazer a Meta e, de quebra, soa como o bot.
- **Template aprovado é imutável na prática** — editar joga para nova revisão e
  derruba a aprovação. Por isso o sufixo de versão: mudou o texto → `lembrete_v2`,
  e o `.env` aponta para o novo.
- No editor, o botão **"Add variable"** faz *trim* do texto antes da variável:
  `Lembrete: ` + variável vira `Lembrete:{{1}}`, sem espaço. Confira no preview
  e recoloque o espaço na mão.
- Nada de conteúdo promocional em template de Utilidade.
- A Meta pede um **exemplo** para `{{1}}` — use algo real e inocente ("pagar o
  boleto da escola às 8h"). O exemplo é só para a revisão, não vai ao usuário.
- **Validade padrão de 10 minutos**: se a mensagem de Utilidade não for
  entregue nesse prazo (celular desligado), ela expira — não é cobrada e não
  aparece. Para lembrete isso é até desejável; se algum dia não for, há
  "Message validity period" no formulário.

Preencha `WHATSAPP_TEMPLATE_LEMBRETE=lembrete_v1` no `.env`. O código manda o
texto do lembrete como **um** parâmetro posicional — por isso o "Type of
variable" do formulário fica em **Number** (`{{1}}`), não em "Named".

---

## Verificação de negócio: o que ela REALMENTE exige (levantado em 13/08/2026)

Blogs de revendedor dizem que é obrigatória e que exige CNPJ. **O painel da
Meta diz outra coisa** — na tela do Step 3, textualmente: *"Upload documents
for Meta review (2–10 business days). **Optional but recommended**."*

O que se tem SEM verificar nada (estado atual desta conta, lido no
WhatsApp Manager → Messaging limits):

| | Sem verificação | Com verificação |
|---|---|---|
| Números por portfólio | **2** | 20 |
| Conversas iniciadas pelo negócio | **250 / 24h** | 2.000 → 10.000 → ilimitado |
| Nome de exibição no chat | número | nome do negócio |
| Proteção extra contra banimento | — | sim |

Para uma família, **250 conversas iniciadas por dia é ordens de grandeza mais
do que o necessário** (lembrete é coisa de unidades por dia). Ou seja: dá para
operar sem verificação nenhuma.

Se um dia quiser verificar, os documentos aceitos são mais amplos que "CNPJ":
certificado de constituição, licença comercial, registro fiscal, **extrato
bancário**, **conta de luz/água/internet/telefone** ou relatório de crédito —
desde que mostrem o nome legal do negócio. Só um usuário **admin** pode enviar.

## Custo real para uma família (rate card de 08/2026)

- Resposta dentro da janela de 24h: **grátis** (mensagem de serviço).
- Template de Utilidade dentro da janela: **grátis** também.
- Template de Utilidade FORA da janela (o caso do lembrete): **~US$ 0,0068 por
  mensagem entregue** no Brasil (~R$ 0,04).

Conta de guardanapo: 4 pessoas × 3 lembretes/dia × 30 dias ≈ 360 mensagens ≈
**US$ 2,50/mês** — e isso é o teto, porque todo lembrete que cai dentro de uma
janela aberta sai de graça.

## Etapa 8 — Opt-in e política (o mínimo honesto)

A Meta exige opt-in de quem recebe. Para uma família de 4 pessoas isso é uma
conversa, não um formulário — mas registre:

- cada membro entrou por `/vincular CÓDIGO`, ou seja, **pediu** para ser
  contactado. Esse é o opt-in, e ele fica registrado em `product_events`
  (`invite_used`, com canal).
- diga em voz alta o que o bot guarda (lembretes, agenda, cofre) e que dá para
  sair pedindo — a mesma frase que já está no `/start`.

---

## Custos — o detalhe que muda o desenho

- **Fora da janela de 24h**: cada template enviado é cobrado (conversa de
  Utilidade; centavos de real, mas não zero).
- **Dentro da janela de 24h é grátis**: mensagem não-template não é cobrada
  (a cobrança é por mensagem entregue e só vale para template, desde 07/2025).
  Em 01/10/2026 há reajuste de TARIFA por mercado — não é o fim da gratuidade.
  Ou seja: responder a família custa zero; o que custa é o lembrete que sai
  fora da janela, porque esse precisa de template.
- Consequência prática já assumida no código: proativo **só sai se houver
  conteúdo** (briefing vazio não vira mensagem), e o dashboard vai ganhar a
  dimensão de custo por canal.

Confira a tabela vigente em
<https://developers.facebook.com/docs/whatsapp/pricing> antes de ligar o
briefing para a família toda.

---

## Checklist final do `.env` (VPS)

```bash
WHATSAPP_TOKEN=EAAG...                  # etapa 3 (permanente, System User)
WHATSAPP_PHONE_NUMBER_ID=1234567890     # etapa 2
WHATSAPP_WABA_ID=1234567890             # etapa 2
WHATSAPP_APP_SECRET=abc123...           # etapa 4
WHATSAPP_VERIFY_TOKEN=<openssl rand>    # etapa 5
WHATSAPP_TEMPLATE_LEMBRETE=lembrete_v1  # etapa 7
WHATSAPP_PORTA=8090                     # porta interna atrás do Caddy
```

Teste de fumaça sem o bot (troque os `<...>`):

```bash
curl -X POST "https://graph.facebook.com/v25.0/<PHONE_NUMBER_ID>/messages" -H "Authorization: Bearer <TOKEN>" -H "Content-Type: application/json" -d '{"messaging_product":"whatsapp","to":"<SEU_NUMERO_SO_DIGITOS>","type":"template","template":{"name":"hello_world","language":{"code":"en_US"}}}'
```

Chegou? Então token, número e permissões estão certos, e qualquer erro daqui
em diante é nosso — não da Meta.

---

## Sequência recomendada (o que fazer em que ordem)

1. Etapas 1–5 (colher credenciais) — pode ser hoje, sem VPS.
2. `curl` de fumaça acima — prova o token.
3. Etapa 7 (templates) — mande aprovar cedo, a fila é da Meta.
4. Subdomínio no DNS + bloco no Caddy (`docs/deploy-vps.md`, seção WhatsApp).
5. Deploy do bot com as variáveis → etapa 6 (webhook) → tela verde.
6. **Canário**: só o seu número no WhatsApp por ~1 semana, família segue no
   Telegram, comparando os dois no dashboard (o campo `canal` já viaja em todo
   evento). Só depois migra o resto.

> As etiquetas exatas do painel da Meta mudam de tempos em tempos (a interface
> é traduzida e reorganizada com frequência). Se algum nome aqui não bater com
> o que você está vendo, o caminho conceitual continua o mesmo: app Business →
> produto WhatsApp → número + IDs → System User com os dois ativos → app secret
> → webhook assinado → templates de Utilidade.
