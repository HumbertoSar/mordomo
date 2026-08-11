"""Adapter Telegram (aiogram, long polling — sem servidor público em dev).

Responsabilidades do adapter (e SÓ dele — ADR-001):
  - identidade: telegram user_id → member (desconhecido = recusa educada)
  - debounce: brasileiro manda 4 mensagens de 3 palavras; agregamos por
    DEBOUNCE_SEGUNDOS antes de acordar o agente
  - renderização: OutboundMessage semântica → widgets do Telegram
    (inline keyboard) com degradação via plan_rendering()
  - proatividade: notificar() → send_message direto (Telegram é livre)
  - áudio: fase 2 (transcrição via Whisper/Groq) — hoje recusa simpática
"""

import asyncio
import io
import logging
import re
from datetime import UTC, datetime

from aiogram import Bot, Dispatcher, F
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from sqlalchemy import select

from ..analytics import emitir
from ..config import settings
from ..convites import VALIDADE_DIAS, criar_convite, usar_convite
from ..core.pipeline import processar_entrada
from ..db.models import Document
from ..db.session import Sessao
from ..identity import identidade_do_membro, resolver_membro
from ..notify import registrar_adapter
from ..observability import session_de_grupo, session_id_de
from . import transcricao
from .contract import (
    TELEGRAM_CAPS,
    Choice,
    Confirmation,
    InboundMessage,
    OutboundMessage,
    RenderMode,
    fatiar_texto,
    plan_rendering,
    render_numbered_text,
)

log = logging.getLogger(__name__)

# A legenda vira o NOME do documento, mas gente escreve instrução: "Guardar essa
# imagem como Carteirinha do Plano" (caso real, 11/08). Tiramos o verbo e as
# palavras de ligação; se sobrar nada, ficamos com a legenda original.
_RGX_INSTRUCAO_LEGENDA = re.compile(
    r"^(?:por favor[,\s]+)?(?:pode(?:ria)?\s+)?"
    r"(?:guardar?|salvar?|anotar?|arquivar?)\s*"
    r"(?:ess[ae]|est[ae]|o|a)?\s*(?:imagem|foto|arquivo|documento)?\s*"
    r"(?:como|de|:)?\s*",
    re.IGNORECASE,
)


def _nome_da_legenda(legenda: str) -> str:
    nome = _RGX_INSTRUCAO_LEGENDA.sub("", legenda.strip(), count=1).strip()
    return nome or legenda.strip()


def _dias_do_dashboard(args: str | None, padrao: int = 30) -> int | None:
    """Janela do /dashboard a partir do argumento. None = inválido (o handler
    responde com a dica de uso); fora da faixa vira o limite mais próximo."""
    if not args:
        return padrao
    try:
        dias = int(args.split()[0])
    except ValueError:
        return None
    return max(1, min(365, dias))


def _direcionado_ao_bot(texto: str, username: str, reply_ao_bot: bool) -> tuple[bool, str]:
    """ADR-008: em grupo o mordomo só age quando chamado — menção ao @username
    (removida do texto) ou reply a uma mensagem dele. Devolve (é_para_mim, texto)."""
    if not username:
        return False, texto
    padrao = re.compile(rf"@{re.escape(username)}\b", re.IGNORECASE)
    if padrao.search(texto):
        return True, padrao.sub("", texto).strip() or texto
    return reply_ao_bot, texto


