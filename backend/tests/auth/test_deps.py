import uuid

import pytest

from app.auth.deps import get_current_user_optional, rate_tier
from app.auth.schemas import Principal
from app.core.exceptions import AuthError
from app.db.enums import UserRole


def _principal(kind, role=None):
    return Principal(
        kind=kind,
        user_id=uuid.UUID("00000000-0000-0000-0000-000000000001"),
        email="x@pu.edu.pk",
        role=role or (UserRole.admin if kind == "admin" else UserRole.student),
        api_key_id=uuid.UUID("00000000-0000-0000-0000-000000000002") if kind == "api_key" else None,
    )


@pytest.mark.parametrize(
    "principal, expected",
    [
        (None, ("ip:1.2.3.4", 5)),
        (_principal("student"), ("user:00000000-0000-0000-0000-000000000001", 20)),
        (_principal("admin"), ("user:00000000-0000-0000-0000-000000000001", 60)),
        (_principal("api_key"), ("apikey:00000000-0000-0000-0000-000000000002", 30)),
    ],
    ids=["anonymous", "student", "admin", "api_key"],
)
def test_rate_tier_matches_the_matrix(principal, expected):
    assert rate_tier(principal, "1.2.3.4") == expected


def test_an_admins_api_key_gets_the_api_key_tier_not_the_admin_tier():
    p = _principal("api_key", role=UserRole.admin)

    assert rate_tier(p, "1.2.3.4")[1] == 30


async def test_anonymous_costs_no_db_query():
    """AC-26: the zero-friction ask path must not touch Postgres to learn nobody is logged in."""

    class ExplodingSession:
        def __getattr__(self, name):
            raise AssertionError("anonymous must not query the DB")

    assert await get_current_user_optional(token=None, key=None, session=ExplodingSession()) is None


async def test_a_bad_token_does_not_silently_downgrade_to_anonymous(session, auth_settings):
    """AC-27: absent means anonymous; wrong means wrong."""
    with pytest.raises(AuthError) as exc:
        await get_current_user_optional(token="not.a.token", key=None, session=session)

    assert exc.value.status == 401
