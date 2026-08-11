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
from datetime import UTC, datetime

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from ..analytics import emitir
from ..config import settings
from ..convites import VALIDADE_DIAS, criar_convite, usar_convite
from ..core.pipeline import processar_entrada
from ..identity import identidade_do_membro, resolver_membro
from ..notify import registrar_adapter
from ..observability import session_id_de
from . import transcricao
from .contract import (
    TELEGRAM_CAPS,
    Choice,
    Confirmation,
    InboundMessage,
    OutboundMessage,
    RenderMode,
    plan_rendering,
    render_numbered_text,
)

log = logging.getLogger(__name__)


class TelegramAdapter:
    caps = TELEGRAM_CAPS

    def __init__(self, grafo) -> None:
        self.grafo = grafo
        self.bot = Bot(token=settings.telegram_bot_token)
        self.dp = Dispatcher()
        self._buffers: dict[int, list[str]] = {}
        self._tarefas: dict[int, asyncio.Task] = {}
        self._veio_audio: dict[int, bool] = {}  # algum item do buffer nasceu de voz?
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

        @self.dp.message(Command("convidar"))
        async def convidar(msg: Message, command: CommandObject) -> None:
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

        @self.dp.message(F.voice | F.audio)
        async def audio(msg: Message) -> None:
            # Transcrição é responsabilidade do ADAPTER (ADR-001): o núcleo só
            # vê texto. Sem GROQ_API_KEY, recusa simpática — o resto funciona.
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
            await self._receber(msg.chat.id, msg.from_user.id, msg.text or "", str(msg.message_id))

        @self.dp.callback_query()
        async def callback(cb: CallbackQuery) -> None:
            await cb.answer()
            # Resposta de botão entra no MESMO pipeline, como texto (o id da opção)
            await self._receber(cb.message.chat.id, cb.from_user.id, cb.data or "", f"cb-{cb.id}")

    # ── Debounce + pipeline ──────────────────────────────────────────────

    async def _receber(
        self, chat_id: int, user_id: int, texto: str, message_id: str, de_audio: bool = False
    ) -> None:
        self._buffers.setdefault(user_id, []).append(texto)
        if de_audio:
            self._veio_audio[user_id] = True
        if tarefa := self._tarefas.get(user_id):
            tarefa.cancel()
        self._tarefas[user_id] = asyncio.create_task(
            self._flush_apos(chat_id, user_id, message_id)
        )

    async def _flush_apos(self, chat_id: int, user_id: int, message_id: str) -> None:
        try:
            await asyncio.sleep(settings.debounce_segundos)
        except asyncio.CancelledError:
            return  # chegou mais mensagem; o novo flush cuida de tudo
        textos = self._buffers.pop(user_id, [])
        self._tarefas.pop(user_id, None)
        veio_de_audio = self._veio_audio.pop(user_id, False)
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

        inbound = InboundMessage(
            member_id=membro.id,
            canal="telegram",
            texto="\n".join(textos),
            message_id=message_id,
            timestamp=datetime.now(UTC),
            veio_de_audio=veio_de_audio,
        )
        await self.bot.send_chat_action(chat_id, "typing")
        turn_id, respostas = await processar_entrada(membro, inbound, self.grafo)
        for resposta in respostas:
            await self._enviar_chat(chat_id, resposta)
            await emitir(
                "message_sent",
                membro.id,
                session_id_de(membro.id),
                turn_id,
                canal="telegram",
                tamanho=len(resposta.texto),
            )

    # ── Renderização (contrato → Telegram) ───────────────────────────────

    async def _enviar_chat(self, chat_id: int, msg: OutboundMessage) -> None:
        modo = plan_rendering(msg.interacao, self.caps)
        if modo is RenderMode.PLAIN:
            await self.bot.send_message(chat_id, msg.texto)
            return
        if modo is RenderMode.NUMBERED_TEXT and isinstance(msg.interacao, Choice):
            await self.bot.send_message(chat_id, render_numbered_text(msg.texto, msg.interacao))
            return
        if isinstance(msg.interacao, Confirmation):
            teclado = [[
                InlineKeyboardButton(text=msg.interacao.sim_rotulo, callback_data="sim"),
                InlineKeyboardButton(text=msg.interacao.nao_rotulo, callback_data="nao"),
            ]]
        else:  # Choice em botões
            teclado = [
                [InlineKeyboardButton(text=op.rotulo, callback_data=op.id)]
                for op in msg.interacao.opcoes
            ]
        await self.bot.send_message(
            chat_id, msg.texto, reply_markup=InlineKeyboardMarkup(inline_keyboard=teclado)
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
        log.info("Telegram: long polling iniciado")
        await self.dp.start_polling(self.bot)
