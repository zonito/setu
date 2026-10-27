#!/usr/bin/env python3
"""webapi_probe.py — Phase 0 read-only coexistence proof for the
setu-exec Web API cancel channel (setu-exec v0.8; Client Portal
transport added v0.9).

Authenticates to the IBKR Web API with the account's configured
transport (~/.setu/webapi-<account>.json): OAuth 1.0a against
api.ibkr.com, or — the retail path, since IBKR gates OAuth
self-service to institutional clients — the local Client
Portal Gateway, whose browser-established session it checks.
Opens a brokerage session and prints what the channel can see:
auth status, session accounts, and the live-orders list —
flagging the --target order when given. It exists to prove,
BEFORE any cancel is wired live, that a web session coexists
with the IB Gateway feed (watch the Setu feeds while it runs)
and that the feed's order id is the Web API's order id.

READ-ONLY by construction: this script calls no DELETE and
contains no cancel code path at all.

Usage:
  webapi_probe.py --account U19377323 [--target 829711680]
                  [--config-dir ~/.setu]

Exit codes: 0 = session established (and target found, when a
target was given); 2 = credentials absent or unusable; 3 =
session or transport failure; 4 = target not in the list.
"""

import argparse
import json
import sys
from pathlib import Path

import setu_exec


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Read-only Web API coexistence probe (Phase 0).")
    parser.add_argument("--account", required=True,
                        help="IBKR account id, e.g. U19377323")
    parser.add_argument("--target", default="",
                        help="web order id to look for in the live list")
    parser.add_argument("--config-dir",
                        default=str(Path.home() / ".setu"),
                        help="directory holding webapi-<account>.json")
    args = parser.parse_args()

    cfg = {"_config_dir": args.config_dir}
    creds_path = setu_exec.webapi_credentials_path(cfg, args.account)
    try:
        creds = setu_exec.load_webapi_credentials(
            cfg, args.account, enforce_permissions=False)
    except setu_exec.WebApiError as exc:
        print(f"credentials unusable: {exc}")
        return 2
    if creds is None:
        print(f"no credentials file at {creds_path} — the web route "
              f"stays unavailable until a webapi config is in place")
        return 2

    client = setu_exec.build_webapi_client(creds)
    try:
        if getattr(creds, "mode", "oauth") == "client_portal":
            print(f"transport: Client Portal Gateway at "
                  f"{creds.base_url}")
        else:
            lst = client.mint_live_session_token()
            print(f"live session token minted and signature-verified "
                  f"({len(lst)} bytes)")
        init = client.init_brokerage_session()
        print(f"brokerage session initialised: "
              f"{json.dumps(init, default=str)[:200]}")
        status = client.auth_status()
        print(f"auth status: {json.dumps(status, default=str)[:300]}")
        accounts = client.session_accounts()
        print(f"session accounts: {accounts}")
        orders = client.live_orders()
    except setu_exec.WebApiError as exc:
        print(f"Web API session failed: {exc}")
        return 3

    print(f"live orders: {len(orders)}")
    found = False
    for entry in orders:
        marker = ""
        if args.target and str(entry.get("orderId") or "") == str(args.target):
            marker = "  <-- TARGET"
            found = True
        print("  " + json.dumps(entry, default=str)[:300] + marker)
    if args.target:
        if found:
            print(f"TARGET {args.target} is listed under the same "
                  f"order id — the Phase 0 identity holds")
            return 0
        print(f"TARGET {args.target} is NOT in the Web API "
              f"live-orders list")
        return 4
    return 0


if __name__ == "__main__":
    sys.exit(main())
