"""Cliente da WhatsApp Cloud API — o ÚNICO módulo que fala com a Meta.

ADR-009: sem pywa. A superfície que usamos é pequena (mandar mensagem, marcar
como lida, baixar/subir mídia) e concentrá-la aqui mantém o resto do canal —
parsing, dedupe, renderer, janela de 24h — como lógica pura, testável sem rede.

Nada aqui conhece o contrato interno (OutboundMessage & cia): isto é transporte.
Quem traduz semântica → payload é o renderer em `whatsapp.py`.

Referência: https://developers.facebook.com/docs/whatsapp/cloud-api/reference
"""

from __future__ import annotations

import logging
import re

import httpx

log = logging.getLogger(__name__)

TIMEOUT = httpx.Timeout(20.0, connect=10.0)

# Limites da própria API (a Meta REJEITA o excedente, não trunca):
LIMITE_BOTAO = 20        # título de reply button
LIMITE_ITEM = 24         # título de linha de lista
LIMITE_DESCRICAO = 72    # descrição de linha de lista
LIMITE_CORPO = 1024      # corpo de mensagem interativa
LIMITE_TEXTO = 4096      # corpo de mensagem de texto simples


class WhatsAppErro(RuntimeError):
    """Erro devolvido pela Graph API (já com o código da Meta na mensagem).

    Existe para o adapter distinguir "a Meta recusou" (ex.: 131047 — fora da
    janela de 24h) de "a rede caiu"."""

    def __init__(self, status: int, corpo: dict | str) -> None:
        detalhe = corpo.get("error", corpo) if isinstance(corpo, dict) else corpo
        self.status = status
        self.codigo = detalhe.get("code") if isinstance(detalhe, dict) else None
        super().__init__(f"WhatsApp {status}: {detalhe}")


def so_digitos(numero: str) -> str:
    """wa_id é o telefone com DDI e SÓ dígitos: +55 (21) 99999-8888 → 5521999998888."""
    return re.sub(r"\D", "", numero or "")


def cortar(texto: str, limite: int) -> str:
    """Corta preservando a intenção: cabe → devolve; não cabe → reticências.

    Usado nos rótulos de botão/lista, onde o excedente não é fatiado em outra
    mensagem (como no corpo) e sim rejeitado pela API."""
    texto = (texto or "").strip()
    if len(texto) <= limite:
        return texto
    return texto[: limite - 1].rstrip() + "…"


