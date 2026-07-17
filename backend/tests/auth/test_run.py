from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select, update

from app.auth import service
from app.auth.run import main
from app.auth.schemas import RegisterRequest
from app.core.exceptions import AuthError
from app.db.models import ApiKey, LoginAttempt, RefreshToken

PW = "probation123"
EMAIL = "s@pu.edu.pk"


@pytest.fixture
async def user(sessionmaker_, auth_settings):
    async with sessionmaker_() as s:
        return await service.register(
            s, RegisterRequest(email=EMAIL, password=PW), settings=auth_settings
        )


async def _run(argv, sessionmaker_, auth_settings):
    return await main(argv, settings=auth_settings, sessionmaker=sessionmaker_)


@pytest.mark.parametrize(
    "argv",
    [[], ["--prune", "--issue-key"], ["--issue-key"], ["--revoke-key"]],
    ids=["no_mode", "two_modes", "issue_without_email", "revoke_without_label"],
)
async def test_usage_errors_return_2(argv, sessionmaker_, auth_settings):
    assert await _run(argv, sessionmaker_, auth_settings) == 2


async def test_prune_deletes_expired_and_keeps_live(user, sessionmaker_, auth_settings, session):
    async with sessionmaker_() as s:
        await service.authenticate(s, EMAIL, PW, settings=auth_settings)
        with pytest.raises(AuthError):
            await service.authenticate(s, EMAIL, "wrongpassword", settings=auth_settings)
        await service.authenticate(s, EMAIL, PW, settings=auth_settings)

    # age one refresh token past expiry and one attempt out of the lockout window
    stale = datetime.now(timezone.utc) - timedelta(days=1)
    async with sessionmaker_() as s:
        first = (await s.scalars(select(RefreshToken).order_by(RefreshToken.issued_at))).first()
        await s.execute(
            update(RefreshToken).where(RefreshToken.id == first.id).values(expires_at=stale)
        )
        oldest = (await s.scalars(select(LoginAttempt).order_by(LoginAttempt.attempted_at))).first()
        await s.execute(
            update(LoginAttempt).where(LoginAttempt.id == oldest.id).values(attempted_at=stale)
        )
        await s.commit()

    assert await _run(["--prune"], sessionmaker_, auth_settings) == 0

    assert len((await session.scalars(select(RefreshToken))).all()) == 1
    assert len((await session.scalars(select(LoginAttempt))).all()) == 2


async def test_issue_key_prints_once_and_stores_only_the_hash(
    user, sessionmaker_, auth_settings, session, capsys
):
    code = await _run(
        ["--issue-key", "--email", EMAIL, "--label", "telegram"], sessionmaker_, auth_settings
    )
    printed = capsys.readouterr().out

    assert code == 0
    raw = printed.strip().splitlines()[-1]
    assert raw.startswith("crag_")

    key = await session.scalar(select(ApiKey))
    assert key.key_hash != raw
    assert raw not in key.key_hash
    assert (await service.resolve_api_key(session, raw, settings=auth_settings)).user_id == user.id


async def test_issue_key_for_an_unknown_user_returns_1(sessionmaker_, auth_settings):
    code = await _run(
        ["--issue-key", "--email", "nobody@pu.edu.pk", "--label", "x"], sessionmaker_, auth_settings
    )

    assert code == 1


async def test_revoke_key(user, sessionmaker_, auth_settings, session, capsys):
    await _run(["--issue-key", "--email", EMAIL, "--label", "telegram"], sessionmaker_, auth_settings)
    raw = capsys.readouterr().out.strip().splitlines()[-1]

    assert await _run(["--revoke-key", "--label", "telegram"], sessionmaker_, auth_settings) == 0

    with pytest.raises(AuthError):
        await service.resolve_api_key(session, raw, settings=auth_settings)


async def test_revoke_an_unknown_label_returns_1(sessionmaker_, auth_settings):
    assert await _run(["--revoke-key", "--label", "nope"], sessionmaker_, auth_settings) == 1
