"""Posting approved replies to the helpdesk. Queue mechanics live in app.worker."""

import httpx

from app.config import settings

# Module-level so connections are reused; tests swap it for one with a short timeout.
# Redirects are not followed: a redirect could lead outside the allowlist.
http = httpx.AsyncClient(timeout=settings.send_timeout)


class SendError(Exception):
    def __init__(self, message: str, *, retryable: bool):
        super().__init__(message)
        self.retryable = retryable


def target_url(reply_url: str) -> str:
    """Validate the untrusted reply_url (SSRF) and apply HELPDESK_MOCK_URL. Raises ValueError.

    Parsed with httpx itself, so what we check is exactly what we send to."""
    try:
        url = httpx.URL(reply_url)
    except httpx.InvalidURL as e:
        raise ValueError(f"reply_url is not a valid URL: {e}") from e
    if url.scheme not in ("http", "https"):
        raise ValueError(f"reply_url scheme {url.scheme!r} is not http or https")
    if url.userinfo:
        raise ValueError("reply_url must not contain credentials")
    allowed = {h.strip().lower() for h in settings.helpdesk_allowed_hosts.split(",") if h.strip()}
    if url.host not in allowed:
        raise ValueError(f"reply_url host {url.host!r} is not in HELPDESK_ALLOWED_HOSTS")
    if settings.helpdesk_mock_url:
        mock = httpx.URL(settings.helpdesk_mock_url)
        url = url.copy_with(scheme=mock.scheme, host=mock.host, port=mock.port)
    return str(url)


async def post_reply(url: str, idempotency_key: str, ticket_id: str, message: str) -> None:
    """POST one reply. Returns on 2xx, raises SendError otherwise."""
    try:
        response = await http.post(
            url,
            json={"ticket_id": ticket_id, "message": message},
            headers={"Idempotency-Key": idempotency_key},
        )
    except httpx.TimeoutException as e:
        raise SendError(f"timeout: {type(e).__name__}", retryable=True) from e
    except httpx.TransportError as e:
        raise SendError(f"connection error: {e!r}", retryable=True) from e
    except httpx.HTTPError as e:
        # Anything else httpx raises (e.g. DecodingError on a broken body): the reply may or
        # may not be stored, so retry; the Idempotency-Key makes that safe.
        raise SendError(f"request failed: {e!r}", retryable=True) from e
    if response.is_success:
        return
    status = response.status_code
    raise SendError(f"HTTP {status}", retryable=status in (408, 429) or status >= 500)
