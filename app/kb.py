import html
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import HTMLResponse
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db import SessionLocal, get_session
from app.models import KbArticle

router = APIRouter()

# Tuned in tests/test_kb.py: off-topic text scores <= 0.25, on-topic tickets 0.35-0.75.
MIN_SCORE = 0.3

# `<<%` is strict_word_similarity(q, doc) >= the threshold and can use kb_articles_trgm_idx;
# the expression must match the index: title || ' ' || body.
SEARCH_SQL = text("""
    SELECT slug, title, body, strict_word_similarity(:q, title || ' ' || body) AS score
    FROM kb_articles
    WHERE :q <<% (title || ' ' || body)
    ORDER BY score DESC, slug
    LIMIT :limit
""")


def article_url(slug: str) -> str:
    return f"{settings.public_base_url.rstrip('/')}/kb/{slug}"


async def search(message: str, limit: int = 3) -> list[dict]:
    """Top KB articles for a ticket message: [{slug, title, body, url, score}], best first."""
    async with SessionLocal() as session, session.begin():
        await session.execute(
            text("SELECT set_config('pg_trgm.strict_word_similarity_threshold', :t, true)"),
            {"t": str(MIN_SCORE)},
        )
        rows = (await session.execute(SEARCH_SQL, {"q": message, "limit": limit})).mappings()
        return [dict(row, url=article_url(row["slug"])) for row in rows]


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
