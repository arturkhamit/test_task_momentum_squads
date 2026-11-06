"""Stress and failure scenarios against a running stack (S1-S6 of SPEC.md section 5).

Not a pytest module: run with the project environment, point it at a LOCAL/QA stack only.

    uv run python tests/load/scenarios.py fire --n 500 --duration 180      # S1 load
    uv run python tests/load/scenarios.py wait --prefix <printed prefix> --expect 500
    uv run python tests/load/scenarios.py dup --events 5 --copies 20       # S2
    uv run python tests/load/scenarios.py approve --prefix <p> --count 100 # S3 (copies=1)
    uv run python tests/load/scenarios.py approve --prefix <p> --count 5 --copies 10  # S4
    uv run python tests/load/scenarios.py wait --prefix <p> --status sent --expect 100
    uv run python tests/load/scenarios.py replies --prefix <p>             # mock: 1 per ticket
    docker compose -p msq-qa logs --no-log-prefix worker | \
        uv run python tests/load/scenarios.py llm-rate --prefix <p>        # S6

Environment: BASE_URL (http://localhost:8000), MOCK_URL (http://localhost:8001),
DB_DSN (postgresql://app:app@127.0.0.1:55432/app), UI_USER / UI_PASSWORD (admin / admin).
Every ticket made here has event_id "<prefix>-<n>", so scenarios can be counted separately.
Exit code 1 when a criterion fails.
"""

import argparse
import asyncio
import json
import os
import random
import sys
import time
import uuid
from collections import Counter
from datetime import datetime

import asyncpg
import httpx

BASE_URL = os.environ.get("BASE_URL", "http://localhost:8000")
MOCK_URL = os.environ.get("MOCK_URL", "http://localhost:8001")
DB_DSN = os.environ.get("DB_DSN", "postgresql://app:app@127.0.0.1:55432/app")
AUTH = (os.environ.get("UI_USER", "admin"), os.environ.get("UI_PASSWORD", "admin"))

MESSAGES = [
    "Не можу експортувати звіт у PDF",
    "Забув пароль і не можу увійти в акаунт",
    "Скільки коштує тариф для команди?",
    "Як поділитися звітом з клієнтом, у якого немає акаунта?",
    "Не приходить щотижнева розсилка звіту на пошту",
    "Як вивантажити таблицю в Excel?",
    "Де створити API токен і який ліміт запитів?",
    "Яка сьогодні погода?",
]

failed = False


def verdict(name: str, ok: bool, actual: str) -> None:
    global failed
    failed |= not ok
    print(f"{'PASS' if ok else 'FAIL'}  {name}: {actual}")


def payload(event_id: str, i: int = 0) -> dict:
    return {
        "event_id": event_id,
        "ticket_id": f"T-{event_id}",
        "customer_email": f"load{i}@example.com",
        "message": random.choice(MESSAGES),
        "reply_url": f"https://helpdesk.example.com/tickets/T-{event_id}/replies",
    }


def pct(values: list[float], p: float) -> float:
    values = sorted(values)
    return values[min(len(values) - 1, int(p / 100 * len(values)))]


async def db() -> asyncpg.Connection:
    return await asyncpg.connect(DB_DSN)


async def status_counts(conn, prefix: str) -> Counter:
    rows = await conn.fetch(
        "SELECT status, count(*) n FROM tickets WHERE event_id LIKE $1 GROUP BY 1", prefix + "-%"
    )
    return Counter({r["status"]: r["n"] for r in rows})


async def cmd_fire(a) -> None:
    """S1: --n webhooks spread evenly over --duration seconds."""
    prefix = a.prefix or f"load-{uuid.uuid4().hex[:6]}"
    lat: list[float] = []
    codes: Counter = Counter()

    async def one(client, i):
        t = time.perf_counter()
        try:
            r = await client.post("/webhooks/helpdesk", json=payload(f"{prefix}-{i}", i))
            codes[r.status_code] += 1
        except httpx.HTTPError as e:
            codes[type(e).__name__] += 1
        lat.append(time.perf_counter() - t)

    started = time.perf_counter()
    async with httpx.AsyncClient(
        base_url=BASE_URL, timeout=10, limits=httpx.Limits(max_connections=200)
    ) as client:
        tasks = []
        for i in range(a.n):
            await asyncio.sleep(max(0, started + i * a.duration / a.n - time.perf_counter()))
            tasks.append(asyncio.create_task(one(client, i)))
        await asyncio.gather(*tasks)
    print(f"prefix={prefix}")
    print(f"sent {a.n} in {time.perf_counter() - started:.1f}s ({a.n / a.duration:.2f}/s)")
    print("codes:", dict(codes))
    print("latency ms: " + " ".join(f"p{p}={pct(lat, p) * 1000:.0f}" for p in (50, 95, 99)), end="")
    print(f" max={max(lat) * 1000:.0f}")
    conn = await db()
    rows = await conn.fetchval("SELECT count(*) FROM tickets WHERE event_id LIKE $1", prefix + "-%")
    await conn.close()
    verdict("non-200 responses", codes == {200: a.n}, str(dict(codes)))
    verdict("webhook p99 < 2000 ms", pct(lat, 99) < 2, f"{pct(lat, 99) * 1000:.0f} ms")
    verdict(f"rows == {a.n}", rows == a.n, str(rows))


