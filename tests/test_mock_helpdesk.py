import asyncio

REPLY = {"ticket_id": "T-1", "message": "Ось відповідь"}
URL = "/tickets/T-1/replies"


async def replies(mock_client):
    return (await mock_client.get("/replies")).json()["replies"]


async def test_stores_reply(mock_client):
    r = await mock_client.post(URL, json=REPLY, headers={"Idempotency-Key": "k1"})
    assert r.status_code == 201
    assert r.json()["message"] == "Ось відповідь" and r.json()["attempts"] == 1
    [record] = await replies(mock_client)
    assert record["path_ticket_id"] == "T-1" and record["idempotency_key"] == "k1"


async def test_dedups_by_idempotency_key_and_counts_attempts(mock_client):
    results = await asyncio.gather(
        *(mock_client.post(URL, json=REPLY, headers={"Idempotency-Key": "k1"}) for _ in range(5))
    )
    assert [r.status_code for r in results] == [201] * 5
    [record] = await replies(mock_client)
    assert record["attempts"] == 5


async def test_reply_without_key_is_stored_but_not_deduplicated(mock_client):
    await mock_client.post(URL, json=REPLY)
    await mock_client.post(URL, json=REPLY)
    assert len(await replies(mock_client)) == 2
    page = (await mock_client.get("/")).text
    assert page.count("DUPLICATE") == 2


async def test_fail_rate_returns_503_and_stores_nothing(mock_client, mock_helpdesk):
    mock_helpdesk.settings.fail_rate = 1.0
    r = await mock_client.post(URL, json=REPLY, headers={"Idempotency-Key": "k1"})
    assert r.status_code == 503
    assert await replies(mock_client) == []
    mock_helpdesk.settings.fail_rate = 0.0
    r = await mock_client.post(URL, json=REPLY, headers={"Idempotency-Key": "k1"})
    assert r.status_code == 201
    [record] = await replies(mock_client)
    assert record["attempts"] == 2


async def test_slow_rate_stores_before_answering(mock_client, mock_helpdesk):
    mock_helpdesk.settings.slow_rate = 1.0
    mock_helpdesk.settings.slow_seconds = 0.3
    post = asyncio.create_task(mock_client.post(URL, json=REPLY, headers={"Idempotency-Key": "k1"}))
    await asyncio.sleep(0.1)
    assert len(await replies(mock_client)) == 1  # stored while the response is still pending
    assert not post.done()
    assert (await post).status_code == 201


async def test_rejects_invalid_body(mock_client):
    r = await mock_client.post(URL, json={"ticket_id": "T-1"})
    assert r.status_code == 422


async def test_index_lists_replies_without_duplicates(mock_client):
    await mock_client.post(URL, json=REPLY, headers={"Idempotency-Key": "k1"})
    await mock_client.post(URL, json=REPLY, headers={"Idempotency-Key": "k1"})
    page = (await mock_client.get("/")).text
    assert "Ось відповідь" in page and "DUPLICATE" not in page
    assert "<td>1</td></tr>" in page  # one retry absorbed
