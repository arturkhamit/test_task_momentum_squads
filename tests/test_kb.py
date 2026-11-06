import pytest

from app.config import settings
from app.kb import MIN_RATIO, MIN_TOP_SCORE, article_url, search


@pytest.mark.parametrize(
    ("message", "top"),
    [
        ("Не можу експортувати звіт у PDF", "export-pdf"),
        (
            "Добрий день! Підкажіть, будь ласка, як завантажити місячний звіт у пдф, "
            "бо кнопка не працює",
            "export-pdf",
        ),
        ("Скільки коштує тариф для команди?", "billing"),
        ("Як дати колезі доступ до дашборду, щоб він тільки переглядав?", "roles-permissions"),
        ("Забув пароль і не можу увійти в акаунт", "password-reset"),
        ("Як змінити пароль?", "password-reset"),
        ("Як вивантажити таблицю в Excel?", "export-excel-csv"),
        ("Де взяти API токен?", "api-tokens"),
        ("Не приходить щотижнева розсилка звіту на пошту", "scheduled-reports"),
        ("Не підключається база даних PostgreSQL, пише тайм-аут", "data-sources"),
        ("Скасуйте мою підписку", "billing"),
        ("Яка сьогодні погода?", None),
        ("Привіт, як справи?", None),
        ("Хочу замовити піцу з доставкою", None),
        ("У вас гарний сайт", None),
        ("Коли буде нова версія?", None),
        ("Де знаходиться ваш офіс?", None),
        ("", None),
    ],
)
async def test_search_top_article(message, top):
    results = await search(message)
    assert (results[0]["slug"] if results else None) == top


@pytest.mark.parametrize(
    ("message", "slugs"),
    [
        # Runners-up at exactly half the top score are dropped (title boost doubles the top).
        ("Як змінити пароль?", ["password-reset"]),
        ("Скільки коштує тариф для команди?", ["billing"]),
        ("Не можу експортувати звіт у PDF", ["export-pdf"]),
    ],
)
async def test_search_drops_weak_runners_up(message, slugs):
    assert [r["slug"] for r in await search(message)] == slugs


async def test_search_result_shape():
    results = await search("Не можу експортувати звіт у PDF")
    assert results[0]["url"] == article_url("export-pdf")
    assert results[0]["title"] and results[0]["body"]


async def test_results_are_capped_sorted_and_close_to_top():
    results = await search("Як дати колезі доступ до дашборду, щоб він тільки переглядав?")
    scores = [r["score"] for r in results]
    assert 1 <= len(results) <= 3
    assert scores == sorted(scores, reverse=True)
    assert scores[0] >= MIN_TOP_SCORE
    assert all(s > MIN_RATIO * scores[0] for s in scores[1:])


async def test_article_page(client):
    r = await client.get("/kb/export-pdf")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/html")
    assert "<h1>Як експортувати звіт у PDF</h1>" in r.text


async def test_unknown_article_is_404(client):
    r = await client.get("/kb/no-such-article")
    assert r.status_code == 404


async def test_article_page_escapes_html(client):
    r = await client.get("/kb/api-tokens")
    assert "Bearer &lt;токен&gt;" in r.text


def test_article_url_uses_public_base_url(monkeypatch):
    monkeypatch.setattr(settings, "public_base_url", "https://support.example.org/")
    assert article_url("export-pdf") == "https://support.example.org/kb/export-pdf"
