"""Key issuance is a CLI, not an endpoint: the consumer is the F16 Telegram bot, provisioned once
by an operator. Pruning is a command, not a scheduler — F15 owns scheduling it."""

import argparse
import asyncio
import sys

from app.auth import service
from app.core.exceptions import AuthError
from app.core.settings import settings as default_settings
from app.db.engine import get_sessionmaker


def _parse_args(argv):
    p = argparse.ArgumentParser(prog="app.auth.run")
    p.add_argument("--prune", action="store_true",
                   help="delete expired refresh tokens and aged-out login attempts")
    p.add_argument("--issue-key", action="store_true", help="mint an API key for --email")
    p.add_argument("--revoke-key", action="store_true", help="revoke the API key named --label")
    p.add_argument("--email", help="owning user for --issue-key")
    p.add_argument("--label", help="key label for --issue-key / --revoke-key")
    return p.parse_args(argv)


async def main(argv=None, settings=None, sessionmaker=None) -> int:
    args = _parse_args(argv)
    settings = settings or default_settings
    sessionmaker = sessionmaker or get_sessionmaker()

    modes = [args.prune, args.issue_key, args.revoke_key]
    if sum(modes) != 1:
        print("error: exactly one of --prune, --issue-key, --revoke-key is required",
              file=sys.stderr)
        return 2

    async with sessionmaker() as session:
        if args.prune:
            tokens, attempts = await service.prune(session, settings=settings)
            print(f"[auth] pruned {tokens} refresh tokens, {attempts} login attempts")
            return 0

        if args.issue_key:
            if not (args.email and args.label):
                print("error: --issue-key requires --email and --label", file=sys.stderr)
                return 2
            try:
                raw = await service.issue_key(session, args.email, args.label)
            except AuthError as exc:
                print(f"error: {exc.detail}: {args.email}", file=sys.stderr)
                return 1
            print(f"[auth] issued key for {args.email} ({args.label}). Shown once, store it now:")
            print(raw)
            return 0

        if not args.label:
            print("error: --revoke-key requires --label", file=sys.stderr)
            return 2
        revoked = await service.revoke_key(session, args.label)
        if revoked:
            print(f"[auth] revoked {revoked} key(s) labelled {args.label!r}")
            return 0
        print(f"[auth] no live key labelled {args.label!r}", file=sys.stderr)
        return 1


def _entrypoint():
    raise SystemExit(asyncio.run(main()))


if __name__ == "__main__":
    _entrypoint()
