"""F9 CLI entrypoint (T12, AC-20/AC-21).

    python -m app.caching.run --flush
    python -m app.caching.run --delete-query "What is the fee refund policy?"

`--flush` is the documented step after a re-index: entries carry an `index_manifest_id` and expire
lazily on lookup, but a flush makes the next run start cold, which is what the F9 eval gate needs so
its hit rate is a function of the workload rather than of yesterday's traffic.

`--delete-query` is poison control. It keys on the question, not a request id: nothing in the
pipeline generates a request id yet (F13 owns request logging), and the question is what an operator
actually has in hand when they spot a bad cached answer. Normalization is applied for them, so they
can paste the student's text verbatim.

Mirrors `app/evals/run.py`'s shape: argparse, injectable `settings`/`sessionmaker` so the CLI is
testable without touching the app-wide singletons, `async def main(argv) -> int`, and an
`_entrypoint` that turns the return code into a SystemExit.
"""

import argparse
import asyncio
import sys

from app.caching import redis_hot, store
from app.core.settings import settings as default_settings
from app.db.engine import get_sessionmaker


def _parse_args(argv):
    p = argparse.ArgumentParser(prog="app.caching.run")
    p.add_argument("--flush", action="store_true", help="delete every cache entry (both tiers)")
    p.add_argument("--delete-query", metavar="QUESTION",
                   help="delete the single entry this question maps to (poison control)")
    return p.parse_args(argv)


async def main(argv=None, settings=None, sessionmaker=None) -> int:
    args = _parse_args(argv)
    settings = settings or default_settings
    sessionmaker = sessionmaker or get_sessionmaker()

    if args.flush and args.delete_query:
        print("error: --flush and --delete-query are mutually exclusive", file=sys.stderr)
        return 2

    try:
        if args.flush:
            deleted = await store.flush(settings=settings, sessionmaker=sessionmaker)
            print(f"[cache] flushed {deleted} entries")
            return 0

        if args.delete_query:
            deleted = await store.delete_by_query(
                args.delete_query, settings=settings, sessionmaker=sessionmaker
            )
            if deleted:
                print(f"[cache] deleted 1 entry for: {args.delete_query!r}")
                return 0
            print(f"[cache] no cached entry for: {args.delete_query!r}", file=sys.stderr)
            return 1

        print("error: one of --flush or --delete-query is required", file=sys.stderr)
        return 2
    finally:
        # redis.asyncio pools connections; without this the CLI hangs on exit waiting for them.
        await redis_hot.close()


def _entrypoint():
    raise SystemExit(asyncio.run(main()))


if __name__ == "__main__":
    _entrypoint()
