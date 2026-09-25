"""Vercel entrypoint: Telegram webhook that turns Meera's notes into LinkedIn drafts.

Telegram POSTs each message to /api/telegram. For each note the handler:
transcribes (voice) -> triages the note 0-10 -> looks for a Google News hook ->
drafts the post -> scores the draft on 7 criteria and attaches news sources.
It replies in the same chat and never posts anywhere.
"""

import hmac
import html
import logging
import os
import re
import time
from collections import deque
from typing import Optional

import httpx
from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse

from drafting import (CRITERIA, DraftError, overall, score_draft, substance_capped, transcribe, triage,
                      write_draft)
from news import search_news

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
WEBHOOK_SECRET = os.environ.get("TELEGRAM_WEBHOOK_SECRET", "")
_allowed = os.environ.get("ALLOWED_TELEGRAM_USER_ID", "").strip()
ALLOWED_USER_ID = int(_allowed) if _allowed.isdigit() else None
TRIAGE_MIN_SCORE = int(os.environ.get("TRIAGE_MIN_SCORE", "5"))
FORCE_WORDS = {"draft anyway", "draft it anyway", "force"}
TRANSCRIPT_PREFIX = "Transcript:\n\n"
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
# Last few outcomes, shown at /api/debug. Per instance, so it resets on cold starts.
_events: deque = deque(maxlen=20)


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

    async def send_html(self, chat_id: int, text: str) -> None:
        """Send one short HTML-formatted message (used for the scorecard, so links can be clickable)."""
        try:
            await self.call("sendMessage", chat_id=chat_id, text=text[:TELEGRAM_LIMIT], parse_mode="HTML",
                            link_preview_options={"is_disabled": True})
        except RuntimeError:
            # If Telegram rejects the markup, fall back to plain text rather than losing the scorecard.
            log.exception("HTML message rejected")
            plain = html.unescape(re.sub(r"<[^>]+>", "", text))
            await self.send(chat_id, plain)

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


async def build_report(note: str, draft, news) -> str:
    esc = html.escape
    lines = []
    try:
        card = await score_draft(note, draft.post)
        lines.append(f"<b>Draft score: {overall(card)}/10</b>")
        if substance_capped(card):
            lines.append("(Held down by Worth posting or Evidence: the overall can't be more than "
                         "1 point above the weaker of the two.)")
        for key, label, weight in CRITERIA:
            criterion = getattr(card, key)
            counts = " (counts double)" if weight > 1 else ""
            lines.append(f"{esc(label)}{counts}: <b>{criterion.score}</b>/10. {esc(criterion.note)}")
        if card.unsupported_claims:
            lines.append("\n<b>Not in the note, check these:</b>")
            lines += [f"• {esc(claim)}" for claim in card.unsupported_claims]
    except DraftError as e:
        lines.append(esc(str(e)))
    if draft.news_hook_used:
        by_id = {item.id: item for item in news}
        lines.append("\n<b>News hook sources</b> (cross-check before publishing):")
        for n, source_id in enumerate(draft.source_ids, start=1):
            item = by_id[source_id]
            link = html.escape(item.link, quote=True)
            lines.append(f'{n}. <a href="{link}">{esc(item.title)}</a>\n    {esc(item.publisher)}, {esc(item.date)}')
    else:
        lines.append("\nNo news hook used.")
    lines.append("\nResolve every [VERIFY] before posting on LinkedIn.")
    return "\n".join(lines)


