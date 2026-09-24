"""Gemini calls: transcribe, triage the note, draft the post, and score the draft."""

import os
import re
from pathlib import Path
from typing import List, Optional, Type, TypeVar

from google import genai
from google.genai import errors, types
from pydantic import BaseModel, ValidationError

from news import NewsItem

DRAFT_MODEL = os.getenv("GEMINI_DRAFT_MODEL", "gemini-pro-latest")
FAST_MODEL = os.getenv("GEMINI_FAST_MODEL", "gemini-flash-latest")
# Pro gave identical scores on repeated runs of the same draft; Flash drifted by up to 0.6.
SCORE_MODEL = os.getenv("GEMINI_SCORE_MODEL", "gemini-pro-latest")
VOICE_GUIDE = (Path(__file__).parent / "meera_voice.txt").read_text(encoding="utf-8")

T = TypeVar("T", bound=BaseModel)
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


async def _generate(step: str, model: str, contents, config: types.GenerateContentConfig):
    try:
        response = await client().aio.models.generate_content(model=model, contents=contents, config=config)
    except errors.APIError as e:
        raise _explain(step, e)
    except OSError:
        raise DraftError(f"{step} failed: could not reach Gemini.")
    if not (response.text or "").strip():
        feedback = response.prompt_feedback
        if feedback and feedback.block_reason:
            raise DraftError(f"{step} failed: Gemini blocked the request ({feedback.block_reason}).")
        reason = response.candidates[0].finish_reason if response.candidates else None
        raise DraftError(f"{step} failed: Gemini returned no text (finish reason: {reason}).")
    return response


async def _ask_json(step: str, model: str, prompt: str, schema: Type[T], system: Optional[str] = None,
                    temperature: Optional[float] = None) -> T:
    config = types.GenerateContentConfig(
        system_instruction=system,
        response_mime_type="application/json",
        response_schema=schema,
        temperature=temperature,
    )
    response = await _generate(step, model, prompt, config)
    try:
        return schema.model_validate_json(response.text)
    except ValidationError:
        raise DraftError(f"{step} failed: Gemini's answer wasn't in the expected format. Try again.")


# ---------------------------------------------------------------- transcription

async def transcribe(audio: bytes, mime_type: str) -> str:
    response = await _generate(
        "Transcription",
        FAST_MODEL,
        [
            types.Part.from_bytes(data=audio, mime_type=mime_type),
            "Transcribe this voice note word for word. Reply with the transcript only.",
        ],
        types.GenerateContentConfig(),
    )
    return response.text.strip()


# ---------------------------------------------------------------- triage

class Triage(BaseModel):
    score: int
    reason: str
    missing: str
    news_query: str


TRIAGE_PROMPT = """You are triaging a raw note from Meera Pillai, founder of the Indian skincare brand
Skinstinct, before anyone spends time drafting a LinkedIn post from it. Her posts (see the voice guide
in your instructions) need concrete material: a number, a dated scene, or a specific product/formulation
point, plus a mechanism or lesson a reader can act on.

Score how publishable the note is, 0-10, using these anchors exactly:
0-2  No usable idea: greeting, logistics, off-topic, or unintelligible.
3-4  A topic, but nothing concrete: no fact, number, scene or specific claim to build on.
5-6  One concrete element (a number, scene, or specific product point) and a clear point.
7-8  Concrete material plus a mechanism or lesson, with a Skinstinct angle.
9-10 All of the above plus an honest cost, limit or mistake, and an obvious action for the reader.

Return:
- score: the integer score.
- reason: one sentence explaining the score.
- missing: one sentence on what would raise the score (empty string if 9 or 10).
- news_query: a 2-5 word Google News search query for a recent Indian or global skincare-industry
  story that could give this post a timely hook (for example: "sunscreen SPF labelling India").
  Use an empty string if no news angle would genuinely fit.

<note>
{note}
</note>"""


async def triage(note: str) -> Triage:
    result = await _ask_json("Triage", FAST_MODEL, TRIAGE_PROMPT.format(note=note), Triage,
                             system=VOICE_GUIDE, temperature=0)
    result.score = max(0, min(10, result.score))
    return result


# ---------------------------------------------------------------- drafting

class Draft(BaseModel):
    post: str
    news_hook_used: bool
    source_ids: List[int]


DRAFT_PROMPT = """Below is a note from Meera, either typed or transcribed from a voice note.
Write one LinkedIn post in her voice that follows the voice guide in your instructions, including the
hard rules on [VERIFY] and the checklist in section 6.

The note is the only source of facts about Skinstinct. Do not add any Skinstinct event, test, decision,
result, motive, timeline or customer detail that the note does not state, even a plausible one: if the
post needs such a detail to follow the structure, write a short placeholder like
[VERIFY: what testing led to pH 4.2?] instead of inventing it. Any number or outside fact not in the note
must carry [VERIFY]. General science explanation is fine; claims about what Skinstinct did or saw are
not, unless they are in the note.

{news_block}

Return:
- post: the post text only, paragraphs separated by a blank line. No title, preamble or notes.
- news_hook_used: true only if the post refers to one of the news items.
- source_ids: if news_hook_used, the ids of the TWO items most relevant to the hook (the one the post
  relies on first, then the best corroborating one). Otherwise an empty list.

<note>
{note}
</note>"""

