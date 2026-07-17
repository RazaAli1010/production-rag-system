"""T8 — sessions REST router (AC-1/2/3/4/5/6)."""

import uuid


async def test_anonymous_create_sets_signed_cookie(client):
    r = await client.post("/api/sessions")
    assert r.status_code == 201
    assert "anon_session" in r.headers.get("set-cookie", "")
    body = r.json()
    assert body["title"] is None and body["total_tokens"] == 0


async def test_authed_create_lists_and_is_scoped(client, authed):
    r = await client.post("/api/sessions", headers=authed["headers"])
    assert r.status_code == 201
    sid = r.json()["id"]

    r = await client.get("/api/sessions", headers=authed["headers"])
    assert r.status_code == 200
    assert [s["id"] for s in r.json()] == [sid]


async def test_list_requires_auth(client):
    r = await client.get("/api/sessions")
    assert r.status_code == 401


async def test_foreign_session_is_404(client, authed):
    # someone else's (nonexistent to this user) session id
    r = await client.get(f"/api/sessions/{uuid.uuid4()}/messages", headers=authed["headers"])
    assert r.status_code == 404


async def test_messages_returns_full_transcript(client, authed):
    from app.db.enums import MessageRole
    from app.db.models.chat import Message

    r = await client.post("/api/sessions", headers=authed["headers"])
    sid = uuid.UUID(r.json()["id"])

    # seed two messages directly (the ask route is exercised in test_ask_memory)
    from .conftest import make_settings  # noqa: F401 — keep import local to avoid ordering issues

    # use the app dependency's DB by hitting the endpoint after inserting via a fresh session
    import app.db.engine as db_engine

    import datetime as dt
    base = dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc)
    async with db_engine.get_sessionmaker()() as db:
        db.add(Message(session_id=sid, role=MessageRole.user, content="hi", token_count=1,
                       created_at=base))
        db.add(Message(session_id=sid, role=MessageRole.assistant, content="hello", token_count=1,
                       created_at=base + dt.timedelta(seconds=1)))
        await db.commit()

    r = await client.get(f"/api/sessions/{sid}/messages", headers=authed["headers"])
    assert r.status_code == 200
    contents = [m["content"] for m in r.json()]
    assert contents == ["hi", "hello"]


async def test_delete_archives(client, authed):
    r = await client.post("/api/sessions", headers=authed["headers"])
    sid = r.json()["id"]

    r = await client.delete(f"/api/sessions/{sid}", headers=authed["headers"])
    assert r.status_code == 204

    r = await client.get("/api/sessions", headers=authed["headers"])
    assert [s["id"] for s in r.json()] == []  # archived → hidden (AC-5)


async def test_auto_title_truncated_to_60(client, authed):
    """The title is set from the first persisted question; drive one ask to trigger it."""
    # created here, titled by the ask route in test_ask_memory; this test asserts the cap via service
    from app.memory import service

    import app.db.engine as db_engine
    from .conftest import make_settings

    settings = make_settings()
    async with db_engine.get_sessionmaker()() as db:
        s = await service.create_session(db, user_id=authed["user_id"])
        await db.commit()
        await service.persist_user(db, s, "y" * 200, settings)
        await db.commit()
        assert len(s.title) == 60
