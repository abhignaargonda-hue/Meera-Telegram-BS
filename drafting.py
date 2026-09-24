"""Gemini calls: transcribe a voice note, and write a LinkedIn draft in Meera's voice."""

import os
from pathlib import Path
from typing import Optional

from google import genai
from google.genai import errors, types

DRAFT_MODEL = os.getenv("GEMINI_DRAFT_MODEL", "gemini-pro-latest")
TRANSCRIBE_MODEL = os.getenv("GEMINI_TRANSCRIBE_MODEL", "gemini-flash-latest")
VOICE_GUIDE = (Path(__file__).parent / "meera_voice.txt").read_text(encoding="utf-8")

_client: Optional[genai.Client] = None


def client() -> genai.Client:
    global _client
    if _client is None:
        _client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
    return _client


class DraftError(Exception):
    """A failure with a plain-language message safe to show in Telegram."""


def _explain(step: str, e: errors.APIError) -> DraftError:
    if e.code in (401, 403) or (e.code == 400 and "API key" in str(e.message)):
        return DraftError(f"{step} failed: the Gemini API key was rejected. Check GEMINI_API_KEY in Vercel.")
    if e.code == 429:
        return DraftError(f"{step} failed: Gemini rate limit or quota reached. Try again in a minute.")
    if e.code and e.code >= 500:
        return DraftError(f"{step} failed: Gemini is having problems right now ({e.code}). Try again shortly.")
    return DraftError(f"{step} failed: Gemini returned an error ({e.code}: {e.message}).")


async def _ask(step: str, model: str, contents, system: Optional[str] = None) -> str:
    config = types.GenerateContentConfig(system_instruction=system) if system else None
    try:
        response = await client().aio.models.generate_content(model=model, contents=contents, config=config)
    except errors.APIError as e:
        raise _explain(step, e)
    except OSError:
        raise DraftError(f"{step} failed: could not reach Gemini.")

    text = (response.text or "").strip()
    if not text:
        feedback = response.prompt_feedback
        if feedback and feedback.block_reason:
            raise DraftError(f"{step} failed: Gemini blocked the request ({feedback.block_reason}).")
        reason = response.candidates[0].finish_reason if response.candidates else None
        raise DraftError(f"{step} failed: Gemini returned no text (finish reason: {reason}).")
    if response.candidates and response.candidates[0].finish_reason == types.FinishReason.MAX_TOKENS:
        text += "\n\n[Cut off at the length limit.]"
    return text


async def transcribe(audio: bytes, mime_type: str) -> str:
    return await _ask(
        "Transcription",
        TRANSCRIBE_MODEL,
        [
            types.Part.from_bytes(data=audio, mime_type=mime_type),
            "Transcribe this voice note word for word. Reply with the transcript only.",
        ],
    )


async def write_draft(note: str) -> str:
    prompt = (
        "Below is a note from Meera, either typed or transcribed from a voice note. "
        "Write one LinkedIn post in her voice that follows the voice guide in your instructions, "
        "including the hard rules on [VERIFY] and the checklist in section 6. "
        "The note is the only source of facts. Do not add any Skinstinct event, test, decision, result, "
        "motive, timeline or customer detail that the note does not state, even a plausible one: "
        "if the post needs such a detail to follow the structure, write a short placeholder like "
        "[VERIFY: what testing led to pH 4.2?] instead of inventing it. Any number or outside fact not "
        "in the note must carry [VERIFY]. General science explanation is fine; claims about what "
        "Skinstinct did or saw are not, unless they are in the note. "
        "Reply with the post text only: no title, no preamble, no notes after it.\n\n"
        f"<note>\n{note}\n</note>"
    )
    return await _ask("Drafting", DRAFT_MODEL, prompt, system=VOICE_GUIDE)