class WhatsAppAPI:
    """Chamadas HTTP da Cloud API. Um cliente httpx reaproveitado por conexão.

    O `cliente` é injetável para os testes rodarem contra httpx.MockTransport —
    sem rede, sem chave (regra nº 7 do projeto)."""

    def __init__(
        self,
        token: str,
        phone_number_id: str,
        versao: str = "v25.0",
        cliente: httpx.AsyncClient | None = None,
    ) -> None:
        self.token = token
        self.phone_number_id = phone_number_id
        self.versao = versao
        self.base = f"https://graph.facebook.com/{versao}"
        self._cliente = cliente
        self._proprio = cliente is None

    @property
    def cliente(self) -> httpx.AsyncClient:
        if self._cliente is None:
            self._cliente = httpx.AsyncClient(timeout=TIMEOUT)
        return self._cliente

    async def fechar(self) -> None:
        if self._cliente is not None and self._proprio:
            await self._cliente.aclose()
            self._cliente = None

    @property
    def _auth(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}"}

    # ── Envio ────────────────────────────────────────────────────────────

    async def _mensagem(self, payload: dict) -> str | None:
        """POST /{phone_number_id}/messages → devolve o wamid da mensagem enviada.

        O wamid é a chave para casar o `statuses` que volta depois (entregue,
        lida, falhou) com a mensagem que mandamos."""
        url = f"{self.base}/{self.phone_number_id}/messages"
        corpo = {"messaging_product": "whatsapp", **payload}
        resposta = await self.cliente.post(url, json=corpo, headers=self._auth)
        if resposta.status_code >= 400:
            raise WhatsAppErro(resposta.status_code, _json_seguro(resposta))
        dados = _json_seguro(resposta)
        mensagens = dados.get("messages") if isinstance(dados, dict) else None
        return mensagens[0].get("id") if mensagens else None

    async def enviar_texto(self, para: str, texto: str) -> str | None:
        return await self._mensagem({
            "recipient_type": "individual",
            "to": so_digitos(para),
            "type": "text",
            # preview_url=False: link em lembrete não deve virar card gigante
            "text": {"preview_url": False, "body": texto[:LIMITE_TEXTO]},
        })

    async def enviar_botoes(
        self, para: str, corpo: str, botoes: list[tuple[str, str]]
    ) -> str | None:
        """Até 3 reply buttons (id, rótulo). A resposta volta como
        interactive.button_reply.id — o mesmo id que o núcleo mandou."""
        return await self._mensagem({
            "recipient_type": "individual",
            "to": so_digitos(para),
            "type": "interactive",
            "interactive": {
                "type": "button",
                "body": {"text": cortar(corpo, LIMITE_CORPO)},
                "action": {
                    "buttons": [
                        {"type": "reply", "reply": {"id": bid, "title": cortar(rotulo, LIMITE_BOTAO)}}
                        for bid, rotulo in botoes[:3]
                    ]
                },
            },
        })

    async def enviar_lista(
        self,
        para: str,
        corpo: str,
        opcoes: list[tuple[str, str]],
        rotulo_botao: str = "Escolher",
    ) -> str | None:
        """List message: até 10 linhas. O rótulo do botão que abre a lista tem
        limite de 20 chars — é o mesmo do reply button."""
        return await self._mensagem({
            "recipient_type": "individual",
            "to": so_digitos(para),
            "type": "interactive",
            "interactive": {
                "type": "list",
                "body": {"text": cortar(corpo, LIMITE_CORPO)},
                "action": {
                    "button": cortar(rotulo_botao, LIMITE_BOTAO),
                    "sections": [{
                        "title": "Opções",
                        "rows": [
                            {"id": oid, "title": cortar(rotulo, LIMITE_ITEM)}
                            for oid, rotulo in opcoes[:10]
                        ],
                    }],
                },
            },
        })

    async def enviar_template(
        self, para: str, nome: str, idioma: str = "pt_BR", parametros: list[str] | None = None
    ) -> str | None:
        """Única forma de falar FORA da janela de 24h. O template precisa estar
        APROVADO (docs/whatsapp-fase3.md, etapa 7) — não dá para improvisar."""
        componentes = []
        if parametros:
            componentes.append({
                "type": "body",
                "parameters": [{"type": "text", "text": p} for p in parametros],
            })
        return await self._mensagem({
            "recipient_type": "individual",
            "to": so_digitos(para),
            "type": "template",
            "template": {
                "name": nome,
                "language": {"code": idioma},
                **({"components": componentes} if componentes else {}),
            },
        })

    async def enviar_midia(
        self, para: str, media_id: str, mime: str, legenda: str = "", nome: str = ""
    ) -> str | None:
        """Imagem vai como `image` (aparece na conversa); o resto como
        `document` (o WhatsApp exige `filename` para o arquivo ter nome)."""
        if mime.startswith("image/"):
            conteudo = {"type": "image", "image": {"id": media_id, "caption": legenda[:LIMITE_CORPO]}}
        elif mime.startswith("audio/"):
            conteudo = {"type": "audio", "audio": {"id": media_id}}
        else:
            documento = {"id": media_id, "caption": legenda[:LIMITE_CORPO]}
            if nome:
                documento["filename"] = nome
            conteudo = {"type": "document", "document": documento}
        return await self._mensagem({
            "recipient_type": "individual", "to": so_digitos(para), **conteudo
        })

    # ── Sinais de leitura ────────────────────────────────────────────────

    async def marcar_lido(self, wamid: str, digitando: bool = True) -> None:
        """Dois check azuis + "digitando…". É a única coisa que a família vê
        entre a pergunta e a resposta — sem isso o bot parece morto por 3-8s.

        Falha aqui NUNCA derruba o turno: é cosmético."""
        payload: dict = {"messaging_product": "whatsapp", "status": "read", "message_id": wamid}
        if digitando:
            # O indicador expira sozinho em ~25s ou quando a resposta chega.
            payload["typing_indicator"] = {"type": "text"}
        try:
            url = f"{self.base}/{self.phone_number_id}/messages"
            await self.cliente.post(url, json=payload, headers=self._auth)
        except Exception:
            log.debug("Não consegui marcar como lida (%s) — seguindo", wamid, exc_info=True)

    # ── Mídia ────────────────────────────────────────────────────────────

    async def baixar_midia(self, media_id: str) -> tuple[bytes, str] | None:
        """Duas chamadas: id → URL temporária → bytes.

        A URL EXPIRA (minutos) e exige o mesmo Bearer token — diferente do
        Telegram, onde o file_id é estável. Por isso o áudio é baixado na hora
        de processar, não depois."""
        try:
            meta = await self.cliente.get(f"{self.base}/{media_id}", headers=self._auth)
            if meta.status_code >= 400:
                raise WhatsAppErro(meta.status_code, _json_seguro(meta))
            dados = _json_seguro(meta)
            url = dados.get("url") if isinstance(dados, dict) else None
            if not url:
                return None
            mime = dados.get("mime_type") or "application/octet-stream"
            binario = await self.cliente.get(url, headers=self._auth)
            if binario.status_code >= 400:
                raise WhatsAppErro(binario.status_code, binario.text)
            return binario.content, mime
        except WhatsAppErro:
            raise
        except Exception:
            log.exception("Falha ao baixar mídia %s", media_id)
            return None

    async def subir_midia(self, dados: bytes, mime: str, nome: str = "arquivo") -> str | None:
        """Upload → media_id reutilizável por 30 dias. Guardamos esse id no
        Document (mesma ideia do telegram_file_id) para não subir o RG do Davi
        toda vez que alguém pedir."""
        url = f"{self.base}/{self.phone_number_id}/media"
        arquivos = {"file": (nome, dados, mime)}
        resposta = await self.cliente.post(
            url,
            data={"messaging_product": "whatsapp", "type": mime},
            files=arquivos,
            headers=self._auth,
        )
        if resposta.status_code >= 400:
            raise WhatsAppErro(resposta.status_code, _json_seguro(resposta))
        dados_json = _json_seguro(resposta)
        return dados_json.get("id") if isinstance(dados_json, dict) else None


def _json_seguro(resposta: httpx.Response) -> dict | str:
    """A Meta às vezes devolve HTML (proxy, manutenção) com status de erro —
    json() explodiria por cima do erro que a gente quer LER."""
    try:
        return resposta.json()
    except Exception:  # noqa: BLE001 — ler o erro é mais importante que o tipo dele
        return resposta.text[:500]
