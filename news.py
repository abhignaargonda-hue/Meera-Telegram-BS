"""Google News search for a timely hook, via the public Google News RSS feed (India edition)."""

import logging
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
from typing import List

import httpx

log = logging.getLogger("meera-bot")

FEED = "https://news.google.com/rss/search"
MAX_ITEMS = 8


@dataclass
class NewsItem:
    id: int
    title: str
    publisher: str
    date: str
    link: str

    def for_prompt(self) -> str:
        return f"[{self.id}] {self.title} ({self.publisher}, {self.date})"


def _parse(xml: str) -> List[NewsItem]:
    items = []
    for i, node in enumerate(ET.fromstring(xml).iter("item"), start=1):
        title = (node.findtext("title") or "").strip()
        publisher = (node.findtext("source") or "").strip()
        # Google appends " - Publisher" to every headline.
        if publisher and title.endswith(f" - {publisher}"):
            title = title[: -len(publisher) - 3]
        try:
            date = parsedate_to_datetime(node.findtext("pubDate") or "").strftime("%d %b %Y")
        except (TypeError, ValueError):
            date = "date unknown"
        items.append(NewsItem(i, title, publisher or "unknown publisher", date, (node.findtext("link") or "").strip()))
        if len(items) == MAX_ITEMS:
            break
    return items


async def search_news(query: str) -> List[NewsItem]:
    """Recent (30-day) results first; widen to any date if nothing recent turns up.
    Returns [] on any failure so drafting can carry on without a hook."""
    if not query.strip():
        return []
    try:
        async with httpx.AsyncClient(timeout=15, follow_redirects=True) as http:
            for q in (f"{query} when:30d", query):
                r = await http.get(FEED, params={"q": q, "hl": "en-IN", "gl": "IN", "ceid": "IN:en"})
                r.raise_for_status()
                items = _parse(r.text)
                if items:
                    return items
    except Exception:
        log.exception("Google News search failed")
    return []
