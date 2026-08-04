"""F15 load test — 50 concurrent users against `POST /api/ask` (SSE).

    LOAD_HOST=https://your-api.onrender.com make load

Mix, per the F15 brief:
  * 80% repeat / 20% novel questions          (questions.py)
  * mixed authenticated + anonymous users     (LOAD_EMAIL/LOAD_PASSWORD, else all anonymous)
  * ~30% of users run 3-turn session conversations, exercising F17 memory reads and writes

The one thing this file must get right is **measuring the stream, not the handshake**. Locust's
default `catch_response=False` marks a streaming request successful the moment response headers
arrive, which for SSE is near-instant and completely meaningless — it would report the p95 of
"the server accepted the connection". Every request below is therefore consumed to its terminal
`done` frame, and a stream that ends without one is a failure.

Two timings are recorded per ask:
  * `POST /api/ask`      — full time to the `done` frame (the number the p95 gate is about)
  * `TTFT /api/ask`      — time to the first `token` frame (what a streaming UI actually feels like)
"""

import json
import os
import random
import time

from locust import HttpUser, between, events, task

import questions

DEEP = False
NAMESPACE = None  # None ⇒ fan out over both pu and hec, the default production shape


def _consume_sse(user, *, question: str, session_id: str | None, name: str):
    """POST /api/ask and drain the stream to `done`. Returns the session_id seen in `meta`."""
    payload = {"question": question}
    if session_id:
        payload["session_id"] = session_id
    if NAMESPACE:
        payload["namespace"] = NAMESPACE
    if DEEP:
        payload["deep"] = True

    started = time.perf_counter()
    ttft = None
    seen_done = False
    error = None
    meta = {}

    with user.client.post(
        "/api/ask",
        json=payload,
        headers={**user.auth_headers, "Accept": "text/event-stream"},
        stream=True,
        catch_response=True,
        name=name,
    ) as resp:
        # 429 is the rate limiter doing its job, not a server fault. Tagged, reported separately,
        # and NOT counted as a failure — but see the README: a high 429 rate invalidates the run,
        # because a p95 achieved by being rejected is not a p95.
        if resp.status_code == 429:
            events.request.fire(request_type="TAG", name="429 rate-limited",
                                response_time=0, response_length=0, exception=None)
            resp.success()
            return session_id
        if resp.status_code == 409:
            # F17 holds a per-session asyncio.Lock; a concurrent second ask on one session is a
            # deliberate 409. Expected in principle, but >1% means think-time is too short and the
            # session cohort is measuring lock contention instead of the pipeline.
            events.request.fire(request_type="TAG", name="409 session_busy",
                                response_time=0, response_length=0, exception=None)
            resp.success()
            return session_id
        if resp.status_code != 200:
            resp.failure(f"HTTP {resp.status_code}")
            return session_id

        event = None
        for raw in resp.iter_lines(decode_unicode=True):
            if raw is None:
                continue
            line = raw.strip()
            if not line:
                event = None            # blank line terminates a frame
                continue
            if line.startswith("event:"):
                event = line[6:].strip()
            elif line.startswith("data:"):
                if event == "token" and ttft is None:
                    ttft = (time.perf_counter() - started) * 1000
                elif event == "meta":
                    try:
                        meta = json.loads(line[5:].strip())
                    except json.JSONDecodeError:
                        pass
                elif event == "error":
                    error = line[5:].strip()[:200]
                    break
                elif event == "done":
                    seen_done = True
                    break

        if error:
            resp.failure(f"stream error: {error}")
        elif not seen_done:
            # A truncated stream is the failure mode this whole function exists to catch.
            resp.failure("stream ended without a `done` frame")
        else:
            resp.success()

    if ttft is not None:
        events.request.fire(request_type="TTFT", name="TTFT /api/ask",
                            response_time=ttft, response_length=0, exception=None)
    if meta.get("cache_hit"):
        events.request.fire(request_type="TAG", name="cache hit",
                            response_time=0, response_length=0, exception=None)
    return meta.get("session_id") or session_id


class _Base(HttpUser):
    abstract = True
    wait_time = between(2, 6)

    def on_start(self):
        self.rng = random.Random()
        self.auth_headers = {}
        # Half the users authenticate, so both rate-limit tiers are exercised
        # (RATE_LIMIT_ANON_PER_MIN=5 vs RATE_LIMIT_STUDENT_PER_MIN=20).
        email, password = os.getenv("LOAD_EMAIL"), os.getenv("LOAD_PASSWORD")
        if email and password and self.rng.random() < 0.5:
            r = self.client.post("/api/auth/token",
                                 data={"username": email, "password": password},
                                 name="POST /api/auth/token")
            if r.status_code == 200:
                self.auth_headers = {"Authorization": f"Bearer {r.json()['access_token']}"}


class SingleTurnUser(_Base):
    """The ~70% who ask one question and leave. Stateless: no session_id."""

    weight = 7

    @task
    def ask(self):
        question, is_repeat = questions.pick(self.rng)
        _consume_sse(self, question=question, session_id=None,
                     name=f"POST /api/ask [{'repeat' if is_repeat else 'novel'}]")


class SessionUser(_Base):
    """The ~30% who hold a 3-turn conversation — F17 memory reads and writes under load.

    The turns are SEQUENTIAL by construction: F17 serialises a session behind an asyncio.Lock and
    returns 409 `session_busy` to a concurrent second ask, so a parallel client would measure the
    lock rather than the pipeline.
    """

    weight = 3

    @task
    def conversation(self):
        r = self.client.post("/api/sessions", headers=self.auth_headers,
                             name="POST /api/sessions")
        if r.status_code != 201:
            return
        session_id = r.json()["id"]
        for turn in questions.CONVERSATION:
            session_id = _consume_sse(self, question=turn, session_id=session_id,
                                      name="POST /api/ask [session]")
            time.sleep(self.rng.uniform(1.5, 4.0))  # think time between turns