class TelegramAdapter:
    caps = TELEGRAM_CAPS

    def __init__(self, grafo) -> None:
        self.grafo = grafo
        self.bot = Bot(token=settings.telegram_bot_token)
        self.dp = Dispatcher()
        # Buffers por (chat, usuário): a mesma pessoa pode conversar no privado
        # e no grupo ao mesmo tempo sem os debounces se misturarem (ADR-008)
        self._buffers: dict[tuple[int, int], list[str]] = {}
        self._tarefas: dict[tuple[int, int], asyncio.Task] = {}
        self._veio_audio: dict[tuple[int, int], bool] = {}
        self._username: str = ""  # preenchido no start() via get_me()
        self._registrar_handlers()
        registrar_adapter(self)

    # ── Handlers ─────────────────────────────────────────────────────────

    def _registrar_handlers(self) -> None:
        @self.dp.message(CommandStart())
        async def start(msg: Message) -> None:
            membro = await resolver_membro("telegram", str(msg.from_user.id))
            if membro is None:
                await msg.answer(
                    "Olá! Sou o mordomo da família. 🤵\n"
                    "Ainda não nos conhecemos — peça um código de convite a quem "
                    "administra e mande: /vincular CÓDIGO"
                )
                return
            await msg.answer(
                f"Às ordens, {membro.nome}! 🤵\n"
                "Posso criar lembretes (\"me lembra amanhã às 8h de…\") e cuidar "
                "da agenda (\"o que temos sábado?\")."
            )

        @self.dp.message(Command("dashboard"))
        async def dashboard(msg: Message, command: CommandObject) -> None:
            # Gestão à vista sob demanda: gera com o dado FRESCO do banco de
            # produção e envia o HTML — sempre atualizado, sem infra nova.
            membro = await resolver_membro("telegram", str(msg.from_user.id))
            if membro is None or membro.papel != "adulto":
                await msg.answer("O painel de gestão é coisa de adulto por aqui. 😉")
                return
            # /dashboard 7 muda a janela SEM pôr filtro (JS) dentro do HTML —
            # o arquivo continua estático e autocontido, quem filtra é a geração
            dias = _dias_do_dashboard(command.args)
            if dias is None:
                await msg.answer("Uso: /dashboard [dias] — por exemplo, /dashboard 7. 🤵")
                return
            from ..reporting.dashboard import gerar_html

            await self.bot.send_chat_action(msg.chat.id, "upload_document")
            conteudo = await gerar_html(dias)
            await msg.answer_document(
                BufferedInputFile(conteudo.encode("utf-8"), filename="mordomo-dashboard.html"),
                caption=f"Gestão à vista dos últimos {dias} dias — abra no navegador. 📊",
            )
            await emitir("dashboard_sent", membro.id, session_id_de(membro.id), dias=dias)

        @self.dp.message(Command("curadoria"))
        async def curadoria_cmd(msg: Message) -> None:
            membro = await resolver_membro("telegram", str(msg.from_user.id))
            if membro is None or membro.papel != "adulto":
                await msg.answer("A curadoria é coisa de adulto por aqui. 😉")
                return
            from ..curadoria import rodar_curadoria

            await self.bot.send_chat_action(msg.chat.id, "typing")
            achados = await rodar_curadoria()
            n = len(achados["expressoes_de_data_falhas"])
            await msg.answer(
                f"Curadoria rodada! {n} caso(s) de eval proposto(s)"
                + (" — issue aberta no GitHub." if n else " — semana limpa. ✨")
            )

        @self.dp.message(Command("convidar"))
        async def convidar(msg: Message, command: CommandObject) -> None:
            if msg.chat.type != "private":
                # O código é o ÚNICO segredo do onboarding — digitado no grupo,
                # qualquer presente (até não-membro) se vincula antes do convidado.
                await msg.answer("Convite é assunto de privado — me chame lá. 🤫")
                return
            membro = await resolver_membro("telegram", str(msg.from_user.id))
            if membro is None:
                await msg.answer("Ainda não nos conhecemos — convites são só para a família. 🤝")
                return
            partes = (command.args or "").split()
            papel = "adulto"
            if partes and partes[-1].lower() in ("adulto", "crianca", "criança"):
                papel = "crianca" if partes.pop().lower().startswith("crian") else "adulto"
            nome = " ".join(partes).strip()
            if not nome:
                await msg.answer(
                    "Uso: /convidar NOME [adulto|crianca]\n"
                    "Ex.: /convidar Maria adulto · /convidar João crianca"
                )
                return
            codigo = await criar_convite(membro, nome, papel)
            if codigo is None:
                await msg.answer("Convites são coisa de adulto por aqui. 😉")
                return
            await msg.answer(
                f"Convite criado para {nome} ({papel}), válido por {VALIDADE_DIAS} dias.\n"
                f"Peça para a pessoa me mandar:\n\n/vincular {codigo}"
            )

        @self.dp.message(Command("vincular"))
        async def vincular(msg: Message, command: CommandObject) -> None:
            if msg.chat.type != "private":
                await msg.answer("Vinculação é assunto de privado — me chame lá. 🤫")
                return
            codigo = (command.args or "").strip()
            if not codigo:
                await msg.answer("Uso: /vincular CÓDIGO (peça o código a quem administra o mordomo).")
                return
            membro, motivo = await usar_convite(codigo, "telegram", str(msg.from_user.id))
            respostas = {
                "ok": f"Bem-vindo(a) à família, {membro.nome if membro else ''}! 🤵 "
                      "Já pode me pedir lembretes e consultar a agenda.",
                "ja_cadastrado": f"Nós já nos conhecemos, {membro.nome if membro else ''}! Às ordens.",
                "codigo_invalido": "Esse código não confere. Confira com quem convidou você. 🤔",
                "ja_usado": "Esse código já foi usado. Peça um novo convite.",
                "expirado": "Esse código expirou. Peça um novo convite.",
            }
            await msg.answer(respostas[motivo])

        @self.dp.message(F.photo | F.document)
        async def documento(msg: Message) -> None:
            # Guardar documento é ação de ADAPTER: os bytes vão direto ao banco,
            # sem passar pelo LLM (ADR-005). A legenda é o nome do documento.
            legenda = msg.caption or ""
            if msg.chat.type != "private":
                # ADR-008: foto compartilhada no grupo NÃO vira documento por
                # acidente — só com menção explícita na legenda.
                para_mim, legenda = _direcionado_ao_bot(legenda, self._username, False)
                if not para_mim:
                    return
                msg = msg.model_copy(update={"caption": legenda})
            membro = await resolver_membro("telegram", str(msg.from_user.id))
            if membro is None:
                await msg.answer("Ainda não nos conhecemos — peça um convite e mande /vincular CÓDIGO. 🤝")
                return
            nome = _nome_da_legenda(msg.caption or "")
            if not nome:
                await msg.answer(
                    "Recebi o arquivo, mas preciso de um nome! Reenvie com uma "
                    "legenda, ex.: \"RG do Davi\" ou \"carteirinha do plano\". 📎"
                )
                return
            if msg.photo:
                arquivo, mime = msg.photo[-1], "image/jpeg"  # [-1] = maior resolução
            else:
                arquivo, mime = msg.document, (msg.document.mime_type or "application/octet-stream")
            buffer = io.BytesIO()
            await self.bot.download(arquivo.file_id, destination=buffer)
            dados = buffer.getvalue()
            async with Sessao() as s:
                res = await s.execute(
                    select(Document).where(Document.nome.ilike(nome), Document.dono == membro.id)
                )
                doc = res.scalar_one_or_none()
                if doc is not None:
                    doc.dados, doc.mime, doc.tamanho = dados, mime, len(dados)
                    doc.telegram_file_id = arquivo.file_id
                    acao = "atualizado"
                else:
                    doc = Document(
                        nome=nome[:120], dono=membro.id, mime=mime, tamanho=len(dados),
                        dados=dados, telegram_file_id=arquivo.file_id,
                    )
                    s.add(doc)
                    acao = "guardado"
                await s.commit()
            await emitir(
                "document_stored", membro.id, session_id_de(membro.id),
                nome=nome, mime=mime, tamanho=len(dados),
            )
            await msg.answer(
                f"Documento \"{nome}\" {acao} no cofre. 🗄️ "
                f"É só pedir: \"me manda o {nome}\"."
            )

        @self.dp.message(F.voice | F.audio)
        async def audio(msg: Message) -> None:
            # Transcrição é responsabilidade do ADAPTER (ADR-001): o núcleo só
            # vê texto. Sem GROQ_API_KEY, recusa simpática — o resto funciona.
            if msg.chat.type != "private":
                return  # ADR-008: não dá para mencionar o bot dentro da voz
            if not transcricao.disponivel():
                await msg.answer(
                    "Áudio ainda não entra na minha alçada — por ora, escreva. 🙏"
                )
                return
            arquivo = msg.voice or msg.audio
            buffer = io.BytesIO()
            await self.bot.download(arquivo.file_id, destination=buffer)
            texto = await transcricao.transcrever(buffer.getvalue())
            if not texto:
                await msg.answer("Não consegui entender o áudio. 😅 Pode escrever?")
                return
            await self._receber(
                msg.chat.id, msg.from_user.id, texto, str(msg.message_id), de_audio=True
            )

        @self.dp.message(F.text)
        async def texto(msg: Message) -> None:
            corpo = msg.text or ""
            em_grupo = msg.chat.type != "private"
            if em_grupo:
                reply_ao_bot = bool(
                    msg.reply_to_message
                    and msg.reply_to_message.from_user
                    and msg.reply_to_message.from_user.id == self.bot.id
                )
                para_mim, corpo = _direcionado_ao_bot(corpo, self._username, reply_ao_bot)
                if not para_mim:
                    return  # conversa da família — o mordomo não se intromete
            await self._receber(
                msg.chat.id, msg.from_user.id, corpo, str(msg.message_id), em_grupo=em_grupo
            )

        @self.dp.callback_query()
        async def callback(cb: CallbackQuery) -> None:
            await cb.answer()
            # Resposta de botão entra no MESMO pipeline, como texto (o id da opção)
            await self._receber(cb.message.chat.id, cb.from_user.id, cb.data or "", f"cb-{cb.id}")

    # ── Debounce + pipeline ──────────────────────────────────────────────

    async def _receber(
        self,
        chat_id: int,
        user_id: int,
        texto: str,
        message_id: str,
        de_audio: bool = False,
        em_grupo: bool = False,
    ) -> None:
        chave = (chat_id, user_id)
        self._buffers.setdefault(chave, []).append(texto)
        if de_audio:
            self._veio_audio[chave] = True
        if tarefa := self._tarefas.get(chave):
            tarefa.cancel()
        self._tarefas[chave] = asyncio.create_task(
            self._flush_apos(chat_id, user_id, message_id, em_grupo)
        )

    async def _flush_apos(
        self, chat_id: int, user_id: int, message_id: str, em_grupo: bool
    ) -> None:
        try:
            await asyncio.sleep(settings.debounce_segundos)
        except asyncio.CancelledError:
            return  # chegou mais mensagem; o novo flush cuida de tudo
        try:
            await self._flush(chat_id, user_id, message_id, em_grupo)
        except Exception:
            # Task solta (create_task): sem este except a exceção morre no GC
            # ("Task exception was never retrieved") e o usuário fica no
            # "digitando…" eterno. O pipeline cobre erro DENTRO do grafo; isto
            # cobre o resto — canal, banco, usuário que bloqueou o bot.
            log.exception("Falha no flush do chat %s", chat_id)
            try:
                await self.bot.send_message(
                    chat_id, "Ops, tropecei aqui do meu lado. 😅 Pode repetir, por favor?"
                )
            except Exception:  # noqa: BLE001 — até a desculpa pode falhar (bloqueio)
                log.warning("Nem a desculpa saiu para o chat %s", chat_id)

    async def _flush(
        self, chat_id: int, user_id: int, message_id: str, em_grupo: bool
    ) -> None:
        chave = (chat_id, user_id)
        textos = self._buffers.pop(chave, [])
        self._tarefas.pop(chave, None)
        veio_de_audio = self._veio_audio.pop(chave, False)
        if not textos:
            return

        membro = await resolver_membro("telegram", str(user_id))
        if membro is None:
            await emitir("unknown_user", canal="telegram", external_id=str(user_id))
            await self.bot.send_message(
                chat_id,
                "Ainda não nos conhecemos! Peça um código de convite a quem "
                "administra o mordomo e mande: /vincular CÓDIGO 🤝",
            )
            return

        texto_final = "\n".join(textos)
        if em_grupo:
            # ADR-008: a thread do grupo mistura vozes — o LLM precisa saber
            # quem disse o quê. No privado o prefixo seria ruído.
            texto_final = f"{membro.nome}: {texto_final}"

        inbound = InboundMessage(
            member_id=membro.id,
            canal="telegram",
            texto=texto_final,
            message_id=message_id,
            timestamp=datetime.now(UTC),
            veio_de_audio=veio_de_audio,
            grupo_id=str(chat_id) if em_grupo else None,
        )
        await self.bot.send_chat_action(chat_id, "typing")
        turn_id, respostas = await processar_entrada(membro, inbound, self.grafo)
        session = session_de_grupo(str(chat_id)) if em_grupo else session_id_de(membro.id)
        for resposta in respostas:
            await self._enviar_chat(chat_id, resposta)
            await emitir(
                "message_sent",
                membro.id,
                session,
                turn_id,
                canal="telegram",
                tamanho=len(resposta.texto),
            )

    # ── Renderização (contrato → Telegram) ───────────────────────────────

    async def _enviar_anexos(self, chat_id: int, msg: OutboundMessage) -> None:
        """Anexos do contrato → send_photo/send_document. Bytes saem do banco;
        o file_id do Telegram é cacheado para reenvio sem re-upload."""
        for anexo in msg.anexos:
            async with Sessao() as s:
                doc = await s.get(Document, anexo.documento_id)
                if doc is None:
                    log.warning("Anexo %s sumiu do banco", anexo.documento_id)
                    continue
                try:
                    if doc.telegram_file_id:
                        enviado = await self._enviar_midia(chat_id, doc.telegram_file_id, doc)
                    else:
                        raise TelegramBadRequest(method=None, message="sem file_id")
                except TelegramBadRequest:
                    # file_id inválido/expirado → sobe os bytes e re-cacheia
                    arquivo = BufferedInputFile(doc.dados, filename=f"{doc.nome}.jpg")
                    enviado = await self._enviar_midia(chat_id, arquivo, doc)
                if enviado is not None:
                    novo_id = (enviado.photo[-1].file_id if enviado.photo
                               else enviado.document.file_id if enviado.document else None)
                    if novo_id and novo_id != doc.telegram_file_id:
                        doc.telegram_file_id = novo_id
                        await s.commit()

    async def _enviar_midia(self, chat_id: int, conteudo, doc: Document):
        if doc.mime.startswith("image/"):
            return await self.bot.send_photo(chat_id, conteudo, caption=doc.nome)
        return await self.bot.send_document(chat_id, conteudo, caption=doc.nome)

    async def _enviar_chat(self, chat_id: int, msg: OutboundMessage) -> None:
        if msg.anexos:
            await self._enviar_anexos(chat_id, msg)
        modo = plan_rendering(msg.interacao, self.caps)
        texto, teclado = msg.texto, None
        if modo is RenderMode.NUMBERED_TEXT and isinstance(msg.interacao, Choice):
            texto = render_numbered_text(msg.texto, msg.interacao)
        elif modo is RenderMode.BUTTONS and isinstance(msg.interacao, Confirmation):
            teclado = [[
                InlineKeyboardButton(text=msg.interacao.sim_rotulo, callback_data="sim"),
                InlineKeyboardButton(text=msg.interacao.nao_rotulo, callback_data="nao"),
            ]]
        elif modo is RenderMode.BUTTONS and isinstance(msg.interacao, Choice):
            teclado = [
                [InlineKeyboardButton(text=op.rotulo, callback_data=op.id)]
                for op in msg.interacao.opcoes
            ]
        # Acima de max_texto o Telegram REJEITA a mensagem inteira (message is
        # too long) — fatiamos; o teclado, quando houver, vai no último bloco.
        partes = fatiar_texto(texto, self.caps.max_texto)
        for parte in partes[:-1]:
            await self.bot.send_message(chat_id, parte)
        await self.bot.send_message(
            chat_id,
            partes[-1],
            reply_markup=InlineKeyboardMarkup(inline_keyboard=teclado) if teclado else None,
        )

    # ── Interface ChannelAdapter ─────────────────────────────────────────

    async def enviar(self, member_id: int, msg: OutboundMessage) -> None:
        ext = await identidade_do_membro(member_id, "telegram")
        if ext:
            await self._enviar_chat(int(ext), msg)

    async def notificar(self, member_id: int, texto: str) -> None:
        # Chat privado no Telegram: chat_id == user_id
        ext = await identidade_do_membro(member_id, "telegram")
        if ext:
            await self.bot.send_message(int(ext), texto)
        else:
            log.warning("Membro %s sem identidade telegram", member_id)

    async def start(self) -> None:
        eu = await self.bot.get_me()
        self._username = (eu.username or "").lower()
        log.info("Telegram: long polling iniciado (@%s; grupos: mencione ou responda)", eu.username)
        await self.dp.start_polling(self.bot)
