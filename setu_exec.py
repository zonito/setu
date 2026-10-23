#!/usr/bin/env python3
"""setu-exec — the guarded GTC executor that rides beside Setu.

Setu (setu.py) is the read-only bridge and stays that way: it has no
order calls at all. This is a SEPARATE program with its own Gateway
client id (91). It exists to do exactly one job from the placement
design (~/workspace/your_files/setu-gtc-placement-design.md): take
Kubera staged orders that Zonito has APPROVED — buy-to-close limit
orders for short options at the 60% take-profit price — revalidate
them live against the Gateway, and report the outcome back to Kubera.

Modes (executor.json next to the main config, default "simulate"):
  simulate — every gate runs, including the Gateway what-if preview,
             and the executor reports "would_place". It is incapable
             of transmitting: its only order call carries whatIf=True.
  live     — after the same gates, the same order is transmitted
             with whatIf=False. Live mode additionally requires the
             enable file executor.live next to the config; without
             it the executor refuses to start.

Safety rails, both modes, no exceptions:
  * Daily arm file (executor.armed, contents = today's Europe/London
    date). Yesterday's arm never works. `arm` writes it, `disarm`
    removes it.
  * Kubera kill switch, read fresh every cycle.
  * Gates, in order: live reads -> position real -> no duplicate ->
    price sane -> frozen order shape -> what-if margin preview.
  * One staged row can produce at most one live order, ever: the
    order carries orderRef = staged row id, and placed orders are
    tracked in executor-state.json across restarts and reconciled
    by that ref against live open orders before anything new runs.
  * No quantity caps (dropped at Zonito's instruction, 2026-10-23).

Config: account, Kubera and Gateway settings come from the same
~/.setu/config.json Setu uses (read, never written by this program).
Mode and executor knobs live in executor.json beside it. The ingest
token is read from the configured token file and never logged.

CLI:
  setu_exec.py [--config PATH] arm | disarm | enable-live |
               disable-live | once | run
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

VERSION = "0.1.0"
LONDON = ZoneInfo("Europe/London")
CLIENT_ID = 91
WHATIF_MARGIN_TOLERANCE = 50.0  # account currency; closing must not add margin
TERMINAL_STATUSES = {"Filled", "Cancelled", "ApiCancelled", "Inactive"}
BENIGN_ERRORS = {2104, 2106, 2107, 2158, 2119}
UNSET = 1.0e100  # ibapi marks unset doubles with a huge sentinel


def log(msg: str) -> None:
    stamp = datetime.now(LONDON).strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{stamp}] {msg}", flush=True)


def to_float(value) -> float | None:
    try:
        if value is None or value == "":
            return None
        out = float(value)
    except (TypeError, ValueError):
        return None
    return None if abs(out) >= UNSET else out


def tick_round(price: float) -> float:
    """Round to the 0.01 option tick (half-up on the cent)."""
    return round(price * 100 + 1e-9) / 100.0


def london_today() -> str:
    return datetime.now(LONDON).strftime("%Y-%m-%d")


# ---------------------------------------------------------------------------
# Pure gate logic (unit-tested in tests/test_exec_gates.py)
#
# A staged row is a plain dict: id, con_id, symbol, expiry (YYYY-MM-DD),
# strike, option_type ("Put"/"Call"), quantity (positive, contracts to
# close), premium (PER-UNIT premium received — the same units as an
# option limit price), target_limit.
# A live position is a plain dict: con_id, quantity (signed, shorts
# negative), avg_cost (per-unit; the Gateway reports option average
# cost per contract, and the reader below divides by the multiplier).
# A live open order is a plain dict: con_id, action ("BUY"/"SELL"),
# limit_price, order_ref, status.
# ---------------------------------------------------------------------------

def gate_position(row: dict, position: dict | None) -> tuple[bool, dict]:
    """G3: the short must still be there, at least as large as asked.
    A smaller live short closes what exists (flagged), never more."""
    if position is None:
        return False, {"gate": "position",
                       "reason": f"no live position for con_id {row['con_id']}"}
    qty = to_float(position.get("quantity")) or 0.0
    if qty >= 0:
        return False, {"gate": "position",
                       "reason": f"position for con_id {row['con_id']} is not short (qty {qty})"}
    available = -qty
    effective = min(float(row["quantity"]), available)
    if effective <= 0:
        return False, {"gate": "position",
                       "reason": f"nothing left to close for con_id {row['con_id']}"}
    return True, {"effective_qty": effective,
                  "position_qty": qty,
                  "reduced": effective < float(row["quantity"])}


def gate_duplicate(row: dict, open_orders: list[dict], limit: float) -> tuple[bool, dict]:
    """G4: never a second working BUY on this contract at ANY price,
    and never anything carrying this row's orderRef. One short position
    carries exactly one resting buy-to-close in this system; a portal
    GTC placed by hand counts, whatever its price (strengthened
    2026-10-23 after the first live rehearsal: every short in
    U19377323 already had a hand-placed GTC at a non-40% price, which
    a same-price check would have sailed past)."""
    ref = str(row["id"])
    for o in open_orders or []:
        if str(o.get("order_ref") or "") == ref:
            return False, {"gate": "duplicate",
                           "reason": f"an open order already carries orderRef {ref}"}
        if (int(o.get("con_id") or 0) == int(row["con_id"])
                and str(o.get("action") or "") == "BUY"):
            return False, {"gate": "duplicate",
                           "reason": f"working BUY on con_id {row['con_id']} "
                                     f"already exists at {o.get('limit_price')} "
                                     f"(staged target was {limit}); replacing a "
                                     f"resting order is a human decision"}
    return True, {}


def gate_price(row: dict, position: dict | None) -> tuple[bool, dict]:
    """G5: the staged target must equal 0.40 x premium tick-rounded,
    and the staged premium must sit within 10% of the live position's
    per-unit average cost (multi-entry averages drift a little)."""
    premium = to_float(row.get("premium"))
    target = to_float(row.get("target_limit"))
    if premium is None or premium <= 0 or target is None:
        return False, {"gate": "price",
                       "reason": "row is missing a usable premium or target_limit"}
    computed = tick_round(0.40 * premium)
    if abs(computed - target) > 0.01:
        return False, {"gate": "price",
                       "reason": f"target_limit {target} != 0.40 x premium "
                                 f"({computed}) within a tick"}
    avg_cost = to_float((position or {}).get("avg_cost"))
    if avg_cost is not None and avg_cost > 0:
        drift = abs(premium - avg_cost) / avg_cost
        if drift > 0.10:
            return False, {"gate": "price",
                           "reason": f"premium {premium} drifts {drift:.0%} from "
                                     f"live avg cost {avg_cost} (> 10%)"}
    return True, {"limit": computed}


def evaluate_gates(row: dict, position: dict | None,
                   open_orders: list[dict]) -> tuple[bool, dict]:
    """G3 -> G4 -> G5. On success the detail carries effective_qty,
    limit and the reduced flag; on failure, gate + reason."""
    ok, detail = gate_position(row, position)
    if not ok:
        return False, detail
    eff_qty = detail["effective_qty"]
    ok, dup = gate_duplicate(row, open_orders, to_float(row.get("target_limit")) or 0.0)
    if not ok:
        return False, dup
    ok, price = gate_price(row, position)
    if not ok:
        return False, price
    return True, {"effective_qty": eff_qty,
                  "limit": price["limit"],
                  "reduced": detail["reduced"],
                  "position_qty": detail["position_qty"]}


def gate_whatif(reply: dict | None) -> tuple[bool, dict]:
    """G6 verdict on a what-if reply dict: the preview must succeed
    and must not materially increase initial margin."""
    if not reply or not reply.get("ok"):
        return False, {"gate": "whatif",
                       "reason": f"what-if failed: {(reply or {}).get('error') or 'no reply'}"}
    change = to_float(reply.get("init_margin_change"))
    if change is not None and change > WHATIF_MARGIN_TOLERANCE:
        return False, {"gate": "whatif",
                       "reason": f"what-if init margin change {change} exceeds "
                                 f"+{WHATIF_MARGIN_TOLERANCE} tolerance"}
    return True, {"whatif": reply}


def build_order_spec(row: dict, effective_qty: float, limit: float,
                     what_if: bool) -> dict:
    """G7, the frozen shape — the ONLY order this program can build:
    BUY / LMT / GTC / regular hours / single OPT leg, SMART/USD,
    orderRef = staged row id. `what_if` is the single field that
    differs between the simulate preview and the live transmit."""
    right = "P" if str(row.get("option_type")) == "Put" else "C"
    expiry = str(row.get("expiry") or "").replace("-", "")
    return {
        "contract": {
            "conId": int(row["con_id"]),
            "symbol": str(row.get("symbol") or ""),
            "secType": "OPT",
            "exchange": "SMART",
            "currency": "USD",
            "lastTradeDateOrContractMonth": expiry,
            "strike": float(row.get("strike") or 0.0),
            "right": right,
            "multiplier": "100",
        },
        "order": {
            "action": "BUY",
            "orderType": "LMT",
            "totalQuantity": float(effective_qty),
            "lmtPrice": float(limit),
            "tif": "GTC",
            "outsideRth": False,
            "orderRef": str(row["id"]),
            "whatIf": bool(what_if),
            "transmit": True,
        },
    }


def order_line(row: dict, qty: float, limit: float) -> str:
    pc = "P" if str(row.get("option_type")) == "Put" else "C"
    strike = row.get("strike")
    strike_s = f"{strike:g}" if isinstance(strike, (int, float)) else str(strike)
    return (f"{row.get('symbol')} {row.get('expiry')} {strike_s}{pc} "
            f"BUY {qty:g} @ {limit:.2f} GTC (row {row.get('id')})")


# ---------------------------------------------------------------------------
# Arm / live-enable files (siblings of the main config file)
# ---------------------------------------------------------------------------

def arm_path(cfg: dict) -> Path:
    return Path(cfg["_config_dir"]) / "executor.armed"


def live_path(cfg: dict) -> Path:
    return Path(cfg["_config_dir"]) / "executor.live"


def state_path(cfg: dict) -> Path:
    return Path(cfg["_config_dir"]) / "executor-state.json"


def is_armed(cfg: dict) -> bool:
    try:
        return arm_path(cfg).read_text(encoding="utf-8").strip() == london_today()
    except OSError:
        return False


def live_enabled(cfg: dict) -> bool:
    return live_path(cfg).exists()


def resolve_mode(cfg: dict) -> tuple[str, str | None]:
    """Returns (mode, refusal). Live mode without the enable file is
    a refusal, never a silent downgrade to simulate."""
    mode = str(cfg.get("mode") or "simulate").strip().lower()
    if mode not in ("simulate", "live"):
        return "simulate", f"unknown executor mode {mode!r}; refusing to start"
    if mode == "live" and not live_enabled(cfg):
        return mode, ("mode is 'live' but the enable file "
                      f"{live_path(cfg)} does not exist; refusing to start. "
                      "Create it (setu_exec.py enable-live) or set mode "
                      "back to 'simulate'.")
    return mode, None


# ---------------------------------------------------------------------------
# Placed-order tracking (persisted; reconciled by orderRef on restart)
# ---------------------------------------------------------------------------

class PlacedTracker:
    def __init__(self, cfg: dict):
        self.path = state_path(cfg)
        self.orders: dict[str, dict] = {}
        self.load()

    def load(self) -> None:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(data, dict) and isinstance(data.get("placed"), dict):
                self.orders = data["placed"]
        except (OSError, ValueError):
            self.orders = {}

    def save(self) -> None:
        try:
            self.path.write_text(
                json.dumps({"placed": self.orders}, indent=2), encoding="utf-8")
        except OSError as exc:
            log(f"executor state save failed: {exc}")

    def add(self, row_id: str, record: dict) -> None:
        self.orders[str(row_id)] = record
        self.save()

    def update(self, row_id: str, **fields) -> None:
        rec = self.orders.get(str(row_id))
        if rec is not None:
            rec.update(fields)
            self.save()

    def drop(self, row_id: str) -> None:
        if self.orders.pop(str(row_id), None) is not None:
            self.save()

    def get(self, row_id: str) -> dict | None:
        return self.orders.get(str(row_id))


# ---------------------------------------------------------------------------
# Kubera staged-orders client (contract: kubera-staged-orders-brief.md)
# ---------------------------------------------------------------------------

class KuberaError(Exception):
    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


class KuberaClient:
    def __init__(self, cfg: dict):
        self.base = str(cfg["kubera_base"]).rstrip("/")
        self.account = str(cfg["account_id"])
        token_path = Path(os.path.expanduser(str(cfg["token_file"])))
        self.token = token_path.read_text(encoding="utf-8").strip()
        proxy = cfg.get("http_proxy")
        if proxy:
            self.opener = urllib.request.build_opener(
                urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
        else:
            self.opener = urllib.request.build_opener()

    def _request(self, method: str, path: str, payload: dict | None = None) -> dict:
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        req = urllib.request.Request(
            self.base + path, data=data, method=method,
            headers={"Content-Type": "application/json",
                     "X-Kubera-Ingest-Token": self.token})
        try:
            with self.opener.open(req, timeout=15) as resp:
                return json.loads(resp.read().decode("utf-8") or "{}")
        except urllib.error.HTTPError as exc:
            raise KuberaError(f"HTTP {exc.code} on {method} {path}",
                              status=exc.code) from exc
        except Exception as exc:
            raise KuberaError(f"{type(exc).__name__} on {method} {path}: {exc}") from exc

    def get_control(self) -> dict:
        return self._request(
            "GET", f"/api/v1/broker-staged-orders/control?account={self.account}")

    def poll_approved(self) -> list[dict]:
        body = self._request(
            "GET", f"/api/v1/broker-staged-orders?account={self.account}&state=APPROVED")
        rows = None
        if isinstance(body, list):
            rows = body
        elif isinstance(body, dict):
            for key in ("orders", "rows", "staged_orders"):
                if isinstance(body.get(key), list):
                    rows = body[key]
                    break
        return [normalise_row(r) for r in (rows or [])]

    def post_event(self, row_id: str, kind: str, detail: dict) -> dict:
        return self._request(
            "POST", f"/api/v1/broker-staged-orders/{row_id}/event",
            {"kind": kind, "detail": detail})


def normalise_row(raw: dict) -> dict:
    return {
        "id": str(raw.get("id")),
        "account_id": str(raw.get("account_id") or ""),
        "con_id": int(to_float(raw.get("con_id")) or 0),
        "symbol": str(raw.get("symbol") or ""),
        "local_symbol": str(raw.get("local_symbol") or ""),
        "expiry": str(raw.get("expiry") or ""),
        "strike": to_float(raw.get("strike")) or 0.0,
        "option_type": str(raw.get("option_type") or ""),
        "quantity": to_float(raw.get("quantity")) or 0.0,
        "premium": to_float(raw.get("premium")),
        "target_limit": to_float(raw.get("target_limit")),
    }


# ---------------------------------------------------------------------------
# Gateway client (own client id; the ONLY place ibapi order calls exist)
# ---------------------------------------------------------------------------

class ExecGateway:
    def __init__(self, cfg: dict):
        from ibapi.client import EClient
        from ibapi.wrapper import EWrapper

        self.cfg = cfg
        self.account = str(cfg["account_id"])
        self.connected_event = threading.Event()
        self.positions: dict[int, dict] = {}
        self.positions_event = threading.Event()
        self.open_orders: list[dict] = []
        self.status_by_key: dict = {}
        self.last_status_by_perm: dict[int, str] = {}
        self.open_orders_event = threading.Event()
        self.next_id = 0
        # orderId -> {"event": Event, "order_state": OrderState|None,
        #             "status": str, "perm_id": int, "error": str|None}
        self.pending: dict[int, dict] = {}
        outer = self

        class Wrapper(EWrapper):
            def nextValidId(self, orderId):
                outer.next_id = int(orderId)
                outer.connected_event.set()

            def position(self, account, contract, pos, avgCost):
                if str(account) != outer.account:
                    return
                con_id = int(to_float(getattr(contract, "conId", 0)) or 0)
                mult = to_float(getattr(contract, "multiplier", "")) or 1.0
                if str(getattr(contract, "secType", "")) in ("OPT", "FOP") and mult <= 1:
                    mult = 100.0  # US equity options; field can arrive blank
                # Gateway reports option average cost per CONTRACT;
                # gates compare per-unit, so normalise here.
                avg = to_float(avgCost)
                if avg is not None and mult > 1:
                    avg = avg / mult
                outer.positions[con_id] = {
                    "con_id": con_id,
                    "symbol": str(getattr(contract, "symbol", "") or ""),
                    "quantity": to_float(pos) or 0.0,
                    "avg_cost": avg,
                }

            def positionEnd(self):
                outer.positions_event.set()

            def openOrder(self, orderId, contract, order, orderState):
                acct = str(getattr(order, "account", "") or "")
                if acct and acct != outer.account:
                    return
                perm = int(to_float(getattr(order, "permId", 0)) or 0)
                key = ("p", perm) if perm else ("i", int(orderId))
                lmt = to_float(getattr(order, "lmtPrice", None))
                outer.open_orders.append({
                    "con_id": int(to_float(getattr(contract, "conId", 0)) or 0),
                    "action": str(getattr(order, "action", "") or ""),
                    "limit_price": lmt if (lmt is not None and lmt < UNSET) else None,
                    "order_ref": str(getattr(order, "orderRef", "") or ""),
                    "status": str(getattr(orderState, "status", "") or ""),
                    "perm_id": perm,
                    "_key": key,
                })
                slot = outer.pending.get(int(orderId))
                if slot is not None:
                    slot["order_state"] = orderState
                    slot["perm_id"] = perm
                    if getattr(orderState, "status", ""):
                        slot["status"] = str(orderState.status)
                    slot["event"].set()

            def openOrderEnd(self):
                outer.open_orders_event.set()

            def orderStatus(self, orderId, status, filled, remaining, avgFillPrice,
                            permId=0, parentId=0, lastFillPrice=0.0, clientId=0,
                            whyHeld="", mktCapPrice=0.0):
                key = ("p", int(permId)) if permId else ("i", int(orderId))
                outer.status_by_key[key] = str(status or "")
                slot = outer.pending.get(int(orderId))
                if slot is not None:
                    slot["status"] = str(status or "")
                    if permId:
                        slot["perm_id"] = int(permId)
                    slot["event"].set()

            def error(self, reqId, errorCode, errorString, advancedOrderRejectJson=""):
                if errorCode in BENIGN_ERRORS:
                    return
                slot = outer.pending.get(int(reqId)) if isinstance(reqId, int) else None
                if slot is not None:
                    slot["error"] = f"IB error {errorCode}: {errorString}"
                    slot["event"].set()
                else:
                    log(f"gateway error {errorCode}: {errorString}")

            def connectionClosed(self):
                outer.connected_event.clear()

        class Client(EClient):
            pass

        self.wrapper = Wrapper()
        self.client = Client(self.wrapper)

    # -- lifecycle ------------------------------------------------------
    def connect(self) -> bool:
        self.connected_event.clear()
        try:
            self.client.connect(self.cfg["gateway_host"], int(self.cfg["gateway_port"]),
                                int(self.cfg["exec_client_id"]))
        except Exception as exc:
            log(f"gateway connect raised ({type(exc).__name__}: {exc})")
            return False
        self.thread = threading.Thread(target=self.client.run, daemon=True)
        self.thread.start()
        return self.connected_event.wait(timeout=15)

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

    # -- live reads (G2: always fresh from the Gateway, never Kubera) ---
    def fetch_positions(self, timeout: float = 20.0) -> dict[int, dict] | None:
        self.positions = {}
        self.positions_event.clear()
        self.client.reqPositions()
        try:
            if not self.positions_event.wait(timeout=timeout):
                return None
            return dict(self.positions)
        finally:
            try:
                self.client.cancelPositions()
            except Exception:
                pass

    def fetch_open_orders(self, timeout: float = 20.0) -> list[dict] | None:
        self.open_orders = []
        self.status_by_key = {}
        self.open_orders_event.clear()
        self.client.reqAllOpenOrders()
        if not self.open_orders_event.wait(timeout=timeout):
            return None
        self.last_status_by_perm = {
            int(k[1]): v for k, v in self.status_by_key.items() if k[0] == "p"}
        out = []
        for o in self.open_orders:
            status = self.status_by_key.get(o.pop("_key"), None)
            if status:
                o["status"] = status
            out.append(o)
        return out

    # -- order calls ------------------------------------------------------
    def _ib_objects(self, spec: dict):
        from ibapi.contract import Contract
        from ibapi.order import Order
        cs, os_ = spec["contract"], spec["order"]
        contract = Contract()
        contract.conId = int(cs["conId"])
        contract.symbol = cs["symbol"]
        contract.secType = cs["secType"]
        contract.exchange = cs["exchange"]
        contract.currency = cs["currency"]
        contract.lastTradeDateOrContractMonth = cs["lastTradeDateOrContractMonth"]
        contract.strike = float(cs["strike"])
        contract.right = cs["right"]
        contract.multiplier = cs["multiplier"]
        order = Order()
        order.account = self.account
        order.action = os_["action"]
        order.orderType = os_["orderType"]
        order.totalQuantity = float(os_["totalQuantity"])
        order.lmtPrice = float(os_["lmtPrice"])
        order.tif = os_["tif"]
        order.outsideRth = bool(os_["outsideRth"])
        order.orderRef = os_["orderRef"]
        order.transmit = bool(os_["transmit"])
        order.whatIf = bool(os_["whatIf"])
        # ibapi 9.81 defaults eTradeOnly/firmQuoteOnly to True and always
        # sends them; current IBKR servers reject eTradeOnly outright
        # (error 10268, proven live 2026-10-23). Never wanted here.
        order.eTradeOnly = False
        order.firmQuoteOnly = False
        return contract, order

    def _await_slot(self, order_id: int, timeout: float) -> dict:
        slot = self.pending[order_id]
        slot["event"].wait(timeout=timeout)
        return slot

    def whatif(self, spec: dict) -> dict:
        """The simulate ceiling: a what-if preview. Asserts the order
        carries whatIf=True before the call — a real order can never
        leave through this door."""
        contract, order = self._ib_objects(spec)
        if not order.whatIf:
            raise AssertionError("whatif() refuses an order without whatIf=True")
        order_id = self._next_order_id()
        self.pending[order_id] = {"event": threading.Event(), "order_state": None,
                                  "status": "", "perm_id": 0, "error": None}
        try:
            self.client.placeOrder(order_id, contract, order)
            slot = self._await_slot(order_id, timeout=12.0)
        finally:
            self.pending.pop(order_id, None)
        if slot["error"]:
            return {"ok": False, "error": slot["error"]}
        state = slot["order_state"]
        if state is None:
            return {"ok": False, "error": "no what-if reply from the Gateway"}
        return {
            "ok": True,
            "init_margin_change": to_float(getattr(state, "initMarginChange", "")),
            "maint_margin_change": to_float(getattr(state, "maintMarginChange", "")),
            "equity_with_loan_change": to_float(getattr(state, "equityWithLoanChange", "")),
            "commission": to_float(getattr(state, "commission", "")),
        }

    def transmit(self, spec: dict) -> dict:
        """Live placement. Only reachable in live mode with the enable
        file present (the Executor checks before calling); the order
        must carry whatIf=False by construction of the caller."""
        contract, order = self._ib_objects(spec)
        if order.whatIf:
            raise AssertionError("transmit() refuses a whatIf order")
        order_id = self._next_order_id()
        self.pending[order_id] = {"event": threading.Event(), "order_state": None,
                                  "status": "", "perm_id": 0, "error": None}
        try:
            self.client.placeOrder(order_id, contract, order)
            slot = self._await_slot(order_id, timeout=12.0)
        finally:
            self.pending.pop(order_id, None)
        return {"order_id": order_id, "perm_id": slot["perm_id"],
                "status": slot["status"], "error": slot["error"]}

    def _next_order_id(self) -> int:
        oid = self.next_id
        self.next_id += 1
        return oid


# ---------------------------------------------------------------------------
# Executor
# ---------------------------------------------------------------------------

class Executor:
    def __init__(self, cfg: dict, kubera: KuberaClient, gateway,
                 tracker: PlacedTracker | None = None):
        self.cfg = cfg
        self.account = str(cfg["account_id"])
        self.mode, self.mode_refusal = resolve_mode(cfg)
        self.kubera = kubera
        self.gateway = gateway
        self.tracker = tracker if tracker is not None else PlacedTracker(cfg)
        self._noted: set[str] = set()

    def _note_once(self, key: str, msg: str) -> None:
        if key not in self._noted:
            log(msg)
            self._noted.add(key)

    # -- one poll cycle ---------------------------------------------------
    def cycle(self) -> None:
        if self.mode_refusal:
            log(f"refusing to run: {self.mode_refusal}")
            return
        if not is_armed(self.cfg):
            self._note_once("arm", "not armed for today; idling "
                                   "(setu_exec.py arm to arm)")
            return
        self._noted.discard("arm")
        try:
            control = self.kubera.get_control()
        except KuberaError as exc:
            if exc.status == 404:
                self._note_once("kubera404", "staged-orders endpoints not "
                                             "deployed in Kubera yet; idling")
            else:
                self._note_once("kubera", f"Kubera unreachable ({exc}); idling")
            return
        self._noted.discard("kubera404")
        self._noted.discard("kubera")
        if control.get("kill_switch"):
            self._note_once("kill", "kill switch is ON in Kubera; idling")
            return
        self._noted.discard("kill")

        positions = self.gateway.fetch_positions()
        open_orders = self.gateway.fetch_open_orders()
        if positions is None or open_orders is None:
            log("live Gateway reads timed out; skipping this cycle")
            return

        self.reconcile(open_orders)

        try:
            rows = self.kubera.poll_approved()
        except KuberaError as exc:
            log(f"approved-orders poll failed ({exc}); idling")
            return
        for row in rows:
            if self.tracker.get(row["id"]) is not None:
                continue  # already placed once; never again
            self.process_row(row, positions, open_orders)

    # -- placed-order tracking ---------------------------------------------
    def reconcile(self, open_orders: list[dict]) -> None:
        """Before anything new: every tracked order is matched by
        orderRef against live open orders. Found = still working.
        Missing with a terminal callback status = report it. Missing
        with no word = the order vanished; surface it, never hide it."""
        if not self.tracker.orders:
            return
        live_refs = {str(o.get("order_ref") or ""): o for o in open_orders}
        sweep_status = getattr(self.gateway, "last_status_by_perm", {}) or {}
        for row_id, rec in list(self.tracker.orders.items()):
            live = live_refs.get(str(row_id))
            if live is not None:
                fields = {}
                if not rec.get("perm_id") and live.get("perm_id"):
                    fields["perm_id"] = live["perm_id"]
                if live.get("status"):
                    fields["last_status"] = live["status"]
                if fields:
                    self.tracker.update(row_id, **fields)
                continue
            # Not in open orders: the latest sweep's orderStatus map may
            # have caught a terminal transition for this perm id.
            if rec.get("perm_id") and sweep_status.get(int(rec["perm_id"])):
                seen = sweep_status[int(rec["perm_id"])]
                if seen != rec.get("last_status"):
                    self.tracker.update(row_id, last_status=seen)
                    rec = {**rec, "last_status": seen}
            status = str(rec.get("last_status") or "")
            if status == "Filled":
                self._report(row_id, "filled", {"broker_order_id": rec.get("broker_order_id"),
                                                "perm_id": rec.get("perm_id")})
                self.tracker.drop(row_id)
            elif status in ("Cancelled", "ApiCancelled"):
                self._report(row_id, "cancelled", {"broker_order_id": rec.get("broker_order_id"),
                                                   "perm_id": rec.get("perm_id")})
                self.tracker.drop(row_id)
            elif status == "Inactive":
                self._report(row_id, "rejected", {"broker_order_id": rec.get("broker_order_id"),
                                                  "perm_id": rec.get("perm_id"),
                                                  "reason": "order went Inactive at IB"})
                self.tracker.drop(row_id)
            elif rec.get("confirmed"):
                self._report(row_id, "failed",
                             {"reason": "placed order not found in live open orders "
                                        "and no terminal status seen; verify in the Gateway",
                              "broker_order_id": rec.get("broker_order_id"),
                              "perm_id": rec.get("perm_id")})
                self.tracker.drop(row_id)
            # unconfirmed + missing: leave for the next cycle's reads

    def _report(self, row_id: str, kind: str, detail: dict) -> None:
        try:
            self.kubera.post_event(row_id, kind, detail)
        except KuberaError as exc:
            log(f"event post failed for row {row_id} ({kind}): {exc}")

    # -- one staged row -------------------------------------------------------
    def process_row(self, row: dict, positions: dict[int, dict],
                    open_orders: list[dict]) -> None:
        self._report(row["id"], "placing", {"mode": self.mode})
        position = positions.get(int(row["con_id"]))
        ok, detail = evaluate_gates(row, position, open_orders)
        if not ok:
            log(f"GATE FAILED [{detail['gate']}] {order_line(row, row['quantity'], row['target_limit'] or 0)}"
                f" — {detail['reason']}")
            self._report(row["id"], "gate_failed", detail)
            return
        qty, limit = detail["effective_qty"], detail["limit"]
        line = order_line(row, qty, limit)
        if detail["reduced"]:
            log(f"note: live short is smaller than staged; closing {qty:g} "
                f"of {row['quantity']:g} for row {row['id']}")

        preview = build_order_spec(row, qty, limit, what_if=True)
        try:
            reply = self.gateway.whatif(preview)
        except Exception as exc:
            reply = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        ok, detail = gate_whatif(reply)
        if not ok:
            log(f"GATE FAILED [whatif] {line} — {detail['reason']}")
            self._report(row["id"], "gate_failed", detail)
            return

        if self.mode == "simulate":
            log(f"WOULD PLACE {line}")
            self._report(row["id"], "would_place", {
                "order": preview, "whatif": detail["whatif"],
                "effective_qty": qty})
            return

        # Live mode: gates all passed, enable file verified at start.
        spec = build_order_spec(row, qty, limit, what_if=False)
        # Record BEFORE transmitting so a crash mid-placement leaves a
        # trace the restart reconciliation can find by orderRef.
        self.tracker.add(row["id"], {
            "order_ref": str(row["id"]), "account": self.account,
            "confirmed": False, "broker_order_id": None, "perm_id": None,
            "last_status": "", "placed_at": datetime.now(LONDON).isoformat(timespec="seconds"),
            "order": spec})
        try:
            result = self.gateway.transmit(spec)
        except Exception as exc:
            result = {"order_id": None, "perm_id": 0, "status": "",
                      "error": f"{type(exc).__name__}: {exc}"}
        if result.get("error"):
            log(f"PLACEMENT REJECTED {line} — {result['error']}")
            self._report(row["id"], "rejected", {"reason": result["error"], "order": spec})
            self.tracker.drop(row["id"])
            return
        if not result.get("status") and not result.get("perm_id"):
            # Transmitted but unconfirmed: keep the trace; the restart /
            # next-cycle reconciliation by orderRef resolves it. Never
            # a blind retry.
            log(f"PLACED (unconfirmed) {line} — no Gateway reply yet; tracking by orderRef")
            return
        self.tracker.update(row["id"], confirmed=True,
                            broker_order_id=result.get("order_id"),
                            perm_id=result.get("perm_id") or None,
                            last_status=result.get("status") or "")
        log(f"PLACED {line} — order {result.get('order_id')} perm {result.get('perm_id')}")
        self._report(row["id"], "placed", {
            "broker_order_id": result.get("order_id"),
            "perm_id": result.get("perm_id"), "order": spec})

    # -- runners ---------------------------------------------------------------
    def run_forever(self) -> int:
        if self.mode_refusal:
            log(f"refusing to start: {self.mode_refusal}")
            return 2
        poll = float(self.cfg.get("exec_poll_seconds", 30))
        backoff = 5.0
        log(f"setu-exec {VERSION} starting: account {self.account}, mode {self.mode}, "
            f"gateway {self.cfg['gateway_host']}:{self.cfg['gateway_port']}, "
            f"kubera {self.cfg['kubera_base']}")
        while True:
            if not self.gateway.is_connected():
                self.gateway.disconnect()
                if self.gateway.connect():
                    backoff = 5.0
                    log("connected to gateway")
                else:
                    log(f"gateway connect failed; retrying in {backoff:.0f}s")
                    time.sleep(backoff)
                    backoff = min(backoff * 2, 60.0)
                    continue
            try:
                self.cycle()
            except Exception as exc:
                log(f"cycle error ({type(exc).__name__}: {exc}); reconnecting")
                self.gateway.disconnect()
                continue
            time.sleep(poll)

    def run_once(self) -> int:
        if self.mode_refusal:
            log(f"refusing to start: {self.mode_refusal}")
            return 2
        if not self.gateway.connect():
            log("could not connect to the gateway")
            return 1
        try:
            self.cycle()
        finally:
            self.gateway.disconnect()
        return 0


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

def load_config(path: str) -> dict:
    cfg_path = Path(path)
    cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    cfg["_config_dir"] = str(cfg_path.resolve().parent)
    # Executor knobs live beside the main config so Setu's file is
    # never touched: executor.json {"mode": "simulate", ...}
    exec_path = cfg_path.resolve().parent / "executor.json"
    if exec_path.exists():
        cfg.update(json.loads(exec_path.read_text(encoding="utf-8")))
    cfg.setdefault("gateway_host", cfg.get("ib_host", "127.0.0.1"))
    cfg.setdefault("gateway_port", cfg.get("ib_port", 4001))
    cfg.setdefault("exec_client_id", CLIENT_ID)
    cfg.setdefault("kubera_base", "http://127.0.0.1:8000")
    cfg.setdefault("token_file", "~/.kubera-ingest-token")
    cfg.setdefault("mode", "simulate")
    cfg.setdefault("exec_poll_seconds", 30)
    if not cfg.get("account_id"):
        raise SystemExit("config is missing account_id")
    return cfg


def main() -> int:
    parser = argparse.ArgumentParser(
        description="setu-exec — guarded GTC executor (simulate by default)")
    parser.add_argument("--config", default=os.path.expanduser("~/.setu/config.json"))
    parser.add_argument("command", choices=("arm", "disarm", "enable-live",
                                            "disable-live", "once", "run"))
    args = parser.parse_args()
    cfg = load_config(args.config)

    if args.command == "arm":
        arm_path(cfg).write_text(london_today() + "\n", encoding="utf-8")
        print(f"armed for {london_today()} (Europe/London). "
              "Arming never carries overnight.")
        return 0
    if args.command == "disarm":
        arm_path(cfg).unlink(missing_ok=True)
        print("disarmed.")
        return 0
    if args.command == "enable-live":
        live_path(cfg).write_text("live enabled " + london_today() + "\n",
                                  encoding="utf-8")
        print(f"live enable file written: {live_path(cfg)}. "
              "Live mode also needs mode=live in executor.json, a daily "
              "arm, and the Kubera kill switch off.")
        return 0
    if args.command == "disable-live":
        live_path(cfg).unlink(missing_ok=True)
        print("live enable file removed; live mode will refuse to start.")
        return 0

    kubera = KuberaClient(cfg)
    gateway = ExecGateway(cfg)
    executor = Executor(cfg, kubera, gateway)
    if args.command == "once":
        return executor.run_once()
    return executor.run_forever()


if __name__ == "__main__":
    sys.exit(main())
