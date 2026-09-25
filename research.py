"""Research step: find sourced outside facts for a note (Gemini Flash + Google Search), so every outside
fact in the draft comes with the link it was taken from."""

import asyncio
import logging
import re
from dataclasses import dataclass, field
from typing import List, Tuple

import httpx
from google.genai import errors, types

from drafting import FAST_MODEL, client

log = logging.getLogger("meera-bot")

MAX_FACTS = 6
SOURCES_PER_FACT = 2

PROMPT = """Meera Pillai, founder of the Indian skincare brand Skinstinct, will write a LinkedIn post from
the note below. Find the outside facts that post would need: the science mechanism behind the note,
specific thresholds or numbers, relevant regulations or standards (India first where relevant), and
any well-established study. Do not research Skinstinct itself or restate the note's own figures.

Search the web for each. Prefer regulators (CDSCO, BIS, FDA, EU SCCS), standards bodies (ISO),
peer-reviewed journals and PubMed, and established outlets over brand blogs and shops.

Write up to {n} lines, each exactly in this form, with nothing before or after:
FACT: <one specific fact, with its number or threshold, in at most 30 words>

<note>
{note}
</note>"""


@dataclass
class Fact:
    id: int
    text: str
    sources: List[Tuple[str, str]] = field(default_factory=list)  # (title, url)

    def for_prompt(self) -> str:
        return f"[F{self.id}] {self.text}"


async def _resolve(http: httpx.AsyncClient, url: str) -> str:
    """Search links are Google redirects; show the real page URL where we can."""
    try:
        r = await http.get(url, follow_redirects=False)
        return r.headers.get("location") or url
    except Exception:
        return url


async def research(note: str) -> List[Fact]:
    """Returns [] on any failure; the draft then carries on with no outside facts."""
    try:
        response = await client().aio.models.generate_content(
            model=FAST_MODEL,
            contents=PROMPT.format(n=MAX_FACTS, note=note),
            config=types.GenerateContentConfig(tools=[types.Tool(google_search=types.GoogleSearch())], temperature=0),
        )
    except (errors.APIError, OSError):
        log.exception("Research search failed")
        return []

    text = (response.text or "").replace("**", "")
    lines = [m.strip() for m in re.findall(r"(?m)^\s*FACT:\s*(.+)$", text)][:MAX_FACTS]
    facts = [Fact(i, line) for i, line in enumerate(lines, start=1)]

    metadata = response.candidates[0].grounding_metadata if response.candidates else None
    chunks = (metadata.grounding_chunks or []) if metadata else []
    for support in (metadata.grounding_supports or []) if metadata else []:
        segment = (support.segment.text or "").replace("**", "").replace("FACT:", "").strip() if support.segment else ""
        if not segment:
            continue
        for fact in facts:
            if segment[:50] in fact.text or fact.text[:50] in segment:
                for index in support.grounding_chunk_indices or []:
                    web = chunks[index].web if index < len(chunks) else None
                    if web and web.uri and all(web.uri != u for _, u in fact.sources):
                        fact.sources.append((web.title or "source", web.uri))
                break

    # Only keep facts we can actually link to a page.
    facts = [f for f in facts if f.sources]
    async with httpx.AsyncClient(timeout=5) as http:
        for fact in facts:
            fact.sources = fact.sources[:SOURCES_PER_FACT]
            urls = await asyncio.gather(*(_resolve(http, url) for _, url in fact.sources))
            fact.sources = [(title, url) for (title, _), url in zip(fact.sources, urls)]
    for i, fact in enumerate(facts, start=1):
        fact.id = i
    return facts
