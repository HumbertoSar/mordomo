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

## WhatsApp (fase 3)

Bot não entra em grupo EXISTENTE de usuários (segue bloqueado pela Meta). Mas
desde 02/2026 a Cloud API tem **Groups API**: o número do negócio CRIA grupos
de até 8 membros e participa deles. O desenho acima porta direto — o adapter
WhatsApp cria o "grupo da família + mordomo" e preenche `grupo_id`; núcleo,
threads e analytics ficam como estão.

## Consequências

+ Dinâmica coletiva ("gente, o que temos sábado?") com uma thread que lembra
  a conversa DO GRUPO.
− Lembrete criado no grupo continua PRIVADO do autor (dispara no privado) —
  simples e previsível; "lembrete do grupo" fica para quando alguém pedir.
− Duas fontes de contexto (thread privada vs. do grupo) podem divergir — é o
  esperado: o que se falou no grupo pertence ao grupo.
