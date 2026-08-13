# ADR-008 — Mordomo em grupo: thread do grupo, autor por mensagem

**Status:** aceito · 08/2026

## Contexto

A família quer o mordomo no grupo, não só no privado. Mas o ADR-003 amarra
thread = membro — e num grupo a conversa é COLETIVA: o contexto que importa é
o do grupo ("marca aí" se refere ao que acabou de ser discutido por todos),
enquanto identidade e permissão continuam individuais.

## Decisão

1. **Thread por grupo** (`grupo-{id}`), **autor por mensagem**: `member_id`
   continua vindo do `configurable`, resolvido a cada mensagem pela borda
   (ADR-003 intacto no que importa — identidade nunca vem do LLM). A sessão
   de analytics do grupo é própria (`g{id}:{data}`).
2. **Só responde quando chamado**: menção ao @username ou reply a uma mensagem
   dele. O resto da conversa da família não gera turno, não gera custo e não
   vira ruído. (Requer privacidade do bot DESLIGADA no @BotFather —
   `/setprivacy` → Disable — senão o Telegram nem entrega as menções.)
3. **Autoria no contexto**: no grupo, o texto vai ao grafo prefixado com o nome
   ("Davi: e no sábado?") — a thread mistura vozes e o LLM precisa saber quem
   disse o quê. No privado nada muda.
4. **Desconhecido em grupo** só é respondido se mencionar o bot (recusa educada
   com /vincular) — sem spam para convidados de fora.
5. **Mídia em grupo é ignorada** a menos que a legenda mencione o bot: foto
   compartilhada no grupo da família NÃO pode virar documento do cofre por
   acidente. Áudio em grupo é ignorado (não dá para mencionar dentro da voz).
6. **Canal-agnóstico**: o grupo entra pelo contrato (`InboundMessage.grupo_id`);
   o núcleo não sabe que canal é.

## WhatsApp (fase 3) — VERIFICADO EM 13/08/2026: não dá, e o motivo importa

A Groups API existe na Cloud API, mas **exige Official Business Account
(OBA)** — o status de conta oficial da Meta, que soma verificação de negócio a
uma avaliação de notoriedade da marca. Conta de família não obtém. Além disso:

- o bot **nunca entra em grupo existente**: ele só CRIA grupos, e as pessoas
  entram por **link de convite** (não há endpoint para adicionar participante);
- teto de **8 participantes** por grupo;
- grupos **não suportam mensagens interativas** (botões e listas) — o
  `plan_rendering()` teria que degradar para texto numerado sempre.

Ou seja: no WhatsApp, o mordomo é **1:1 por enquanto**. O desenho deste ADR
continua válido e portável (o `grupo_id` do contrato segue existindo, e o
adapter Telegram usa), mas o adapter WhatsApp não implementa grupo — e não
adianta implementar antes do OBA.

Consequência prática para a família: o que o grupo dava (perguntar junto,
"o que temos sábado?") continua acessível no privado de cada um, porque agenda
e cofre COMPARTILHADO já são por família, não por pessoa. O que se perde é a
conversa coletiva em si.

## Consequências

+ Dinâmica coletiva ("gente, o que temos sábado?") com uma thread que lembra
  a conversa DO GRUPO.
− Lembrete criado no grupo continua PRIVADO do autor (dispara no privado) —
  simples e previsível; "lembrete do grupo" fica para quando alguém pedir.
− Duas fontes de contexto (thread privada vs. do grupo) podem divergir — é o
  esperado: o que se falou no grupo pertence ao grupo.
