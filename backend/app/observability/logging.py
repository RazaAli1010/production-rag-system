"""The one `structlog.configure` call (F13, AC-7).

Every feature already emits `logger.info("...", ...)`; nobody configured the renderer. F13 does it
once at boot so those lines become JSON carrying F11's `request_id` (via `merge_contextvars`) and the
`APP_ENV` tag. `LOG_JSON=false` swaps in the console renderer for readable local dev.
"""

import logging

import structlog


def configure_logging(settings) -> None:
    """Idempotent — safe to call again under test re-config."""
    renderer = (
        structlog.processors.JSONRenderer()
        if settings.LOG_JSON
        else structlog.dev.ConsoleRenderer()
    )
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,  # pulls in F11's request_id
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            renderer,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(
            logging.getLevelName(settings.LOG_LEVEL)
        ),
        cache_logger_on_first_use=True,
    )
    # Bind the env tag once so every line carries it (unbound only on interpreter exit).
    structlog.contextvars.bind_contextvars(env=settings.APP_ENV)
