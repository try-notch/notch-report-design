"""
python -m notch_api.admin — change the running server's config and accounts, without a deploy.

    config push <file.json|-> [--note TEXT] validate the body (a file, or - for stdin), then append it
    config show                             the version and body the server is running
    account block <uuid> [--code CODE]      refuse the account's processing and Notch Cloud writes
    account unblock <uuid>

It writes the meter DB named by NOTCH_METER_DB, the same file the server reads (on the
VPS, the `notch-admin` wrapper that deploy/setup.sh installs runs it as the service's
user against that file; see DEPLOY.md). A push that would not validate is refused before
anything is written; the server picks up a new version on its next request. Rolling back
is pushing the older body again: the table is the history.
"""

import argparse
import getpass
import json
import os
import re
import sys

from . import config as env
from .meter import Meter
from .remote_config import ConfigInvalid, RemoteConfig
from .wire_v2 import UUID

CODE = re.compile(r"[a-z][a-z0-9_]{0,31}")


def _parser():
    parser = argparse.ArgumentParser(prog="python -m notch_api.admin", description=__doc__.split("\n\n")[0])
    what = parser.add_subparsers(dest="what", required=True)
    cfg = what.add_parser("config").add_subparsers(dest="action", required=True)
    push = cfg.add_parser("push", help="validate a config body and append it as the next version")
    push.add_argument("file")
    push.add_argument("--note", default=None)
    cfg.add_parser("show", help="print the version and body the server is running")
    account = what.add_parser("account").add_subparsers(dest="action", required=True)
    block = account.add_parser("block", help="block an account")
    block.add_argument("user_id")
    block.add_argument("--code", default="blocked")
    unblock = account.add_parser("unblock", help="unblock an account")
    unblock.add_argument("user_id")
    return parser


def main(argv=None, *, meter_db=None, out=sys.stdout, err=sys.stderr, stdin=sys.stdin):
    args = _parser().parse_args(argv)
    meter = Meter(meter_db or env.METER_DB)
    if args.what == "config":
        remote = RemoteConfig(meter)
        if args.action == "show":
            current = remote.current()
            print(json.dumps({"config_version": current.version, "overrides": current.overrides,
                              "effective": current.data}, indent=2, sort_keys=True), file=out)
            return 0
        try:
            if args.file == "-":
                body = json.load(stdin)
            else:
                with open(args.file, encoding="utf-8") as f:
                    body = json.load(f)
            version = remote.push(body, note=args.note,
                                  created_by=os.environ.get("SUDO_USER") or getpass.getuser())
        except (OSError, ValueError, ConfigInvalid) as exc:
            print(f"refused: {exc}", file=err)
            return 2
        print(f"config version {version} pushed", file=out)
        return 0
    if not UUID.fullmatch(args.user_id):
        print("refused: the account id must be a uuid", file=err)
        return 2
    user_id = args.user_id.lower()
    if args.action == "block":
        if not CODE.fullmatch(args.code):
            print("refused: --code must be a short code (a-z, 0-9, _), never free text", file=err)
            return 2
        meter.set_blocked(user_id, args.code)
        print(f"account {user_id} blocked ({args.code})", file=out)
    else:
        meter.set_blocked(user_id, None)
        print(f"account {user_id} unblocked", file=out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