async def cmd_wait(a) -> None:
    """Poll until `expect` tickets with the prefix are in --status; report the drain time."""
    conn = await db()
    started = time.monotonic()
    max_conns = 0
    while True:
        counts = await status_counts(conn, a.prefix)
        conns = await conn.fetchval(
            "SELECT count(*) FROM pg_stat_activity WHERE datname=current_database()"
        )
        max_conns = max(max_conns, conns)
        elapsed = time.monotonic() - started
        print(f"[{elapsed:6.0f}s] {dict(counts)} db_conns={conns}", flush=True)
        if counts[a.status] >= a.expect or elapsed > a.timeout:
            break
        await asyncio.sleep(a.interval)
    stuck = await conn.fetchval(
        "SELECT count(*) FROM tickets WHERE event_id LIKE $1"
        " AND status IN ('generating','sending')",
        a.prefix + "-%",
    )
    total = sum(counts.values())
    await conn.close()
    print(f"drain time {elapsed:.0f}s (waiting started when this command started)")
    verdict(f"rows == {a.expect}", total == a.expect, str(total))
    verdict(f"all {a.expect} reached {a.status}", counts[a.status] == a.expect, str(dict(counts)))
    verdict("nothing stuck in generating/sending", stuck == 0, str(stuck))
    print(f"max DB connections seen: {max_conns}")


async def cmd_dup(a) -> None:
    """S2: --copies concurrent posts of each of --events event_ids; expect one row each."""
    prefix = a.prefix or f"dup-{uuid.uuid4().hex[:6]}"
    codes: Counter = Counter()
    async with httpx.AsyncClient(
        base_url=BASE_URL, timeout=10, limits=httpx.Limits(max_connections=200)
    ) as client:

        async def post(body):
            try:
                codes[(await client.post("/webhooks/helpdesk", json=body)).status_code] += 1
            except httpx.HTTPError as e:
                codes[type(e).__name__] += 1

        bodies = [payload(f"{prefix}-{i}", i) for i in range(a.events)]
        await asyncio.gather(*(post(b) for b in bodies for _ in range(a.copies)))
    conn = await db()
    per_event = await conn.fetch(
        "SELECT event_id, count(*) n FROM tickets WHERE event_id LIKE $1 GROUP BY 1", prefix + "-%"
    )
    await conn.close()
    sent = a.events * a.copies
    verdict(f"all {sent} posts answered 200", codes == {200: sent}, str(dict(codes)))
    verdict(
        f"{a.events} event_ids -> exactly {a.events} rows, 1 each",
        len(per_event) == a.events and all(r["n"] == 1 for r in per_event),
        f"{len(per_event)} event_ids, max rows per id "
        f"{max((r['n'] for r in per_event), default=0)}",
    )
    print(f"prefix={prefix}")


async def cmd_approve(a) -> None:
    """S3/S4: approve up to --count draft tickets of the prefix, each --copies times at once."""
    conn = await db()
    ids = [
        str(r["id"])
        for r in await conn.fetch(
            "SELECT id FROM tickets WHERE event_id LIKE $1 AND status='draft'"
            " ORDER BY created_at LIMIT $2",
            a.prefix + "-%",
            a.count,
        )
    ]
    await conn.close()
    print(f"approving {len(ids)} drafts x {a.copies} copies")
    sem = asyncio.Semaphore(a.parallel)
    outcomes: Counter = Counter()
    headers = {"Origin": BASE_URL}
    async with httpx.AsyncClient(base_url=BASE_URL, auth=AUTH, headers=headers, timeout=20) as c:

        async def approve(tid):
            async with sem:
                r = await c.post(
                    f"/ui/tickets/{tid}/approve", data={"final_message": f"QA reply {tid}"}
                )
                outcomes[r.headers.get("location", str(r.status_code)).split("notice=")[-1]] += 1

        await asyncio.gather(*(approve(t) for t in ids for _ in range(a.copies)))
    print("outcomes (notice):", dict(outcomes))
    n = len(ids)
    verdict(
        "exactly one approve per ticket changed state",
        outcomes["approved"] == n and outcomes["stale"] == n * (a.copies - 1),
        str(dict(outcomes)),
    )


