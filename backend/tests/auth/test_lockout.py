from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import update

from app.auth import service
from app.auth.schemas import RegisterRequest
from app.core.exceptions import AuthError
from app.db.models import LoginAttempt

PW = "probation123"
EMAIL = "s@pu.edu.pk"


async def _register(session, settings):
    return await service.register(
        session, RegisterRequest(email=EMAIL, password=PW), settings=settings
    )


async def _fail(session, settings, n):
    for _ in range(n):
        with pytest.raises(AuthError):
            await service.authenticate(session, EMAIL, "wrongpassword", settings=settings)


async def test_nine_failures_still_allows_login(session, auth_settings):
    await _register(session, auth_settings)

    await _fail(session, auth_settings, 9)

    assert await service.authenticate(session, EMAIL, PW, settings=auth_settings)


async def test_ten_failures_locks_out(session, auth_settings):
    await _register(session, auth_settings)

    await _fail(session, auth_settings, 10)

    with pytest.raises(AuthError) as exc:
        await service.authenticate(session, EMAIL, PW, settings=auth_settings)
    assert exc.value.status == 429
    assert exc.value.reason == "lockout"


async def test_lockout_short_circuits_before_bcrypt(session, auth_settings, monkeypatch):
    """AC-13: the point of counting first is that a locked account costs no CPU."""
    await _register(session, auth_settings)
    await _fail(session, auth_settings, 10)

    async def _boom(*a, **kw):
        raise AssertionError("bcrypt must not run for a locked-out account")

    monkeypatch.setattr(service, "verify_password", _boom)

    with pytest.raises(AuthError) as exc:
        await service.authenticate(session, EMAIL, PW, settings=auth_settings)
    assert exc.value.status == 429


async def test_lockout_ages_out_of_the_window(session, auth_settings):
    """AC-14: a window over login_attempts, not a stored locked_until flag — so it clears with no
    operator action."""
    await _register(session, auth_settings)
    await _fail(session, auth_settings, 10)

    stale = datetime.now(timezone.utc) - timedelta(
        minutes=auth_settings.LOGIN_LOCKOUT_WINDOW_MIN + 1
    )
    await session.execute(update(LoginAttempt).values(attempted_at=stale))
    await session.commit()

    assert await service.authenticate(session, EMAIL, PW, settings=auth_settings)


async def test_lockout_is_per_email_not_global(session, auth_settings):
    """Keying on email is why one hostile client cannot lock every account behind a shared NAT."""
    await _register(session, auth_settings)
    await service.register(
        session, RegisterRequest(email="other@pu.edu.pk", password=PW), settings=auth_settings
    )

    await _fail(session, auth_settings, 10)

    assert await service.authenticate(session, "other@pu.edu.pk", PW, settings=auth_settings)
