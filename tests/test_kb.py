import pytest

from app.config import settings
from app.kb import MIN_SCORE, article_url, search


async def test_example_ticket_finds_pdf_export_first():
    results = await search("Не можу експортувати звіт у PDF")
    assert results[0]["slug"] == "export-pdf"
    assert results[0]["url"] == article_url("export-pdf")
    assert results[0]["title"] and results[0]["body"]


async def test_password_ticket_finds_password_article_first():
    results = await search("Забув пароль і не можу увійти в акаунт")
    assert results[0]["slug"] == "password-reset"


@pytest.mark.parametrize(
    "message", ["Яка сьогодні погода?", "Привіт, як справи?", "Хочу замовити піцу з доставкою"]
)
async def test_off_topic_finds_nothing(message):
    assert await search(message) == []


async def test_results_are_capped_sorted_and_above_threshold():
    results = await search("Як додати колегу до звіту?")
    scores = [r["score"] for r in results]
    assert 1 <= len(results) <= 3
    assert scores == sorted(scores, reverse=True)
    assert all(s >= MIN_SCORE for s in scores)


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