async def cmd_replies(a) -> None:
    """Compare tickets (DB) with the mock helpdesk's stored replies: one record per ticket."""
    conn = await db()
    rows = await conn.fetch(
        "SELECT id, status, attempts FROM tickets WHERE event_id LIKE $1", a.prefix + "-%"
    )
    await conn.close()
    ids = {str(r["id"]) for r in rows}
    sent = {str(r["id"]) for r in rows if r["status"] == "sent"}
    async with httpx.AsyncClient(timeout=30) as c:
        records = (await c.get(f"{MOCK_URL}/replies")).json()["replies"]
    mine = [r for r in records if r["idempotency_key"] in ids]
    per_key = Counter(r["idempotency_key"] for r in mine)
    dups = {k: n for k, n in per_key.items() if n > 1}
    absorbed = sum(r["attempts"] - 1 for r in mine)
    print(
        f"tickets={len(ids)} sent={len(sent)} mock records={len(mine)} retries absorbed={absorbed}"
    )
    print(
        f"ticket attempts histogram: {dict(sorted(Counter(r['attempts'] for r in rows).items()))}"
    )
    verdict("zero duplicate records in the mock", not dups, f"{len(dups)} duplicated keys")
    verdict(
        "mock has a record for exactly the sent tickets",
        set(per_key) == sent,
        f"{len(set(per_key) ^ sent)} differ",
    )


async def cmd_llm_rate(a) -> None:
    """S6: read worker JSON logs on stdin; max llm_request starts in any sliding 60 s window,
    and llm_request lines per ticket vs tickets.attempts."""
    stamps: list[float] = []
    per_ticket: Counter = Counter()
    for line in sys.stdin:
        try:
            d = json.loads(line)
        except ValueError:
            continue
        if d.get("event") == "llm_request":
            stamps.append(datetime.fromisoformat(d["ts"]).timestamp())
            per_ticket[d["ticket_id"]] += 1
    stamps.sort()
    best, j = 0, 0
    for i, t in enumerate(stamps):
        while t - stamps[j] >= 60:
            j += 1
        best = max(best, i - j + 1)
    gaps = [b - a_ for a_, b in zip(stamps, stamps[1:], strict=False)]
    print(f"llm_request lines: {len(stamps)} over {stamps[-1] - stamps[0]:.0f}s")
    print(f"min gap between starts (log ts has 1 s resolution): {min(gaps):.0f}s")
    verdict("max llm_request in any sliding 60 s window <= 60", best <= 60, str(best))
    conn = await db()
    rows = await conn.fetch(
        "SELECT id, attempts FROM tickets WHERE event_id LIKE $1", a.prefix + "-%"
    )
    await conn.close()
    bad = [
        (str(r["id"]), r["attempts"], per_ticket[str(r["id"])])
        for r in rows
        if r["attempts"] != per_ticket[str(r["id"])]
    ]
    verdict(
        "per ticket: llm_request lines == attempts",
        not bad,
        f"{len(bad)} of {len(rows)} differ" + (f", e.g. {bad[:3]}" if bad else ""),
    )


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("fire")
    s.add_argument("--n", type=int, default=500)
    s.add_argument("--duration", type=float, default=180)
    s.add_argument("--prefix")
    s = sub.add_parser("wait")
    s.add_argument("--prefix", required=True)
    s.add_argument("--expect", type=int, required=True)
    s.add_argument("--status", default="draft")
    s.add_argument("--timeout", type=float, default=1800)
    s.add_argument("--interval", type=float, default=15)
    s = sub.add_parser("dup")
    s.add_argument("--events", type=int, default=5)
    s.add_argument("--copies", type=int, default=20)
    s.add_argument("--prefix")
    s = sub.add_parser("approve")
    s.add_argument("--prefix", required=True)
    s.add_argument("--count", type=int, default=100)
    s.add_argument("--copies", type=int, default=1)
    s.add_argument("--parallel", type=int, default=20)
    s = sub.add_parser("replies")
    s.add_argument("--prefix", required=True)
    s = sub.add_parser("llm-rate")
    s.add_argument("--prefix", required=True)
    a = p.parse_args()
    asyncio.run(
        {
            "fire": cmd_fire,
            "wait": cmd_wait,
            "dup": cmd_dup,
            "approve": cmd_approve,
            "replies": cmd_replies,
            "llm-rate": cmd_llm_rate,
        }[a.cmd](a)
    )
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
