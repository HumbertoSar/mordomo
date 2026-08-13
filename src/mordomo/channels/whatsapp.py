"""Adapter WhatsApp (Cloud API oficial, webhook) — fase 3.

Responsabilidades do adapter (e SÓ dele — ADR-001):
  - identidade: wa_id (telefone) → member (desconhecido = recusa educada)
  - dedupe por `wamid`: a Meta reentrega o webhook por ATÉ 7 DIAS
  - ordem: o payload traz timestamp; a Meta NÃO garante ordem de entrega
  - debounce: mesma agregação do Telegram (a família manda 4 mensagens curtas)
  - renderização: OutboundMessage semântica → reply buttons / list message /
    texto numerado, via plan_rendering() + WHATSAPP_CAPS
  - proatividade: dentro da janela de 24h → free-form; fora → template aprovado
  - áudio: a URL da mídia EXPIRA — baixar na hora e transcrever (Whisper/Groq)
  - statuses (sent/delivered/read/failed) → analytics (métricas que o Telegram
    nunca deu: latência até a LEITURA e taxa de falha de entrega)

O que fala com a Meta é `whatsapp_api.py`; o que fala HTTP é
`whatsapp_webhook.py`. Este módulo é lógica — e por isso é testável sem rede.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError

from ..analytics import emitir
from ..config import settings
from ..core.pipeline import processar_entrada
from ..db.models import ChannelIdentity, ChannelMessage, Document
from ..db.session import Sessao
from ..identity import resolver_membro
from ..notify import registrar_adapter
from ..observability import session_id_de
from . import comandos, documentos, transcricao
from .contract import (
    WHATSAPP_CAPS,
    Choice,
    Confirmation,
    InboundMessage,
    OutboundMessage,
    RenderMode,
    fatiar_texto,
    plan_rendering,
    render_numbered_text,
)
from .whatsapp_api import WhatsAppAPI, WhatsAppErro, so_digitos

log = logging.getLogger(__name__)

CANAL = "whatsapp"

# Tipos de mensagem que sabemos ler. O resto (sticker, localização, contato,
# reação) recebe recusa simpática — silêncio parece bot travado.
TIPOS_TEXTO = ("text", "interactive", "button")
TIPOS_MIDIA = ("image", "document", "audio", "voice")


# ── Assinatura do webhook ────────────────────────────────────────────────


def assinatura_valida(corpo: bytes, cabecalho: str | None, app_secret: str) -> bool:
    """Confere o X-Hub-Signature-256 sobre o corpo CRU.

    Sem isto, quem descobrir a URL manda mensagem fingindo ser a família — e o
    mordomo obedece. `compare_digest` porque comparação byte a byte com `==`
    vaza, pelo tempo, quantos bytes bateram."""
    if not app_secret or not cabecalho:
        return False
    esperado = hmac.new(app_secret.encode(), corpo, hashlib.sha256).hexdigest()
    recebido = cabecalho.removeprefix("sha256=").strip()
    return hmac.compare_digest(esperado, recebido)


# ── Parsing do payload (puro) ────────────────────────────────────────────


@dataclass
class EntradaWA:
    wamid: str
    de: str                      # wa_id de quem mandou
    timestamp: datetime
    tipo: str
    texto: str = ""
    media_id: str | None = None
    mime: str = ""
    nome_arquivo: str = ""


@dataclass
class StatusWA:
    wamid: str
    status: str                  # sent | delivered | read | failed
    timestamp: datetime
    destinatario: str
    erro: str = ""
    # O CÓDIGO sozinho não diz nada a quem lê o log às 23h: "erro 130497" mandou
    # a gente procurar na documentação para descobrir "conta restrita de mandar
    # mensagem para usuários deste país". A Meta manda o título junto — guardar
    # custa nada e transforma o log em diagnóstico.
    erro_titulo: str = ""
    categoria: str = ""          # pricing.category — de onde sai o custo


@dataclass
class Lote:
    entradas: list[EntradaWA] = field(default_factory=list)
    statuses: list[StatusWA] = field(default_factory=list)


def _quando(bruto) -> datetime:
    """timestamp da Meta é epoch em SEGUNDOS, como string."""
    try:
        return datetime.fromtimestamp(int(bruto), UTC)
    except (TypeError, ValueError):
        return datetime.now(UTC)


def _texto_da_mensagem(m: dict) -> str:
    """Texto útil de cada tipo. Resposta de botão/lista volta como o ID que o
    núcleo mandou — o mesmo caminho do callback_data do Telegram."""
    tipo = m.get("type")
    if tipo == "text":
        return (m.get("text") or {}).get("body", "")
    if tipo == "button":  # resposta de botão de TEMPLATE
        return (m.get("button") or {}).get("payload") or (m.get("button") or {}).get("text", "")
    if tipo == "interactive":
        interativo = m.get("interactive") or {}
        for chave in ("button_reply", "list_reply"):
            if resposta := interativo.get(chave):
                return resposta.get("id") or resposta.get("title", "")
    return ""


def parse_webhook(payload: dict) -> Lote:
    """Payload da Meta → mensagens e statuses, ORDENADOS por timestamp.

    Um POST pode trazer vários `entry`/`changes`, e a Meta não promete ordem —
    "compra pão" chegando depois de "esquece" inverteria o sentido da conversa.
    A ordenação é estável: empate mantém a ordem do payload."""
    lote = Lote()
    for entry in (payload or {}).get("entry", []) or []:
        for change in entry.get("changes", []) or []:
            valor = change.get("value") or {}
            for m in valor.get("messages", []) or []:
                tipo = m.get("type", "")
                midia = m.get(tipo) if tipo in TIPOS_MIDIA else None
                lote.entradas.append(EntradaWA(
                    wamid=m.get("id", ""),
                    de=m.get("from", ""),
                    timestamp=_quando(m.get("timestamp")),
                    tipo=tipo,
                    # legenda de imagem/documento é o NOME do documento
                    texto=_texto_da_mensagem(m) or (midia or {}).get("caption", ""),
                    media_id=(midia or {}).get("id"),
                    mime=(midia or {}).get("mime_type", ""),
                    nome_arquivo=(midia or {}).get("filename", ""),
                ))
            for s in valor.get("statuses", []) or []:
                erros = s.get("errors") or []
                primeiro = erros[0] if erros else {}
                lote.statuses.append(StatusWA(
                    wamid=s.get("id", ""),
                    status=s.get("status", ""),
                    timestamp=_quando(s.get("timestamp")),
                    destinatario=s.get("recipient_id", ""),
                    erro=str(primeiro.get("code")) if erros else "",
                    # `title` é curto ("Business Account is restricted..."); o
                    # `error_data.details` é a frase completa, quando vem.
                    erro_titulo=(
                        (primeiro.get("error_data") or {}).get("details")
                        or primeiro.get("title", "")
                    ),
                    categoria=((s.get("pricing") or {}).get("category") or ""),
                ))
    lote.entradas.sort(key=lambda e: e.timestamp)
    lote.statuses.sort(key=lambda s: s.timestamp)
    return lote


# ── Identidade (o nono dígito brasileiro) ────────────────────────────────


def variantes_wa_id(wa_id: str) -> list[str]:
    """O mesmo celular aparece com e sem o nono dígito.

    A Meta às vezes devolve o wa_id brasileiro no formato antigo (55 + DDD + 8
    dígitos) mesmo quando o número tem o 9. Quem vinculou por /vincular fica
    gravado como a Meta manda, mas um seed feito à mão (ou o número trocando de
    formato) quebraria a identidade em silêncio — e "não nos conhecemos" para
    quem já é da família é o pior erro possível neste canal."""
    numero = so_digitos(wa_id)
    variantes = [numero]
    if numero.startswith("55") and len(numero) == 12 and numero[4] in "6789":
        variantes.append(numero[:4] + "9" + numero[4:])      # sem 9 → com 9
    elif numero.startswith("55") and len(numero) == 13 and numero[4] == "9":
        variantes.append(numero[:4] + numero[5:])            # com 9 → sem 9
    return variantes


async def resolver_membro_wa(wa_id: str):
    for variante in variantes_wa_id(wa_id):
        if (membro := await resolver_membro(CANAL, variante)) is not None:
            return membro
    return None


# ── Dedupe e janela de 24h (banco) ───────────────────────────────────────


async def registrar_entrada(canal: str, message_id: str) -> bool:
    """True = mensagem NOVA (pode processar). False = reentrega da Meta.

    A trava é o UNIQUE (canal, message_id) no banco, não um set em memória: o
    retry da Meta dura 7 dias e sobrevive a qualquer restart nosso."""
    if not message_id:
        return True
    async with Sessao() as s:
        s.add(ChannelMessage(canal=canal, message_id=message_id))
        try:
            await s.commit()
            return True
        except IntegrityError:
            await s.rollback()
            return False


async def marcar_entrada(canal: str, external_id: str, quando: datetime) -> None:
    """Abre (ou renova) a janela de 24h desta identidade."""
    async with Sessao() as s:
        await s.execute(
            update(ChannelIdentity)
            .where(ChannelIdentity.canal == canal, ChannelIdentity.external_id == external_id)
            .values(ultima_entrada_em=quando)
        )
        await s.commit()


async def identidade_wa(member_id: int) -> ChannelIdentity | None:
    async with Sessao() as s:
        res = await s.execute(
            select(ChannelIdentity).where(
                ChannelIdentity.member_id == member_id, ChannelIdentity.canal == CANAL
            )
        )
        return res.scalar_one_or_none()


def janela_aberta(ultima_entrada: datetime | None, horas: int = 24) -> bool:
    """Regra da Meta: só há free-form nas N horas seguintes à última mensagem
    DO USUÁRIO. Fora disso, template aprovado ou nada."""
    if ultima_entrada is None:
        return False
    return datetime.now(UTC) - ultima_entrada < timedelta(hours=horas)


async def limpar_dedupe(dias: int = 30) -> int:
    """A janela de retry da Meta é de 7 dias; guardamos 30 e apagamos o resto —
    a tabela cresce uma linha por mensagem da família, para sempre, senão."""
    from sqlalchemy import delete

    async with Sessao() as s:
        res = await s.execute(
            delete(ChannelMessage).where(
                ChannelMessage.criado_em < datetime.now(UTC) - timedelta(days=dias)
            )
        )
        await s.commit()
        return res.rowcount or 0


# ── Adapter ──────────────────────────────────────────────────────────────


class WhatsAppAdapter:
    caps = WHATSAPP_CAPS

    def __init__(self, grafo, api: WhatsAppAPI | None = None) -> None:
        self.grafo = grafo
        self.api = api or WhatsAppAPI(
            token=settings.whatsapp_token,
            phone_number_id=settings.whatsapp_phone_number_id,
            versao=settings.whatsapp_api_version,
        )
        # Fila: o webhook devolve 200 em milissegundos e o processamento (3-8s
        # de LLM) acontece aqui atrás. Sem isso a Meta considera timeout e
        # REENTREGA — a mesma pergunta, de novo, enquanto ainda respondemos.
        self.fila: asyncio.Queue[dict] = asyncio.Queue()
        self._consumidor: asyncio.Task | None = None
        self._buffers: dict[str, list[str]] = {}
        self._tarefas: dict[str, asyncio.Task] = {}
        self._veio_audio: dict[str, bool] = {}
        self._ultimo_wamid: dict[str, str] = {}
        registrar_adapter(self)

    # ── Ciclo de vida ────────────────────────────────────────────────────

    async def preparar(self) -> None:
        """Sobe o consumidor da fila e limpa o dedupe velho. NÃO bloqueia —
        os testes chamam isto e mandam payload direto, sem HTTP."""
        self._consumidor = asyncio.create_task(self._consumir())
        removidas = await limpar_dedupe()
        log.info(
            "WhatsApp: canal ativo (número %s, API %s; dedupe limpou %d)",
            settings.whatsapp_phone_number_id, settings.whatsapp_api_version, removidas,
        )

    async def start(self) -> None:
        """Prepara e serve o webhook. Bloqueia — igual ao long polling do
        Telegram, para o main.py tratar os dois canais do mesmo jeito."""
        await self.preparar()
        from .whatsapp_webhook import servir

        await servir(self)

    async def parar(self) -> None:
        if self._consumidor:
            self._consumidor.cancel()
        await self.api.fechar()

    def enfileirar(self, payload: dict) -> None:
        """Chamado pelo webhook, ANTES de responder 200. Nunca bloqueia."""
        self.fila.put_nowait(payload)

    async def _consumir(self) -> None:
        while True:
            payload = await self.fila.get()
            try:
                await self.processar(payload)
            except Exception:
                # Um payload ruim não pode matar o consumidor — seria o canal
                # inteiro caindo em silêncio até o próximo deploy.
                log.exception("Falha ao processar payload do WhatsApp")
            finally:
                self.fila.task_done()

    # ── Entrada ──────────────────────────────────────────────────────────

    async def processar(self, payload: dict) -> None:
        lote = parse_webhook(payload)
        for status in lote.statuses:
            await self._registrar_status(status)
        for entrada in lote.entradas:
            if not await registrar_entrada(CANAL, entrada.wamid):
                # Reentrega da Meta: o turno original já rodou. Registrar (em
                # vez de ignorar calado) é o que permite ver no dashboard se
                # estamos demorando a responder 200.
                await emitir("message_duplicated", canal=CANAL, wamid=entrada.wamid)
                log.info("Webhook reentregue pela Meta, ignorado: %s", entrada.wamid)
                continue
            try:
                await self._processar_entrada(entrada)
            except Exception:
                log.exception("Falha na mensagem %s", entrada.wamid)
                await self._desculpa(entrada.de)

    async def _processar_entrada(self, entrada: EntradaWA) -> None:
        # Dois checks azuis + "digitando…": é o único sinal de vida entre a
        # pergunta e a resposta. Vale até para quem ainda não conhecemos —
        # a pessoa precisa ver que a mensagem chegou.
        await self.api.marcar_lido(entrada.wamid)

        texto = entrada.texto.strip() if entrada.tipo in TIPOS_TEXTO else ""

        # ONBOARDING ANTES DA IDENTIDADE. `/vincular` e `/conectar` são os
        # comandos de quem AINDA não é conhecido — checar identidade primeiro
        # (como estava) transformava a recusa educada num laço: ela pede
        # "mande /vincular CÓDIGO", a pessoa manda, e recebe a mesma recusa.
        # Quebrou o onboarding inteiro do canal e só apareceu no uso real.
        if texto.startswith("/"):
            resposta = await comandos.responder(CANAL, entrada.de, texto)
            if resposta is not None:
                await self.api.enviar_texto(entrada.de, resposta)
                # Um /conectar bem-sucedido acabou de criar a identidade: a
                # janela de 24h abre agora, não no próximo turno.
                await marcar_entrada(CANAL, entrada.de, entrada.timestamp)
                return

        membro = await resolver_membro_wa(entrada.de)
        if membro is None:
            await emitir("unknown_user", canal=CANAL, external_id=entrada.de)
            await self.api.enviar_texto(entrada.de, comandos.RECUSA_DESCONHECIDO)
            return

        await marcar_entrada(CANAL, entrada.de, entrada.timestamp)

        if entrada.tipo in ("audio", "voice"):
            await self._audio(membro, entrada)
            return
        if entrada.tipo in ("image", "document"):
            await self._documento(membro, entrada)
            return
        if entrada.tipo not in TIPOS_TEXTO:
            await self.api.enviar_texto(
                entrada.de,
                "Esse tipo de mensagem eu ainda não sei ler. 🙏 "
                "Me escreva ou mande um áudio.",
            )
            return

        if not texto:
            return
        # Comando que sobrou daqui é o que `comandos.responder` não conhece
        # (devolveu None lá em cima) — /dashboard e os que precisam de arquivo.
        if texto.startswith("/"):
            await self._comando_local(membro, entrada, texto)
            return

        self._ultimo_wamid[entrada.de] = entrada.wamid
        await self._receber(entrada.de, texto)

    async def _comando_local(self, membro, entrada: EntradaWA, texto: str) -> None:
        """Comandos que precisam de ARQUIVO (e por isso não cabem no módulo
        canal-agnóstico de comandos)."""
        comando, _, args = texto[1:].partition(" ")
        if comando.lower() != "dashboard":
            await self.api.enviar_texto(
                entrada.de, "Não conheço esse comando. Tente /ajuda. 🤵"
            )
            return
        if membro.papel != "adulto":
            await self.api.enviar_texto(entrada.de, "O painel de gestão é coisa de adulto por aqui. 😉")
            return
        dias = 30
        if args.strip():
            try:
                dias = max(1, min(365, int(args.split()[0])))
            except ValueError:
                await self.api.enviar_texto(
                    entrada.de, "Uso: /dashboard [dias] — por exemplo, /dashboard 7. 🤵"
                )
                return
        # Em TEXTO, não como arquivo: a Cloud API não aceita `text/html` como
        # documento — o painel completo continua saindo pelo Telegram.
        from ..reporting.dashboard import gerar_texto

        await self.api.enviar_texto(entrada.de, await gerar_texto(dias))
        await emitir(
            "dashboard_sent", membro.id, session_id_de(membro.id),
            dias=dias, canal=CANAL, formato="texto",
        )

    async def _audio(self, membro, entrada: EntradaWA) -> None:
        """A URL da mídia EXPIRA em minutos — baixamos AGORA, não na hora de
        transcrever (diferença real para o file_id estável do Telegram)."""
        if not transcricao.disponivel():
            await self.api.enviar_texto(
                entrada.de, "Áudio ainda não entra na minha alçada — por ora, escreva. 🙏"
            )
            return
        baixado = await self.api.baixar_midia(entrada.media_id or "")
        if not baixado:
            await self.api.enviar_texto(entrada.de, "Não consegui pegar esse áudio. 😅 Pode escrever?")
            return
        texto = await transcricao.transcrever(baixado[0])
        if not texto:
            await self.api.enviar_texto(entrada.de, "Não consegui entender o áudio. 😅 Pode escrever?")
            return
        self._veio_audio[entrada.de] = True
        self._ultimo_wamid[entrada.de] = entrada.wamid
        await self._receber(entrada.de, texto)

    async def _documento(self, membro, entrada: EntradaWA) -> None:
        nome = documentos.nome_da_legenda(entrada.texto or "")
        if not nome:
            nome = documentos.nome_da_legenda(entrada.nome_arquivo or "")
        if not nome:
            await self.api.enviar_texto(
                entrada.de,
                "Recebi o arquivo, mas preciso de um nome! Reenvie com uma "
                "legenda, ex.: \"RG do Davi\" ou \"carteirinha do plano\". 📎",
            )
            return
        baixado = await self.api.baixar_midia(entrada.media_id or "")
        if not baixado:
            await self.api.enviar_texto(entrada.de, "Não consegui baixar esse arquivo. 😅 Pode reenviar?")
            return
        dados, mime = baixado
        acao = await documentos.guardar(membro, nome, dados, entrada.mime or mime)
        await self.api.enviar_texto(
            entrada.de,
            f"Documento \"{nome}\" {acao} no cofre. 🗄️ É só pedir: \"me manda o {nome}\".",
        )

    # ── Debounce + pipeline ──────────────────────────────────────────────

    async def _receber(self, wa_id: str, texto: str) -> None:
        self._buffers.setdefault(wa_id, []).append(texto)
        if tarefa := self._tarefas.get(wa_id):
            tarefa.cancel()
        self._tarefas[wa_id] = asyncio.create_task(self._flush_apos(wa_id))

    async def _flush_apos(self, wa_id: str) -> None:
        try:
            await asyncio.sleep(settings.debounce_segundos)
        except asyncio.CancelledError:
            return  # chegou mais mensagem; o novo flush cuida de tudo
        try:
            await self._flush(wa_id)
        except Exception:
            # Task solta: sem este except a exceção morre no GC e o usuário
            # fica esperando para sempre.
            log.exception("Falha no flush do %s", wa_id)
            await self._desculpa(wa_id)

    async def _flush(self, wa_id: str) -> None:
        textos = self._buffers.pop(wa_id, [])
        self._tarefas.pop(wa_id, None)
        veio_de_audio = self._veio_audio.pop(wa_id, False)
        wamid = self._ultimo_wamid.pop(wa_id, "")
        if not textos:
            return
        membro = await resolver_membro_wa(wa_id)
        if membro is None:
            return

        inbound = InboundMessage(
            member_id=membro.id,
            canal=CANAL,
            texto="\n".join(textos),
            message_id=wamid,
            timestamp=datetime.now(UTC),
            veio_de_audio=veio_de_audio,
        )
        turn_id, respostas = await processar_entrada(membro, inbound, self.grafo)
        session = session_id_de(membro.id)
        for resposta in respostas:
            wamid_saida = await self._enviar_wa(wa_id, resposta)
            await emitir(
                "message_sent", membro.id, session, turn_id,
                # O wamid amarra ESTA resposta aos statuses que chegam depois
                # (entregue/lida) — é o que liga o turno à leitura de verdade.
                canal=CANAL, tamanho=len(resposta.texto), wamid=wamid_saida,
            )

    async def _desculpa(self, wa_id: str) -> None:
        try:
            await self.api.enviar_texto(
                wa_id, "Ops, tropecei aqui do meu lado. 😅 Pode repetir, por favor?"
            )
        except Exception:  # noqa: BLE001 — até a desculpa pode falhar
            log.warning("Nem a desculpa saiu para %s", wa_id)

    # ── Renderização (contrato → WhatsApp) ───────────────────────────────

    async def _enviar_wa(self, wa_id: str, msg: OutboundMessage) -> str | None:
        """Devolve o wamid da ÚLTIMA mensagem enviada (a que carrega a
        pergunta) — é por ele que os statuses de entrega/leitura se casam."""
        for anexo in msg.anexos:
            await self._enviar_anexo(wa_id, anexo)

        modo = plan_rendering(msg.interacao, self.caps)
        texto = msg.texto
        if modo is RenderMode.NUMBERED_TEXT and isinstance(msg.interacao, Choice):
            texto = render_numbered_text(msg.texto, msg.interacao)

        # Acima do limite a API REJEITA a mensagem inteira; fatiamos e a
        # interação (botões/lista) vai no ÚLTIMO pedaço — onde a pergunta está.
        partes = fatiar_texto(texto, self.caps.max_texto)
        for parte in partes[:-1]:
            await self.api.enviar_texto(wa_id, parte)
        ultima = partes[-1]

        if modo is RenderMode.BUTTONS and isinstance(msg.interacao, Confirmation):
            return await self.api.enviar_botoes(
                wa_id, ultima,
                [("sim", msg.interacao.sim_rotulo), ("nao", msg.interacao.nao_rotulo)],
            )
        if modo is RenderMode.BUTTONS and isinstance(msg.interacao, Choice):
            return await self.api.enviar_botoes(
                wa_id, ultima, [(op.id, op.rotulo) for op in msg.interacao.opcoes]
            )
        if modo is RenderMode.LIST and isinstance(msg.interacao, Choice):
            return await self.api.enviar_lista(
                wa_id, ultima, [(op.id, op.rotulo) for op in msg.interacao.opcoes]
            )
        return await self.api.enviar_texto(wa_id, ultima)

    async def _enviar_anexo(self, wa_id: str, anexo) -> None:
        """Bytes saem do banco e sobem a cada envio.

        Sem cache de media_id (o análogo do telegram_file_id) de propósito: o id
        da Meta expira em 30 dias e uma coluna a mais no Document custaria
        migração para economizar um upload de RG por mês."""
        async with Sessao() as s:
            doc = await s.get(Document, anexo.documento_id)
            if doc is None:
                log.warning("Anexo %s sumiu do banco", anexo.documento_id)
                return
            dados, mime, nome = doc.dados, doc.mime, doc.nome
        media_id = await self.api.subir_midia(dados, mime, nome)
        if media_id:
            await self.api.enviar_midia(wa_id, media_id, mime, legenda=nome, nome=nome)

    # ── Statuses (observabilidade nova do canal) ─────────────────────────

    async def _registrar_status(self, status: StatusWA) -> None:
        """sent → delivered → read (ou failed). Não existe no Telegram: é o que
        habilita "latência até a leitura" e "taxa de falha de entrega".

        Fora de turno por desenho (o status chega minutos depois; casa pelo
        wamid) — por isso `message_status` está em SEM_TURNO_POR_DESENHO."""
        membro = await resolver_membro_wa(status.destinatario)
        await emitir(
            "message_status",
            membro.id if membro else None,
            session_id_de(membro.id) if membro else None,
            canal=CANAL,
            status=status.status,
            wamid=status.wamid,
            erro=status.erro,
            erro_titulo=status.erro_titulo,
            categoria=status.categoria,
            # Relógio da META, não o nosso: a diferença entre o "sent" e o
            # "read" do mesmo wamid é a latência até a LEITURA — métrica que o
            # Telegram nunca deu. Nosso `ts` mede quando o webhook chegou.
            ts_canal=int(status.timestamp.timestamp()),
        )
        if status.status == "failed":
            log.warning(
                "WhatsApp não entregou %s — erro %s: %s",
                status.wamid, status.erro, status.erro_titulo or "(sem detalhe)",
            )

    # ── Interface ChannelAdapter ─────────────────────────────────────────

    async def enviar(self, member_id: int, msg: OutboundMessage) -> None:
        identidade = await identidade_wa(member_id)
        if identidade:
            await self._enviar_wa(identidade.external_id, msg)

    async def notificar(self, member_id: int, texto: str) -> None:
        """A ÚNICA diferença semântica do WhatsApp para o núcleo (ADR-001).

        Janela de 24h aberta → mensagem normal. Fechada → template APROVADO,
        que é imutável e tem um parâmetro só. Sem template configurado, o
        proativo é engolido com aviso no log: melhor não avisar do que estourar
        um erro da Meta a cada lembrete."""
        identidade = await identidade_wa(member_id)
        if identidade is None:
            log.warning("Membro %s sem identidade whatsapp", member_id)
            return
        aberta = janela_aberta(identidade.ultima_entrada_em, settings.whatsapp_janela_horas)
        if aberta:
            await self.api.enviar_texto(identidade.external_id, texto)
            await emitir("proactive_channel", member_id, canal=CANAL, modo="free_form")
            return
        template = settings.whatsapp_template_lembrete
        if not template:
            log.warning(
                "Membro %s fora da janela de 24h e sem template — proativo não enviado",
                member_id,
            )
            return
        try:
            await self.api.enviar_template(
                identidade.external_id,
                template,
                settings.whatsapp_template_idioma,
                # O template é "Lembrete: {{1}}" — o texto inteiro cabe num
                # parâmetro só. Quebra de linha não é permitida em parâmetro.
                [texto.replace("\n", " ")[:900]],
            )
        except WhatsAppErro as erro:
            # 132001 = template não existe/não aprovado no idioma. É erro de
            # CONFIGURAÇÃO (etapa 7 do guia), não de código — precisa aparecer.
            log.error("Template '%s' recusado pela Meta: %s", template, erro)
            raise
        await emitir("proactive_channel", member_id, canal=CANAL, modo="template", template=template)
