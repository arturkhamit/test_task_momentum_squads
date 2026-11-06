"""Fire webhook deliveries at the service, including concurrent duplicates.

    uv run python scripts/fire.py --n 50 --rate 5 --dup 3 --base-url http://localhost:8000

Each ticket gets a unique event_id; --dup sends that many concurrent copies of it, so the
service must still create one ticket per event. Prints status counts and latency percentiles.
"""

import argparse
import asyncio
import random
import time
import uuid
from collections import Counter

import httpx

MESSAGES = [
    "Не можу експортувати звіт у PDF",
    "Добрий день! Звіт у PDF вивантажується порожнім, без графіків. Що робити?",
    "Забув пароль і не можу увійти в акаунт",
    "Акаунт заблокувався після кількох спроб входу, допоможіть",
    "Скільки коштує тариф для команди?",
    "Потрібен рахунок на оплату для юридичної особи",
    "Як поділитися звітом з клієнтом, у якого немає акаунта?",
    "Не приходить щотижнева розсилка звіту на пошту",
    "Не підключається база даних PostgreSQL, пише тайм-аут з'єднання",
    "Як дати колезі доступ тільки на перегляд?",
    "Кирилиця в CSV відображається кракозябрами",
    "Як вивантажити таблицю в Excel?",
    "Як налаштувати автооновлення дашборда на телевізорі в офісі?",
    "Де створити API токен і який ліміт запитів?",
    "Яка сьогодні погода?",
    "Хочу замовити піцу з доставкою",
]


async def deliver(client: httpx.AsyncClient, payload: dict, latencies: list, codes: Counter):
    started = time.perf_counter()
    try:
        response = await client.post("/webhooks/helpdesk", json=payload)
        codes[response.status_code] += 1
    except httpx.HTTPError as e:
        codes[type(e).__name__] += 1
    latencies.append(time.perf_counter() - started)


def percentile(sorted_values: list[float], p: float) -> float:
    return sorted_values[min(len(sorted_values) - 1, int(p / 100 * len(sorted_values)))]


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--base-url", default="http://localhost:8000")
    parser.add_argument("--n", type=int, default=10, help="number of distinct tickets")
    parser.add_argument("--rate", type=float, default=5.0, help="tickets per second")
    parser.add_argument("--dup", type=int, default=1, help="concurrent copies of each event")
    args = parser.parse_args()

    run = uuid.uuid4().hex[:8]
    latencies: list[float] = []
    codes: Counter = Counter()
    tasks = []
    started = time.perf_counter()
    async with httpx.AsyncClient(base_url=args.base_url, timeout=10) as client:
        for i in range(args.n):
            ticket = f"T-{run}-{i}"
            payload = {
                "event_id": f"evt-{run}-{i}",
                "ticket_id": ticket,
                "customer_email": f"customer{i}@example.com",
                "message": random.choice(MESSAGES),
                "reply_url": f"https://helpdesk.example.com/tickets/{ticket}/replies",
            }
            for _ in range(args.dup):
                tasks.append(asyncio.create_task(deliver(client, payload, latencies, codes)))
            await asyncio.sleep(1 / args.rate)
        await asyncio.gather(*tasks)

    values = sorted(latencies)
    print(
        f"run {run}: {args.n} tickets x {args.dup} copies in {time.perf_counter() - started:.1f} s"
    )
    print("status codes:", dict(codes))
    print(
        "latency ms: "
        + " ".join(f"p{p}={percentile(values, p) * 1000:.0f}" for p in (50, 95, 99))
        + f" max={values[-1] * 1000:.0f}"
    )


if __name__ == "__main__":
    asyncio.run(main())