async def handle_message(tg: Telegram, message: dict) -> None:
    chat_id = message["chat"]["id"]
    text: Optional[str] = message.get("text")
    audio: Optional[dict] = message.get("voice") or message.get("audio")

    if text and text.startswith("/"):
        await tg.send(
            chat_id,
            "Send me a voice note or a text note and I'll reply with a scored LinkedIn draft in Meera's voice. "
            "Nothing is posted anywhere.\n\n"
            f"Notes that score below {TRIAGE_MIN_SCORE}/10 at triage aren't drafted. To draft one anyway, "
            "reply to the note (or to its transcript) with: draft anyway",
        )
        return

    await tg.typing(chat_id)
    try:
        forced = False
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
            await tg.send(chat_id, TRANSCRIPT_PREFIX + note)
        elif text and text.strip().lower() in FORCE_WORDS:
            original = (message.get("reply_to_message") or {}).get("text") or ""
            note = original[len(TRANSCRIPT_PREFIX):] if original.startswith(TRANSCRIPT_PREFIX) else original
            if not note.strip():
                await tg.send(chat_id, "To force a draft, reply to the note or its transcript with: draft anyway")
                return
            forced = True
        elif text:
            note = text.strip()
        else:
            await tg.send(chat_id, "I can only work with voice notes, audio files and text messages.")
            return

        # 1. Triage (Gemini Flash): is this note worth drafting, and is there a news angle?
        await tg.typing(chat_id)
        check = await triage(note)
        verdict = f"Triage: {check.score}/10. {check.reason}"
        if check.score < TRIAGE_MIN_SCORE and not forced:
            tip = f" {check.missing}" if check.missing else ""
            await tg.send(
                chat_id,
                f"{verdict}\n\nNot drafted: below the {TRIAGE_MIN_SCORE}/10 bar.{tip}\n\n"
                "To draft it anyway, reply to your note (or its transcript) with: draft anyway",
            )
            return

        # 2. Context (Google News): recent headlines that could serve as a hook.
        news = await search_news(check.news_query) if check.news_query else []
        if check.news_query:
            found = f"{len(news)} headlines found" if news else "nothing found, drafting without a hook"
            verdict += f'\nNews search: "{check.news_query}" ({found})'
        await tg.send(chat_id, f"{verdict}\n\nWriting the draft. This can take a minute...")

        # 3. Draft in Meera's voice.
        await tg.typing(chat_id)
        draft = await write_draft(note, news)
        await tg.send(chat_id, draft.post)

        # 4. Score the draft against the voice guide, and attach sources for any news hook.
        await tg.typing(chat_id)
        await tg.send_html(chat_id, await build_report(note, draft, news))
    except DraftError as e:
        log.warning("%s", e)
        await tg.send(chat_id, str(e))
    except Exception:
        log.exception("Unexpected error")
        await tg.send(chat_id, "Something unexpected went wrong while making the draft. Check the Vercel logs.")


def _record(update_id, sender, outcome: str) -> dict:
    event = {"time": time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime()), "update_id": update_id,
             "sender": sender, "outcome": outcome}
    _events.append(event)
    log.info("update %s from %s: %s", update_id, sender, outcome)
    return event


@app.post("/api/telegram")
async def telegram_webhook(request: Request) -> Response:
    # Only Telegram knows the secret; it sends it in this header on every delivery.
    header = request.headers.get("x-telegram-bot-api-secret-token", "")
    if not WEBHOOK_SECRET or not hmac.compare_digest(header, WEBHOOK_SECRET):
        return Response(status_code=401)

    update = await request.json()
    update_id = update.get("update_id")
    if update_id in _seen_updates:
        return JSONResponse(_record(update_id, None, "duplicate delivery, skipped"))
    _seen_updates.append(update_id)

    message = update.get("message")
    sender = (message or {}).get("from", {}).get("id")
    if not message:
        return JSONResponse(_record(update_id, sender, "not a message, ignored"))
    if ALLOWED_USER_ID is None:
        return JSONResponse(_record(update_id, sender, "ignored: ALLOWED_TELEGRAM_USER_ID is not set"))
    if sender != ALLOWED_USER_ID:
        # Anyone other than the owner is silently ignored.
        return JSONResponse(_record(update_id, sender, f"ignored: sender is not ALLOWED_TELEGRAM_USER_ID ({ALLOWED_USER_ID})"))

    # Always answer 200, even after a failure, so Telegram doesn't redeliver the same note.
    async with httpx.AsyncClient(timeout=60) as http:
        try:
            await handle_message(Telegram(http), message)
            outcome = "handled"
        except Exception as e:
            log.exception("Could not reply in Telegram")
            outcome = f"failed: {type(e).__name__}: {e}"
    return JSONResponse(_record(update_id, sender, outcome))


@app.get("/")
async def health() -> dict:
    return {
        "status": "ok",
        "telegram_token_set": bool(TELEGRAM_BOT_TOKEN),
        "gemini_key_set": bool(os.environ.get("GEMINI_API_KEY")),
        "webhook_secret_set": bool(WEBHOOK_SECRET),
        "allowed_user_set": ALLOWED_USER_ID is not None,
    }


@app.get("/api/debug")
async def debug(request: Request) -> Response:
    """What this deployment is actually configured with, and what happened to recent messages.
    Protected by the webhook secret: send it in the X-Debug-Secret header."""
    header = request.headers.get("x-debug-secret", "")
    if not WEBHOOK_SECRET or not hmac.compare_digest(header, WEBHOOK_SECRET):
        return Response(status_code=401)
    async with httpx.AsyncClient(timeout=15) as http:
        try:
            me = await Telegram(http).call("getMe")
            bot = f"@{me.get('username')} (id {me.get('id')})"
        except Exception as e:
            bot = f"token not working: {e}"
    return JSONResponse({
        "bot": bot,
        "allowed_user_id": ALLOWED_USER_ID,
        "allowed_user_id_raw_length": len(os.environ.get("ALLOWED_TELEGRAM_USER_ID", "")),
        "recent_updates_on_this_instance": list(_events),
    })
