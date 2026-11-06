from sqlalchemy import select, update

from app.db import SessionLocal
from app.models import KbArticle
from app.seed_kb import load_articles, seed


async def test_articles_are_well_formed():
    articles = load_articles()
    assert len(articles) == 10
    assert all(a["title"] and len(a["body"].split()) >= 100 for a in articles)


async def test_seed_is_idempotent_and_restores_content():
    async with SessionLocal() as session:
        await session.execute(
            update(KbArticle).where(KbArticle.slug == "export-pdf").values(title="stale")
        )
        await session.commit()
        await seed(session)
        await seed(session)
        rows = (await session.scalars(select(KbArticle).order_by(KbArticle.slug))).all()
    assert [(r.slug, r.title, r.body) for r in rows] == [
        (a["slug"], a["title"], a["body"]) for a in load_articles()
    ]
