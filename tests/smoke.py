"""Smoke test for a running deployment; safe to run against production.

    python tests/smoke.py --base-url http://localhost:8000
    BASE_URL=https://example.com UI_USER=admin UI_PASSWORD=... python tests/smoke.py

Read-only apart from ONE clearly marked test ticket (event_id "smoke-<uuid>", message starts
with "[smoke test]"). It never touches /ui POST endpoints, so it never approves anything.
Pass --skip-webhook to avoid creating even that ticket. Exit code 0 = all checks passed.
"""

import argparse
import os
import sys
import uuid

import httpx


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--base-url", default=os.environ.get("BASE_URL", "http://localhost:8000"))
    p.add_argument("--ui-user", default=os.environ.get("UI_USER"))
    p.add_argument("--ui-password", default=os.environ.get("UI_PASSWORD"))
    p.add_argument("--skip-webhook", action="store_true", help="do not create the test ticket")
    args = p.parse_args()

    failures: list[str] = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        print(f"{'PASS' if ok else 'FAIL'}  {name}{'  ' + detail if detail and not ok else ''}")
        if not ok:
            failures.append(name)

    with httpx.Client(base_url=args.base_url.rstrip("/"), timeout=10) as c:
        r = c.get("/health")
        check("GET /health -> 200 OK", r.status_code == 200 and r.text == "OK", f"{r.status_code}")
        r = c.get("/ready")
        check("GET /ready -> 200", r.status_code == 200, f"{r.status_code} {r.text[:80]}")

        if not args.skip_webhook:
            event_id = f"smoke-{uuid.uuid4().hex}"
            body = {
                "event_id": event_id,
                "ticket_id": f"SMOKE-{event_id[6:14]}",
                "customer_email": "smoke-test@example.com",
                "message": "[smoke test] Не можу експортувати звіт у PDF",
                "reply_url": f"https://helpdesk.example.com/tickets/{event_id}/replies",
            }
            r = c.post("/webhooks/helpdesk", json=body)
            check(
                "POST /webhooks/helpdesk (new smoke- event) -> 200 accepted",
                r.status_code == 200 and r.json() == {"status": "accepted"},
                f"{r.status_code} {r.text[:80]}",
            )
            r = c.post("/webhooks/helpdesk", json=body)
            check("POST same event again -> 200 (dedup)", r.status_code == 200, f"{r.status_code}")
            r = c.post("/webhooks/helpdesk", json={"event_id": f"smoke-{uuid.uuid4().hex}"})
            check("POST invalid body -> 422", r.status_code == 422, f"{r.status_code}")

        r = c.get("/kb/export-pdf")
        check(
            "GET /kb/export-pdf -> 200 html",
            r.status_code == 200 and "<h1>" in r.text,
            f"{r.status_code}",
        )
        r = c.get("/kb/no-such-article")
        check("GET /kb/<unknown> -> 404", r.status_code == 404, f"{r.status_code}")

        r = c.get("/ui")
        check(
            "GET /ui without auth -> 401",
            r.status_code == 401 and "basic" in r.headers.get("www-authenticate", "").lower(),
            f"{r.status_code}",
        )
        r = c.get("/ui", auth=("smoke", "definitely-wrong"))
        check("GET /ui with wrong credentials -> 401", r.status_code == 401, f"{r.status_code}")
        if args.ui_user and args.ui_password:
            r = c.get("/ui", auth=(args.ui_user, args.ui_password))
            check(
                "GET /ui with operator credentials -> 200", r.status_code == 200, f"{r.status_code}"
            )
        else:
            print("SKIP  GET /ui with operator credentials (no --ui-user/--ui-password)")

    print(f"\n{'FAILED: ' + ', '.join(failures) if failures else 'all checks passed'}")
    return 1 if failures else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except httpx.HTTPError as e:  # unreachable host, timeout: a failed smoke, not a traceback
        print(f"FAIL  request error: {e!r}")
        sys.exit(1)
