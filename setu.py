#!/usr/bin/env python3
"""Setu — the bridge between IB Gateway and Kubera.

A small, read-only daemon that sits next to IB Gateway on the same
machine, pulls account values and open orders over the local API
socket, and pushes them to Kubera's existing broker ingest endpoints
(the same contracts the IBKR Enhancer userscript uses):

  POST /api/v1/broker-accounts/snapshot
  POST /api/v1/broker-orders/snapshot

Setu NEVER places, cancels or modifies an order. It only reads:
account summary, and all open orders for one configured account.

Run it under the LaunchAgent installed by install.sh and forget it:
it starts at login, reconnects to the Gateway on its own with
backoff, pushes changed values within a minute, and heartbeats
unchanged values every 5 minutes so Kubera never shows it stale
while it is alive.

Config: ~/.setu/config.json (see config.example.json).
Token:  read from the file named by "token_file" in the config
        (default ~/.kubera-ingest-token). Never logged, never sent
        anywhere except as the X-Kubera-Ingest-Token header to Kubera.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
import urllib.request
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

VERSION = "0.1.1"
LONDON = ZoneInfo("Europe/London")

# IB account summary tag -> Kubera values key (the ingest contract).
SUMMARY_TAG_MAP = {
    "NetLiquidation": "net_liquidation",
    "ExcessLiquidity": "excess_liquidity",
    "BuyingPower": "buying_power",
    "MaintMarginReq": "maintenance_margin",
    "UnrealizedPnL": "unrealized_pnl",
    "RealizedPnL": "realized_pnl",
    "SettledCash": "settled_cash",
}
SUMMARY_TAGS = ",".join(SUMMARY_TAG_MAP.keys())
UNSET_PRICE = 1.0e100  # ibapi marks unset doubles with a huge sentinel


def log(msg: str) -> None:
    stamp = datetime.now(LONDON).strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{stamp}] {msg}", flush=True)


def london_now_iso() -> str:
    return datetime.now(LONDON).isoformat(timespec="seconds")


def to_float(value) -> float | None:
    try:
        if value is None or value == "":
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Pure payload mapping (unit-tested in tests/test_mapping.py)
# ---------------------------------------------------------------------------

def account_snapshot(account_id: str, label: str | None, currency: str,
                     values_by_tag: dict[str, float | None],
                     captured_at: str) -> dict:
    values = {key: values_by_tag.get(tag) for tag, key in SUMMARY_TAG_MAP.items()}
    return {
        "ingest_source": "setu",
        "account_id": account_id,
        "account_label": label,
        "captured_at": captured_at,
        "complete": values["net_liquidation"] is not None,
        "currency": currency or "USD",
        "values": values,
    }


def expiry_iso(last_trade: str | None) -> str | None:
    raw = str(last_trade or "").strip()
    if len(raw) == 8 and raw.isdigit():
        return f"{raw[0:4]}-{raw[4:6]}-{raw[6:8]}"
    if len(raw) == 6 and raw.isdigit():  # YYYYMM (monthly, no day)
        return f"{raw[0:4]}-{raw[4:6]}-01"
    return None


def broker_order(order_id, contract, order, order_state) -> dict:
    sec_type = str(getattr(contract, "secType", "") or "")
    is_option = sec_type in ("OPT", "FOP")
    right = str(getattr(contract, "right", "") or "")
    lmt = to_float(getattr(order, "lmtPrice", None))
    total = to_float(getattr(order, "totalQuantity", None)) or 0.0
    filled = to_float(getattr(order_state, "filled", None))
    remain = to_float(getattr(order_state, "remaining", None))
    if filled is None and remain is not None:
        filled = total - remain
    if remain is None and filled is not None:
        remain = total - filled
    strike = to_float(getattr(contract, "strike", None))
    return {
        "order_id": str(order_id),
        "symbol": str(getattr(contract, "symbol", "") or ""),
        "asset_type": "option" if is_option else "equity",
        "expiry_date": expiry_iso(getattr(contract, "lastTradeDateOrContractMonth", None)) if is_option else None,
        "strike_price": strike if (is_option and strike and strike > 0) else None,
        "option_type": ("Put" if right == "P" else "Call" if right == "C" else None) if is_option else None,
        "action": "Buy" if str(getattr(order, "action", "")) == "BUY" else "Sell",
        "qty_total": total,
        "qty_filled": filled if filled is not None else 0.0,
        "qty_remain": remain if remain is not None else 0.0,
        "limit_price": lmt if (lmt is not None and lmt < UNSET_PRICE) else None,
        "tif": (str(getattr(order, "tif", "") or "") or None),
        "status": str(getattr(order_state, "status", "") or ""),
        "status_at": None,
        "currency": str(getattr(contract, "currency", "") or "USD"),
        "raw": "",
        "legs": [],
    }


def orders_snapshot(account_id: str, label: str | None, captured_at: str,
                    orders: list[dict]) -> dict:
    return {
        "ingest_source": "setu",
        "account_id": account_id,
        "account_label": label,
        "captured_at": captured_at,
        "complete": True,
        "orders": orders,
    }


def raw_line(o: dict) -> str:
    parts = [o["symbol"]]
    if o["asset_type"] == "option":
        parts += [o["expiry_date"] or "", str(o["strike_price"] or ""), o["option_type"] or ""]
    parts += [o["action"], str(o["qty_total"])]
    if o["limit_price"] is not None:
        parts += ["@", str(o["limit_price"])]
    if o["tif"]:
        parts.append(o["tif"])
    if o["status"]:
        parts.append(o["status"])
    return " ".join(p for p in parts if p) + " (setu)"


# ---------------------------------------------------------------------------
# Gateway client
# ---------------------------------------------------------------------------

class GatewayClient:
    """ibapi wrapper with the state Setu needs. Read-only calls only."""

    def __init__(self, cfg: dict):
        from ibapi.client import EClient
        from ibapi.wrapper import EWrapper

        self.cfg = cfg
        self.account = cfg["account_id"]
        self.summary: dict[str, tuple[float | None, str]] = {}
        self.summary_at = 0.0
        self.orders: list = []  # (join key, mapped order dict) per sweep
        self.status_by_order: dict = {}  # join key -> (status, filled, remaining)
        self.orders_event = threading.Event()
        self.connected_event = threading.Event()
        self.managed_accounts: list[str] = []
        outer = self

        class Wrapper(EWrapper):
            def nextValidId(self, orderId):
                outer.connected_event.set()

            def managedAccounts(self, accountsList):
                outer.managed_accounts = [a.strip() for a in str(accountsList).split(",") if a.strip()]

            def accountSummary(self, reqId, account, tag, value, currency):
                if str(account) != outer.account:
                    return
                if tag in SUMMARY_TAG_MAP:
                    outer.summary[tag] = (to_float(value), str(currency or "USD"))
                    outer.summary_at = time.time()

            def openOrder(self, orderId, contract, order, orderState):
                if str(getattr(order, "account", "") or "") != outer.account:
                    return
                perm = getattr(order, "permId", 0)
                mapped = broker_order(perm if perm else orderId, contract, order, orderState)
                mapped["raw"] = raw_line(mapped)
                # Join key for orderStatus: permId (globally unique).
                # Bare orderIds repeat across clients and must not be
                # used to merge quantities.
                key = ("p", int(perm)) if perm else ("i", int(orderId))
                outer.orders.append((key, mapped))

            def openOrderEnd(self):
                outer.orders_event.set()

            def orderStatus(self, orderId, status, filled, remaining, avgFillPrice,
                            permId=0, parentId=0, lastFillPrice=0.0, clientId=0,
                            whyHeld="", mktCapPrice=0.0):
                # In this API generation the live quantities arrive here,
                # not on openOrder's OrderState (which leaves them unset).
                key = ("p", int(permId)) if permId else ("i", int(orderId))
                outer.status_by_order[key] = (
                    str(status or ""), to_float(filled), to_float(remaining))

            def connectionClosed(self):
                outer.connected_event.clear()

            def error(self, reqId, errorCode, errorString, advancedOrderRejectJson=""):
                # 2104/2106/2107/2158 are benign farm-status notices.
                if errorCode in (2104, 2106, 2107, 2158, 2119):
                    return
                log(f"gateway error {errorCode}: {errorString}")

        class Client(EClient):
            pass

        self.wrapper = Wrapper()
        self.client = Client(self.wrapper)

    # -- lifecycle ------------------------------------------------------
    def connect(self) -> bool:
        self.connected_event.clear()
        self.client.connect(self.cfg["ib_host"], int(self.cfg["ib_port"]),
                            int(self.cfg["client_id"]))
        self.thread = threading.Thread(target=self.client.run, daemon=True)
        self.thread.start()
        if not self.connected_event.wait(timeout=15):
            return False
        self.client.reqAccountSummary(9001, "All", SUMMARY_TAGS)
        return True

    def disconnect(self):
        try:
            self.client.disconnect()
        except Exception:
            pass
        self.connected_event.clear()

    def is_connected(self) -> bool:
        try:
            return bool(self.client.isConnected()) and self.connected_event.is_set()
        except Exception:
            return False

    # -- reads ----------------------------------------------------------
    def apply_statuses(self) -> list[dict]:
        """Merge orderStatus quantities into the swept open orders."""
        out = []
        for api_id, mapped in self.orders:
            st = self.status_by_order.get(api_id)
            if st:
                status, filled, remaining = st
                if status:
                    mapped["status"] = status
                if filled is not None:
                    mapped["qty_filled"] = filled
                if remaining is not None:
                    mapped["qty_remain"] = remaining
            out.append(mapped)
        return out

    def fetch_orders(self, timeout: float = 20.0) -> list[dict] | None:
        self.orders = []
        self.orders_event.clear()
        self.client.reqAllOpenOrders()
        if not self.orders_event.wait(timeout=timeout):
            return None
        return self.apply_statuses()


# ---------------------------------------------------------------------------
# Kubera push
# ---------------------------------------------------------------------------

class KuberaPush:
    def __init__(self, cfg: dict):
        self.base = str(cfg["kubera_base"]).rstrip("/")
        token_path = Path(os.path.expanduser(str(cfg["token_file"])))
        self.token = token_path.read_text(encoding="utf-8").strip()
        proxy = cfg.get("http_proxy")
        if proxy:
            opener = urllib.request.build_opener(
                urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
        else:
            opener = urllib.request.build_opener()
        self.opener = opener

    def post(self, path: str, payload: dict) -> tuple[bool, str]:
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            self.base + path, data=data, method="POST",
            headers={"Content-Type": "application/json",
                     "X-Kubera-Ingest-Token": self.token})
        try:
            with self.opener.open(req, timeout=15) as resp:
                body = json.loads(resp.read().decode("utf-8") or "{}")
        except Exception as exc:  # network or HTTP error: report, retry next tick
            return False, f"{type(exc).__name__}: {exc}"
        if body.get("accepted") is False:
            return True, f"noted: {body.get('reason', 'rejected')}"
        return True, "accepted"


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

class Setu:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.interval = float(cfg.get("interval_seconds", 60))
        self.heartbeat = float(cfg.get("heartbeat_seconds", 300))
        self.push = KuberaPush(cfg)
        self.gw = GatewayClient(cfg)
        self.last_account_fp: str | None = None
        self.last_account_push = 0.0
        self.last_orders_fp: str | None = None
        self.last_orders_push = 0.0

    def account_payload(self) -> dict:
        values = {tag: pair[0] for tag, pair in self.gw.summary.items()}
        currency = "USD"
        if "NetLiquidation" in self.gw.summary:
            currency = self.gw.summary["NetLiquidation"][1]
        return account_snapshot(self.cfg["account_id"], self.cfg.get("account_label"),
                                currency, values, london_now_iso())

    def tick(self) -> None:
        now = time.time()
        # Account values
        payload = self.account_payload()
        fp = json.dumps(payload["values"], sort_keys=True)
        if payload["complete"] and (fp != self.last_account_fp or now - self.last_account_push >= self.heartbeat):
            ok, detail = self.push.post("/api/v1/broker-accounts/snapshot", payload)
            if ok:
                self.last_account_fp, self.last_account_push = fp, now
                v = payload["values"]
                log(f"account push ({detail}): NLV {v['net_liquidation']} excess {v['excess_liquidity']}")
            else:
                log(f"account push failed: {detail}")
        # Open orders
        orders = self.gw.fetch_orders()
        if orders is not None:
            opayload = orders_snapshot(self.cfg["account_id"], self.cfg.get("account_label"),
                                        london_now_iso(), orders)
            ofp = json.dumps([(o["order_id"], o["status"], o["qty_remain"], o["limit_price"]) for o in orders])
            if ofp != self.last_orders_fp or now - self.last_orders_push >= self.heartbeat:
                ok, detail = self.push.post("/api/v1/broker-orders/snapshot", opayload)
                if ok:
                    self.last_orders_fp, self.last_orders_push = ofp, now
                    log(f"orders push ({detail}): {len(orders)} open orders")
                else:
                    log(f"orders push failed: {detail}")

    def run(self) -> None:
        backoff = 5.0
        log(f"setu {VERSION} starting: account {self.cfg['account_id']}, "
            f"gateway {self.cfg['ib_host']}:{self.cfg['ib_port']}, kubera {self.cfg['kubera_base']}")
        while True:
            if not self.gw.is_connected():
                self.gw.disconnect()
                try:
                    connected = self.gw.connect()
                except Exception as exc:
                    log(f"gateway connect raised ({type(exc).__name__}: {exc})")
                    connected = False
                if connected:
                    backoff = 5.0
                    log(f"connected to gateway; managed accounts: {', '.join(self.gw.managed_accounts)}")
                    if self.gw.managed_accounts and self.cfg["account_id"] not in self.gw.managed_accounts:
                        log(f"WARNING: account {self.cfg['account_id']} is not on this gateway login")
                else:
                    log(f"gateway connect failed; retrying in {backoff:.0f}s")
                    time.sleep(backoff)
                    backoff = min(backoff * 2, 60.0)
                    continue
            try:
                self.tick()
            except Exception as exc:
                log(f"tick error ({type(exc).__name__}: {exc}); reconnecting")
                self.gw.disconnect()
                continue
            time.sleep(self.interval)


def dry_run(cfg: dict) -> int:
    gw = GatewayClient(cfg)
    if not gw.connect():
        print("dry-run: could not connect to the gateway")
        return 1
    print(f"dry-run: managed accounts: {', '.join(gw.managed_accounts)}")
    deadline = time.time() + 12
    while time.time() < deadline and "NetLiquidation" not in gw.summary:
        time.sleep(0.5)
    values = {tag: pair[0] for tag, pair in gw.summary.items()}
    currency = gw.summary.get("NetLiquidation", (None, "USD"))[1]
    snap = account_snapshot(cfg["account_id"], cfg.get("account_label"), currency,
                            values, london_now_iso())
    print("account snapshot that would be pushed:")
    print(json.dumps(snap, indent=2))
    orders = gw.fetch_orders()
    if orders is None:
        print("orders: sweep timed out")
    else:
        osnap = orders_snapshot(cfg["account_id"], cfg.get("account_label"),
                                london_now_iso(), orders)
        print(f"orders snapshot: {len(orders)} open orders for {cfg['account_id']}")
        for o in orders[:10]:
            print("  " + o["raw"])
        print(json.dumps({k: osnap[k] for k in ('ingest_source', 'account_id', 'complete')}, indent=2))
    gw.disconnect()
    return 0


def load_config(path: str) -> dict:
    cfg = json.loads(Path(path).read_text(encoding="utf-8"))
    defaults = {
        "account_label": None,
        "ib_host": "127.0.0.1",
        "ib_port": 4001,
        "client_id": 21,
        "kubera_base": "http://127.0.0.1:8000",
        "token_file": "~/.kubera-ingest-token",
        "interval_seconds": 60,
        "heartbeat_seconds": 300,
        "http_proxy": None,
    }
    for key, val in defaults.items():
        cfg.setdefault(key, val)
    if not cfg.get("account_id"):
        raise SystemExit("config is missing account_id")
    return cfg


def main() -> int:
    parser = argparse.ArgumentParser(description="Setu — IB Gateway to Kubera bridge (read-only)")
    parser.add_argument("--config", default=os.path.expanduser("~/.setu/config.json"))
    parser.add_argument("--dry-run", action="store_true",
                        help="connect, print the snapshots that would be pushed, exit")
    args = parser.parse_args()
    cfg = load_config(args.config)
    if args.dry_run:
        return dry_run(cfg)
    Setu(cfg).run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