NEWS_BLOCK = """Recent Google News headlines that might give the post a timely hook:
{items}

Use one only if it genuinely strengthens the post; otherwise ignore them all. If you use one, name it
the way the voice guide requires (publisher, date, what it reports) and mark the reference [VERIFY].
You only have the headline, so do not state anything about the story beyond what the headline says."""


async def write_draft(note: str, news: List[NewsItem]) -> Draft:
    if news:
        news_block = NEWS_BLOCK.format(items="\n".join(item.for_prompt() for item in news))
    else:
        news_block = "No news items are available; do not use a news hook."
    draft = await _ask_json("Drafting", DRAFT_MODEL, DRAFT_PROMPT.format(note=note, news_block=news_block),
                            Draft, system=VOICE_GUIDE)
    draft.post = draft.post.strip()
    valid = {item.id for item in news}
    draft.source_ids = [i for i in dict.fromkeys(draft.source_ids) if i in valid][:2]
    if not draft.source_ids:
        draft.news_hook_used = False
    return draft


# ---------------------------------------------------------------- scoring

class Criterion(BaseModel):
    score: int
    note: str


class Scorecard(BaseModel):
    # Listed before the scores so the model finds problems first, then scores against them.
    unsupported_claims: List[str]
    opening: Criterion
    format: Criterion
    claim_fencing: Criterion
    evidence: Criterion
    skinstinct_honesty: Criterion
    voice_and_language: Criterion
    closing: Criterion


# Fixed order and labels, so every scorecard reads the same way.
CRITERIA = [
    ("opening", "Opening"),
    ("format", "Format"),
    ("claim_fencing", "Claim fencing"),
    ("evidence", "Evidence and accuracy"),
    ("skinstinct_honesty", "Skinstinct honesty"),
    ("voice_and_language", "Voice and language"),
    ("closing", "Closing"),
]

SCORE_PROMPT = """Score this LinkedIn draft against Meera Pillai's voice guide (in your instructions).

First, fill unsupported_claims: compare the draft with the original note sentence by sentence and list
every claim about Skinstinct or Meera (an event, test, decision, result, plan, motive, cost, timeline or
customer detail) that the note does not state and that is NOT marked [VERIFY]. Quote the words from the
draft, at most 15 words each. General science explanation does not count. Empty list if none.

Then score each of the 7 criteria from 0 to 10 using the definitions below, with a note of at most 15
words naming the specific reason. Be strict and consistent. Start each criterion at 10 and deduct for
every concrete problem you can point to; a solid first draft usually lands at 6-8 overall, and 10 means
there is genuinely nothing to fix. Use the measured facts as given; do not recount.

1. opening: first sentence is concrete (a number, a dated scene, or the reader's own product), never a
   question or hook line; stakes stated plainly in the next sentence or two.
2. format: 7-8 prose paragraphs, roughly 450-600 words; no bullets, headings, bold, emojis, hashtags or
   exclamation marks; no greeting or sign-off.
3. claim_fencing: at least one explicit "I'm not saying X. I'm saying Y." style move that limits the claim.
4. evidence: every technical claim has a mechanism and a specific number or threshold; evidence
   strength is stated; any number or fact not in the original note is marked [VERIFY]; nothing is
   invented about Skinstinct. Deduct 2 points for each item in unsupported_claims.
5. skinstinct_honesty: Skinstinct appears with a cost, limit or mistake, not as a pitch; no competitor
   is named; blame falls on systems, not people. Deduct 1 point for each item in unsupported_claims.
6. voice_and_language: British spelling; terms like "clean", "natural", "clinically tested" only in
   quotes and examined; no wellness or hype language; no fear or triumph; nothing that could sit in a
   generic skincare ad.
7. closing: ends on what the reader can ask for and how (in writing), or a plain statement of what
   Skinstinct does; no question to the audience, call to buy or follow prompt.

Measured facts about the draft:
{facts}

<original_note>
{note}
</original_note>

<draft>
{post}
</draft>"""


HASHTAG = re.compile(r"(?<![&\w])#\w")
LIST_LINE = re.compile(r"(?m)^\s*([-*•]|\d+[.)])\s")


def _measure(post: str) -> str:
    paragraphs = [p for p in re.split(r"\n\s*\n", post) if p.strip()]
    last = paragraphs[-1].strip() if paragraphs else ""
    return "\n".join([
        f"- Words: {len(post.split())}",
        f"- Paragraphs: {len(paragraphs)}",
        f"- Exclamation marks: {post.count('!')}",
        f"- Hashtags: {len(HASHTAG.findall(post))}",
        f"- Bullet or numbered lines: {len(LIST_LINE.findall(post))}",
        f"- [VERIFY] markers: {post.count('[VERIFY')}",
        f"- Ends with a question: {'yes' if last.endswith('?') else 'no'}",
    ])


async def score_draft(note: str, post: str) -> Scorecard:
    card = await _ask_json(
        "Scoring",
        SCORE_MODEL,
        SCORE_PROMPT.format(facts=_measure(post), note=note, post=post),
        Scorecard,
        system=VOICE_GUIDE,
        temperature=0,
    )
    for key, _ in CRITERIA:
        criterion = getattr(card, key)
        criterion.score = max(0, min(10, criterion.score))
    return card


def overall(card: Scorecard) -> float:
    return round(sum(getattr(card, key).score for key, _ in CRITERIA) / len(CRITERIA), 1)
