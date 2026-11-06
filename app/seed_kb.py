"""Upsert kb/*.md into kb_articles. Usage: python -m app.seed_kb"""

import asyncio
from pathlib import Path

from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import SessionLocal, engine
from app.models import KbArticle

KB_DIR = Path(__file__).resolve().parents[1] / "kb"


def load_articles() -> list[dict[str, str]]:
    rows = []
    for path in sorted(KB_DIR.glob("*.md")):
        first, _, body = path.read_text(encoding="utf-8").partition("\n")
        if not first.startswith("# "):
            raise ValueError(f"{path.name}: the first line must be '# Title'")
        rows.append({"slug": path.stem, "title": first[2:].strip(), "body": body.strip()})
    return rows


async def seed(session: AsyncSession) -> int:
    rows = load_articles()
    stmt = insert(KbArticle).values(rows)
    await session.execute(
        stmt.on_conflict_do_update(
            index_elements=["slug"],
            set_={"title": stmt.excluded.title, "body": stmt.excluded.body},
        )
    )
    await session.commit()
    return len(rows)


async def main() -> None:
    async with SessionLocal() as session:
        count = await seed(session)
    await engine.dispose()
    print(f"Seeded {count} KB articles from {KB_DIR}")


if __name__ == "__main__":
    asyncio.run(main())
