#!/usr/bin/env python3
"""Setu — the bridge between IB Gateway and Kubera.

A small, read-only daemon that sits next to IB Gateway on the same
machine, pulls account values and open orders over the local API
socket, and pushes them to Kubera's existing broker ingest endpoints
(the same contracts the IBKR Enhancer userscript uses):

  POST /api/v1/broker-accounts/snapshot
  POST /api/v1/broker-orders/snapshot
  POST /api/v1/broker-positions/snapshot

Setu NEVER places, cancels or modifies an order. It only reads:
the account summary, the account-updates stream (portfolio +
account values) and all open orders for one configured account.

Run it under the LaunchAgent installed by install.sh and forget it:
it starts at login, reconnects to the Gateway on its own with
backoff, pushes changed values within a minute, and heartbeats
unchanged values every 5 minutes so Kubera never shows it stale
while it is alive.

Config: ~/.setu/config.json (see config.example.json).
Token:  read from the file named by "token_file" in the config
        (default ~/.kubera-ingest-token). Never logged, never sent
        anywhere except as the X-Kubera-Ingest-Token header to Kubera.

v0.2.1 (2026-10-23): bounded, quieter logs only (no feed changes).
Under launchd the daemon writes its own log through a rotating
sink — copytruncate at ~2 MB, 3 backups, ~8 MB max, same
~/.setu/logs/setu.log path — and repeated IB farm/connectivity
errors are logged once per run with a summary count instead of
one line per cycle. Same shape as setu-exec v0.2.2.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sys
import threading
import time
import urllib.request
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

VERSION = "0.2.1"
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


# ---------------------------------------------------------------------------
# Logging: bounded rotating file under launchd, farm-error dedupe.
# (Same shape as setu_exec.py; the two programs are standalone files.)
#
# launchd points StandardOutPath at ~/.setu/logs/<prog>.log, but a
# process that only prints cannot bound that file: launchd owns the
# fd. So in daemon mode (stdout not a tty) log() writes through
# RotatingLogSink, which owns an O_APPEND fd to the SAME path and
# copytruncates it at ~2 MB, keeping 3 backups (~8 MB max per
# program). The path and inode never change, so `tail -f` on the
# live log keeps working across rotations, and because this process
# is the file's only writer under a lock, no line is lost or
# duplicated at the seam. In a terminal, log() prints as before.
# ---------------------------------------------------------------------------

LOG_MAX_BYTES = 2_000_000
LOG_BACKUP_COUNT = 3
# IB farm / connectivity notices that repeat verbatim while a farm
# is sick (the benign set below never reaches the logger at all).
FARM_ERROR_CODES = {1100, 1101, 1102, 2103, 2105, 2110, 2157}


class RotatingLogSink:
    """An append-mode log file with copytruncate rotation."""

    def __init__(self, path, max_bytes: int = LOG_MAX_BYTES,
                 backups: int = LOG_BACKUP_COUNT):
        self.path = Path(path)
        self.max_bytes = int(max_bytes)
        self.backups = int(backups)
        self._lock = threading.Lock()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = open(self.path, "ab", buffering=0)
        self._size = os.fstat(self._fh.fileno()).st_size
        if self._size > self.max_bytes:
            self._rotate()  # an oversized pre-upgrade log trims at once

    def write(self, line: str) -> None:
        data = (line + "\n").encode("utf-8")
        with self._lock:
            if self._size > 0 and self._size + len(data) > self.max_bytes:
                self._rotate()
            self._fh.write(data)
            self._size += len(data)

    def _rotate(self) -> None:
        for i in range(self.backups - 1, 0, -1):
            try:
                self._backup(i).replace(self._backup(i + 1))
            except OSError:
                pass
        try:
            with open(self.path, "rb") as src, open(self._backup(1), "wb") as dst:
                shutil.copyfileobj(src, dst)
        except OSError:
            pass
        os.ftruncate(self._fh.fileno(), 0)
        self._size = 0

    def _backup(self, n: int) -> Path:
        return self.path.with_name(self.path.name + f".{n}")


class FarmErrorDeduper:
    """First occurrence of a farm error logs at once; consecutive
    repeats of the same code are counted, not logged. The count
    surfaces as one summary line when the run ends — a different
    farm code, any ordinary log line, or an explicit flush — so a
    sick farm costs two lines, not one per minute."""

    def __init__(self):
        self._lock = threading.Lock()
        self._run_code: int | None = None
        self._suppressed = 0

    def handle(self, code: int, text: str, emit) -> None:
        with self._lock:
            if code == self._run_code:
                self._suppressed += 1
                return
            self._flush_locked(emit)
            self._run_code = code
            self._suppressed = 0
            emit(f"gateway error {code}: {text}")

    def flush(self, emit) -> None:
        with self._lock:
            self._flush_locked(emit)

    def _flush_locked(self, emit) -> None:
        if self._run_code is not None and self._suppressed:
            emit(f"gateway error {self._run_code} repeated "
                 f"{self._suppressed} more times, suppressed")
        self._run_code = None
        self._suppressed = 0


_LOG_SINK: RotatingLogSink | None = None
_FARM = FarmErrorDeduper()


def _write_log_line(msg: str) -> None:
    stamp = datetime.now(LONDON).strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{stamp}] {msg}"
    if _LOG_SINK is not None:
        _LOG_SINK.write(line)
    else:
        print(line, flush=True)


def log(msg: str) -> None:
    # An ordinary line ends any farm-error run: flush its summary
    # first so the count is never stranded.
    _FARM.flush(_write_log_line)
    _write_log_line(msg)


def log_gateway_error(code: int, text: str) -> None:
    """Route for IB error callbacks: farm/connectivity codes go
    through the deduper, everything else logs in full."""
    if code in FARM_ERROR_CODES:
        _FARM.handle(code, text, _write_log_line)
    else:
        log(f"gateway error {code}: {text}")


def _setup_daemon_logging(prog_name: str, config_dir) -> None:
    """Daemon mode only, and only when stdout is not a tty (the
    launchd case): point log() at <config_dir>/logs/<prog>.log —
    the same path install.sh gives launchd. Terminal runs keep
    printing to the terminal. A logging failure must never kill
    the daemon: fall back to plain stdout."""
    global _LOG_SINK
    if _LOG_SINK is not None:
        return
    try:
        if sys.stdout.isatty():
            return
    except Exception:
        pass
    try:
        _LOG_SINK = RotatingLogSink(Path(config_dir) / "logs" / f"{prog_name}.log")
    except OSError:
        _LOG_SINK = None


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


# Tags the account-updates stream (updateAccountValue) can fill when
# the account-summary sweep left them null on this login. Keyed by the
# SUMMARY tag name; values arrive from the stream under different key
# spellings — see STREAM_KEY_MAP. Verified live 2026-10-23: the plain
# PnL keys are never streamed for this account; the $LEDGER- variants
# are, and there is no plain SettledCash key at all (only the by-date
# breakdown in STREAM_SETTLED_CASH_KEY), so settled cash stays null
# rather than guessing at a substitute.
STREAM_FILL_TAGS = ("UnrealizedPnL", "RealizedPnL", "SettledCash")
STREAM_KEY_MAP = {
    "UnrealizedPnL": "UnrealizedPnL",
    "$LEDGER-UnrealizedPnL": "UnrealizedPnL",
    "RealizedPnL": "RealizedPnL",
    "$LEDGER-RealizedPnL": "RealizedPnL",
    "SettledCash": "SettledCash",
}
STREAM_SETTLED_CASH_KEY = "SettledCashByDate"  # "YYYYMMDD:amount,..."


def settled_cash_from_by_date(raw) -> float | None:
    """Parse the stream's SettledCashByDate value ("YYYYMMDD:amount"
    entries, comma-separated) into the latest date's settled cash.
    Returns None when the shape is not exactly as expected: a wrong
    settled-cash number is worse than a null."""
    entries = [e.strip() for e in str(raw or "").split(",") if e.strip()]
    if not entries:
        return None
    parsed = []
    for entry in entries:
        date, sep, amount = entry.partition(":")
        if not sep or len(date) != 8 or not date.isdigit():
            return None
        value = to_float(amount)
        if value is None:
            return None
        parsed.append((date, value))
    return max(parsed)[1]


def fold_stream_values(values_by_tag: dict, stream_values: dict) -> dict:
    """Sweep values win; the stream fills only the tags the sweep left
    as None (never overwrites a real sweep value)."""
    merged = dict(values_by_tag or {})
    for tag in STREAM_FILL_TAGS:
        if merged.get(tag) is None and (stream_values or {}).get(tag) is not None:
            merged[tag] = stream_values[tag]
    return merged


def contract_fields(contract) -> dict:
    """Flatten an ibapi Contract into a plain dict (no ibapi objects),
    so the position mapping stays pure and unit-testable."""
    return {
        "conId": getattr(contract, "conId", 0),
        "symbol": str(getattr(contract, "symbol", "") or ""),
        "localSymbol": str(getattr(contract, "localSymbol", "") or ""),
        "secType": str(getattr(contract, "secType", "") or ""),
        "lastTradeDateOrContractMonth": str(
            getattr(contract, "lastTradeDateOrContractMonth", "") or ""),
        "strike": getattr(contract, "strike", 0.0),
        "right": str(getattr(contract, "right", "") or ""),
        "multiplier": getattr(contract, "multiplier", ""),
        "currency": str(getattr(contract, "currency", "") or "USD"),
        "primaryExchange": str(getattr(contract, "primaryExchange", "")
                               or getattr(contract, "exchange", "") or ""),
    }


def broker_position(contract: dict, portfolio: dict) -> dict:
    """Map one updatePortfolio record (plain dicts in) to the Kubera
    broker-positions record. avg_cost is the RAW per-unit value IBKR
    reports (already per share / per contract unit); it is NOT scaled
    by the multiplier. Quantities pass through signed: shorts are
    negative."""
    c = contract or {}
    p = portfolio or {}
    sec_type = str(c.get("secType") or "")
    right = str(c.get("right") or "")
    strike = to_float(c.get("strike"))
    mult = to_float(c.get("multiplier"))
    return {
        "con_id": int(to_float(c.get("conId")) or 0),
        "symbol": str(c.get("symbol") or ""),
        "local_symbol": str(c.get("localSymbol") or ""),
        "sec_type": sec_type,
        "expiry": expiry_iso(c.get("lastTradeDateOrContractMonth")),
        "strike": strike if (strike and strike > 0) else None,
        "option_type": "Put" if right == "P" else "Call" if right == "C" else None,
        "multiplier": int(mult) if mult else 1,
        "currency": str(c.get("currency") or "USD"),
        "exchange": str(c.get("primaryExchange") or c.get("exchange") or ""),
        "quantity": to_float(p.get("position")) or 0.0,
        "avg_cost": to_float(p.get("averageCost")),
        "market_price": to_float(p.get("marketPrice")),
        "market_value": to_float(p.get("marketValue")),
        "unrealized_pnl": to_float(p.get("unrealizedPNL")),
        "realized_pnl": to_float(p.get("realizedPNL")),
    }


def positions_snapshot(account_id: str, label: str | None, captured_at: str,
                       positions: list[dict]) -> dict:
    return {
        "ingest_source": "setu",
        "account_id": account_id,
        "account_label": label,
        "captured_at": captured_at,
        "complete": True,
        "positions": positions,
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
        # Account-updates stream state: conId -> (contract fields, values)
        self.positions_raw: dict[int, tuple[dict, dict]] = {}
        # updateAccountValue stream: tag -> value (fills sweep nulls)
        self.stream_values: dict[str, float | None] = {}
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

            def updatePortfolio(self, contract, position, marketPrice, marketValue,
                                averageCost, unrealizedPNL, realizedPNL, accountName):
                if str(accountName) != outer.account:
                    return
                con_id = int(to_float(getattr(contract, "conId", 0)) or 0)
                outer.positions_raw[con_id] = (contract_fields(contract), {
                    "position": to_float(position),
                    "marketPrice": to_float(marketPrice),
                    "marketValue": to_float(marketValue),
                    "averageCost": to_float(averageCost),
                    "unrealizedPNL": to_float(unrealizedPNL),
                    "realizedPNL": to_float(realizedPNL),
                })

            def updateAccountValue(self, key, val, currency, accountName):
                if str(accountName) != outer.account:
                    return
                tag = STREAM_KEY_MAP.get(key)
                if tag:
                    outer.stream_values[tag] = to_float(val)
                elif key == STREAM_SETTLED_CASH_KEY:
                    parsed = settled_cash_from_by_date(val)
                    if parsed is not None:
                        outer.stream_values["SettledCash"] = parsed

            def connectionClosed(self):
                outer.connected_event.clear()

            def error(self, reqId, errorCode, errorString, advancedOrderRejectJson=""):
                # 2104/2106/2107/2158 are benign farm-status notices.
                if errorCode in (2104, 2106, 2107, 2158, 2119):
                    return
                log_gateway_error(errorCode, errorString)

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
        # Account-updates stream for the configured account only:
        # drives the positions feed and fills any account tags the
        # summary sweep leaves null.
        self.client.reqAccountUpdates(True, self.account)
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

    def mapped_positions(self) -> list[dict]:
        """Current portfolio from the account-updates stream, mapped
        to Kubera position records, sorted by con_id (stable order so
        the change hash is deterministic)."""
        return [broker_position(fields, values)
                for _con_id, (fields, values) in sorted(self.positions_raw.items())]

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
        self.last_positions_fp: str | None = None
        self.last_positions_push = 0.0
        self.positions_404_noted = False

    def account_payload(self) -> dict:
        values = fold_stream_values(
            {tag: pair[0] for tag, pair in self.gw.summary.items()},
            self.gw.stream_values)
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
        # Positions (account-updates stream)
        positions = self.gw.mapped_positions()
        if positions:
            pfp = hashlib.sha256(
                json.dumps(positions, sort_keys=True).encode("utf-8")).hexdigest()
            if pfp != self.last_positions_fp or now - self.last_positions_push >= self.heartbeat:
                ppayload = positions_snapshot(self.cfg["account_id"],
                                              self.cfg.get("account_label"),
                                              london_now_iso(), positions)
                ok, detail = self.push.post("/api/v1/broker-positions/snapshot", ppayload)
                if ok:
                    self.last_positions_fp, self.last_positions_push = pfp, now
                    self.positions_404_noted = False
                    log(f"positions push ({detail}): {len(positions)} positions")
                elif "404" in detail:
                    # Kubera side not deployed yet: say it once, keep
                    # retrying quietly on change/heartbeat, never let
                    # it disturb the accounts/orders publishing.
                    if not self.positions_404_noted:
                        log("positions endpoint not deployed yet")
                        self.positions_404_noted = True
                else:
                    log(f"positions push failed: {detail}")

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
    values = fold_stream_values(
        {tag: pair[0] for tag, pair in gw.summary.items()}, gw.stream_values)
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
    deadline = time.time() + 6
    while time.time() < deadline and not gw.positions_raw:
        time.sleep(0.5)
    positions = gw.mapped_positions()
    print(f"positions snapshot: {len(positions)} positions for {cfg['account_id']}")
    for p in positions[:10]:
        print(f"  {p['symbol']} {p['sec_type']} qty {p['quantity']} "
              f"avg {p['avg_cost']} mv {p['market_value']}")
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
    _setup_daemon_logging("setu", Path(args.config).resolve().parent)
    Setu(cfg).run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
