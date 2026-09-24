"""Vercel entrypoint: Telegram webhook that turns Meera's notes into LinkedIn drafts.

Telegram POSTs each message to /api/telegram. The handler transcribes voice notes,
asks Gemini for a draft, and replies in the same chat. It never posts anywhere.
"""

import hmac
import logging
import os
from collections import deque
from typing import Optional

import httpx
from fastapi import FastAPI, Request, Response

from drafting import DraftError, transcribe, write_draft

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
WEBHOOK_SECRET = os.environ.get("TELEGRAM_WEBHOOK_SECRET", "")
_allowed = os.environ.get("ALLOWED_TELEGRAM_USER_ID", "").strip()
ALLOWED_USER_ID = int(_allowed) if _allowed.isdigit() else None
TELEGRAM_LIMIT = 4096
MAX_AUDIO_BYTES = 20 * 1024 * 1024  # Telegram bots can't download files larger than 20 MB.

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
logging.getLogger("google_genai").setLevel(logging.ERROR)
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("meera-bot")

app = FastAPI()

# Telegram retries a delivery it thinks failed. Remember recent update IDs so a retry
# landing on the same warm instance doesn't produce a second draft (best effort).
_seen_updates: deque = deque(maxlen=200)


class Telegram:
    def __init__(self, http: httpx.AsyncClient):
        self.http = http
        self.api = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"

    async def call(self, method: str, **params) -> dict:
        r = await self.http.post(f"{self.api}/{method}", json=params)
        data = r.json()
        if not data.get("ok"):
            raise RuntimeError(f"Telegram {method} failed: {data.get('description')}")
        return data["result"]

    async def send(self, chat_id: int, text: str) -> None:
        """Send text, splitting on paragraph breaks if it exceeds Telegram's message limit."""
        chunk = ""
        for para in text.split("\n\n"):
            candidate = f"{chunk}\n\n{para}" if chunk else para
            if len(candidate) <= TELEGRAM_LIMIT:
                chunk = candidate
                continue
            if chunk:
                await self.call("sendMessage", chat_id=chat_id, text=chunk)
            while len(para) > TELEGRAM_LIMIT:
                await self.call("sendMessage", chat_id=chat_id, text=para[:TELEGRAM_LIMIT])
                para = para[TELEGRAM_LIMIT:]
            chunk = para
        if chunk:
            await self.call("sendMessage", chat_id=chat_id, text=chunk)

    async def typing(self, chat_id: int) -> None:
        try:
            await self.call("sendChatAction", chat_id=chat_id, action="typing")
        except Exception:
            pass  # Cosmetic only.

    async def download(self, file_id: str) -> bytes:
        info = await self.call("getFile", file_id=file_id)
        url = f"https://api.telegram.org/file/bot{TELEGRAM_BOT_TOKEN}/{info['file_path']}"
        r = await self.http.get(url)
        r.raise_for_status()
        return r.content


async def handle_message(tg: Telegram, message: dict) -> None:
    chat_id = message["chat"]["id"]
    text: Optional[str] = message.get("text")
    audio: Optional[dict] = message.get("voice") or message.get("audio")

    if text and text.startswith("/"):
        await tg.send(
            chat_id,
            "Send me a voice note or a text note and I'll reply with a LinkedIn draft in Meera's voice. "
            "Nothing is posted anywhere.",
        )
        return

    await tg.typing(chat_id)
    try:
        if audio:
            if audio.get("file_size", 0) > MAX_AUDIO_BYTES:
                raise DraftError("That audio file is over 20 MB, which Telegram won't let bots download. Send a shorter note.")
            await tg.send(chat_id, "Got the voice note. Transcribing...")
            try:
                data = await tg.download(audio["file_id"])
            except Exception:
                log.exception("Telegram download failed")
                raise DraftError("Could not download the voice note from Telegram. Try sending it again.")
            note = await transcribe(data, audio.get("mime_type") or "audio/ogg")
            await tg.send(chat_id, f"Transcript:\n\n{note}")
        elif text:
            note = text.strip()
        else:
            await tg.send(chat_id, "I can only work with voice notes, audio files and text messages.")
            return

        await tg.send(chat_id, "Writing the draft. This can take a minute...")
        await tg.typing(chat_id)
        draft = await write_draft(note)
        await tg.send(chat_id, draft)
        await tg.send(chat_id, "That's the draft. Review and edit before posting on LinkedIn.")
    except DraftError as e:
        log.warning("%s", e)
        await tg.send(chat_id, str(e))
    except Exception:
        log.exception("Unexpected error")
        await tg.send(chat_id, "Something unexpected went wrong while making the draft. Check the Vercel logs.")


@app.post("/api/telegram")
async def telegram_webhook(request: Request) -> Response:
    # Only Telegram knows the secret; it sends it in this header on every delivery.
    header = request.headers.get("x-telegram-bot-api-secret-token", "")
    if not WEBHOOK_SECRET or not hmac.compare_digest(header, WEBHOOK_SECRET):
        return Response(status_code=401)

    update = await request.json()
    update_id = update.get("update_id")
    if update_id in _seen_updates:
        return Response(status_code=200)
    _seen_updates.append(update_id)

    message = update.get("message")
    sender = (message or {}).get("from", {}).get("id")
    if not message or ALLOWED_USER_ID is None or sender != ALLOWED_USER_ID:
        return Response(status_code=200)  # Anyone other than the owner is silently ignored.

    # Always answer 200, even after a failure, so Telegram doesn't redeliver the same note.
    async with httpx.AsyncClient(timeout=60) as http:
        try:
            await handle_message(Telegram(http), message)
        except Exception:
            log.exception("Could not reply in Telegram")
    return Response(status_code=200)


@app.get("/")
async def health() -> dict:
    return {
        "status": "ok",
        "telegram_token_set": bool(TELEGRAM_BOT_TOKEN),
        "gemini_key_set": bool(os.environ.get("GEMINI_API_KEY")),
        "webhook_secret_set": bool(WEBHOOK_SECRET),
        "allowed_user_set": ALLOWED_USER_ID is not None,
    }
