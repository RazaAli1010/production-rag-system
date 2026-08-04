"""Transactional email, currently one message: the password-reset link.

Resend's HTTP API over the `httpx.AsyncClient` already in the stack — no SMTP dependency and no
blocking socket on the event loop. With `RESEND_API_KEY` unset (local dev, CI) the link is logged
instead of sent, so the whole flow is exercisable without an account.
"""

import httpx
import structlog

logger = structlog.get_logger(__name__)

_ENDPOINT = "https://api.resend.com/emails"
_TIMEOUT = 10.0


def _body(link: str, ttl_min: int) -> str:
    return (
        f"<p>Someone asked to reset the password for this CampusRAG account.</p>"
        f'<p><a href="{link}">Choose a new password</a></p>'
        f"<p>The link stops working in {ttl_min} minutes, and as soon as it is used once. "
        f"If this wasn't you, ignore this email — nothing has changed.</p>"
    )


async def send_reset_link(to: str, link: str, *, settings) -> None:
    """Best-effort: a provider outage must not turn into a 500 that tells the caller the address
    exists. The caller answers 202 either way; the failure lands in the log."""
    if settings.RESEND_API_KEY is None:
        logger.info("auth.reset_link_not_sent", reason="no_api_key", link=link)
        return

    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            r = await client.post(
                _ENDPOINT,
                headers={"Authorization": f"Bearer {settings.RESEND_API_KEY.get_secret_value()}"},
                json={
                    "from": settings.RESET_FROM_EMAIL,
                    "to": [to],
                    "subject": "Reset your CampusRAG password",
                    "html": _body(link, settings.RESET_TOKEN_TTL_MIN),
                },
            )
            r.raise_for_status()
    except httpx.HTTPError as exc:
        # The link itself is never logged on this path — a delivery failure is not a reason to
        # leave account-takeover authority in the log aggregator.
        logger.warning("auth.reset_email_failed", error=str(exc))
        return
    logger.info("auth.reset_email_sent")
