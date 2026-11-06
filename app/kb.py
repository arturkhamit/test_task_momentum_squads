import html
import re
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import HTMLResponse
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db import SessionLocal, get_session
from app.models import KbArticle

router = APIRouter()

# Ranking: every distinct ticket word (>= 3 chars, not a stop word) that fuzzy-matches an
# article adds strict_word_similarity * IDF, so words found in few articles weigh more.
# Thresholds are tuned in tests/test_kb.py.
WORD_SIMILARITY = 0.5  # per-word match threshold for `<<%`
MIN_TOP_SCORE = 2.0  # below this the best article is a coincidence: return nothing
MIN_RATIO = 0.5  # keep runners-up only if score >= MIN_RATIO * top score

# Greetings, politeness and filler that are common in tickets but say nothing about the topic.
STOP_WORDS = frozenset(
    """
    добрий доброго день вечір привіт вітаю будь ласка дякую підкажіть допоможіть можу можна
    хочу треба потрібно працює вийшло виходить який яка яке які для але або так що щоб тому
    коли чому мене мені мій моя моє мою мої вас ваш ваша ваше буде все вже цей ця це цього
    він вона вони без при про під над
    """.split()
)

# `<<%` can use kb_articles_trgm_idx; the expression must match the index: title || ' ' || body.
SEARCH_SQL = text("""
    WITH m AS (
        SELECT a.slug, w.w, strict_word_similarity(w.w, a.title || ' ' || a.body) AS s
        FROM unnest(CAST(:words AS text[])) AS w(w), kb_articles a
        WHERE w.w <<% (a.title || ' ' || a.body)
    ),
    df AS (SELECT w, count(*) AS df FROM m GROUP BY w),
    n AS (SELECT count(*) AS n FROM kb_articles)
    SELECT a.slug, a.title, a.body, sum(m.s * ln((n.n + 1.0) / df.df))::float AS score
    FROM m JOIN df USING (w) JOIN kb_articles a USING (slug) CROSS JOIN n
    GROUP BY a.slug
    ORDER BY score DESC, a.slug
    LIMIT :limit
""")


def article_url(slug: str) -> str:
    return f"{settings.public_base_url.rstrip('/')}/kb/{slug}"


async def search(message: str, limit: int = 3) -> list[dict]:
    """Top KB articles for a ticket message: [{slug, title, body, url, score}], best first."""
    words = sorted(
        {w for w in re.findall(r"\w+", message.lower()) if len(w) >= 3 and w not in STOP_WORDS}
    )
    if not words:
        return []
    async with SessionLocal() as session, session.begin():
        await session.execute(
            text("SELECT set_config('pg_trgm.strict_word_similarity_threshold', :t, true)"),
            {"t": str(WORD_SIMILARITY)},
        )
        rows = (await session.execute(SEARCH_SQL, {"words": words, "limit": limit})).mappings()
        rows = [dict(row, url=article_url(row["slug"])) for row in rows]
    if not rows or rows[0]["score"] < MIN_TOP_SCORE:
        return []
    return [r for r in rows if r["score"] >= MIN_RATIO * rows[0]["score"]]


@router.get("/kb/{slug}", response_class=HTMLResponse)
async def show_article(slug: str, session: Annotated[AsyncSession, Depends(get_session)]) -> str:
    article = await session.scalar(select(KbArticle).where(KbArticle.slug == slug))
    if article is None:
        raise HTTPException(status_code=404, detail="Article not found")
    title = html.escape(article.title)
    paragraphs = "\n".join(
        "<p>" + "<br>".join(html.escape(line) for line in block.splitlines()) + "</p>"
        for block in article.body.split("\n\n")
    )
    return (
        '<!doctype html><html lang="uk"><head><meta charset="utf-8">'
        f"<title>{title}</title></head><body><h1>{title}</h1>\n{paragraphs}</body></html>"
    )
