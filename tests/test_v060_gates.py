"""Tests for setu-exec v0.6: sell_shares rows, share-parent
wing_open rows (parent_kind="shares") and cc_open rows staged
with the unresolved con_id sentinel (con_id 0).

Synthetic data only, with fake Kubera/Gateway doubles in the
same style as tests/test_close_wing_gates.py. No real Gateway,
no real orders.

Run: python3 -m unittest discover tests
"""
import copy
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import setu_exec  # noqa: E402

IONQ_STK = 517593749     # IONQ stock (the rehearsal con_id)
IREN_STK = 526906130     # IREN stock (the rehearsal parent)
WING_CON_ID = 815002     # resolved IREN 2027-01-15 20P (placeholder)
CC_CON_ID = 932592066    # MRVL 2026-11-27 295C
MRVL_STK = 37040         # MRVL stock (placeholder)

SELL_ROW = {
    "id": "sell-row-1",
    "account_id": "U19377323",
    "con_id": IONQ_STK,
    "symbol": "IONQ",
    "local_symbol": "IONQ",
    "sec_type": "STK",
    "expiry": "",
    "strike": 0.0,
    "option_type": "",
    "quantity": 100.0,
    "premium": None,
    "target_limit": 40.00,
    "tif": "GTC",
    "action": "place",
    "kind": "sell_shares",
    "approval_mode": "manual",
    "source": "manual",
}

SHARE_WING_ROW = {
    "id": "share-wing-row-1",
    "account_id": "U19377323",
    "con_id": 0,
    "symbol": "IREN",
    "local_symbol": "",
    "expiry": "2027-01-15",
    "strike": 20.0,
    "option_type": "Put",
    "quantity": 18.0,
    "premium": None,
    "target_limit": 1.20,
    "tif": "GTC",
    "action": "place",
    "kind": "wing_open",
    "approval_mode": "manual",
    "source": "manual",
    "parent_kind": "shares",
    "parent_con_id": IREN_STK,
    "parent_symbol": "IREN",
    "parent_quantity": 3700.0,
}

CC_ROW = {
    "id": "cc-sentinel-row-1",
    "account_id": "U19377323",
    "con_id": 0,
    "symbol": "MRVL",
    "local_symbol": "",
    "expiry": "2026-11-27",
    "strike": 295.0,
    "option_type": "Call",
    "quantity": 4.0,
    "premium": None,
    "target_limit": 6.00,
    "action": "place",
    "kind": "cc_open",
    "approval_mode": "manual",
    "source": "manual",
}

STK_VERIFIED = {
    "con_id": IONQ_STK, "symbol": "IONQ", "sec_type": "STK",
    "expiry": "", "strike": None, "right": "", "multiplier": "",
    "min_tick": 0.01,
}

WING_VERIFIED = {
    "con_id": WING_CON_ID, "symbol": "IREN", "sec_type": "OPT",
    "expiry": "20270115", "strike": 20.0, "right": "P",
    "multiplier": "100", "min_tick": 0.01,
}

CC_VERIFIED = {
    "con_id": CC_CON_ID, "symbol": "MRVL", "sec_type": "OPT",
    "expiry": "20261127", "strike": 295.0, "right": "C",
    "multiplier": "100", "min_tick": 0.01,
}


def stock_position(con_id=IONQ_STK, symbol="IONQ", qty=200.0,
                   price=30.65):
    return {"con_id": con_id, "symbol": symbol, "sec_type": "STK",
            "right": "", "quantity": qty, "avg_cost": 32.05,
            "market_price": price}


def sell_order(qty=150.0, con_id=IONQ_STK, **over):
    order = {
        "con_id": con_id, "symbol": "IONQ", "expiry": "",
        "strike": None, "right": "", "action": "SELL",
        "total_quantity": qty, "limit_price": 41.00,
        "order_ref": "", "status": "Submitted", "perm_id": 717171,
        "order_id": 9902, "filled": 0.0, "remaining": qty,
    }
    order.update(over)
    return order


def make_cfg(tmp: str, mode="simulate", live_file=False) -> dict:
    cfg = {
        "_config_dir": tmp,
        "account_id": "U19377323",
        "kubera_base": "http://127.0.0.1:9",
        "token_file": str(Path(tmp) / "token"),
        "gateway_host": "127.0.0.1",
        "gateway_port": 4001,
        "exec_client_id": 91,
        "mode": mode,
        "exec_poll_seconds": 30,
    }
    Path(tmp, "token").write_text("test-token", encoding="utf-8")
    if live_file:
        live = Path(tmp, "executor.live")
        live.write_text("live\n", encoding="utf-8")
        os.chmod(live, 0o600)
    return cfg


def arm(cfg: dict) -> None:
    setu_exec.arm_path(cfg).write_text(setu_exec.london_today() + "\n",
                                       encoding="utf-8")


class FakeKubera:
    def __init__(self, rows=None, kill=False):
        self.rows = rows if rows is not None else [dict(SELL_ROW)]
        self.kill = kill
        self.events = []

    def get_control(self):
        return {"account_id": "U19377323", "kill_switch": self.kill}

    def poll_approved(self):
        return [dict(r) for r in self.rows]

    def post_event(self, row_id, kind, detail):
        self.events.append((row_id, kind, detail))
        return {"status": "success"}


class FakeGatewayV060:
    """Serves every v0.6 path: positions/open orders, contract
    details by con_id and by spec (calls recorded), the standard
    what-if, the cc conclusive what-if (calls recorded — the
    sell and wing paths must never use it) and transmit."""

    def __init__(self, positions="default", open_orders=None,
                 details="default", spec_details="default",
                 whatif_reply=None, whatif_cc_reply=None,
                 transmit_result=None):
        if positions == "default":
            positions = {IONQ_STK: stock_position()}
        self.positions_map = positions  # None means: reads fail
        self.open_orders = open_orders if open_orders is not None else []
        self.details = ([dict(STK_VERIFIED)] if details == "default"
                        else details)
        self.spec_details = ([dict(WING_VERIFIED)]
                             if spec_details == "default"
                             else spec_details)
        self.whatif_reply = whatif_reply or {
            "ok": True, "init_margin_change": 5.0,
            "maint_margin_change": 2.0, "equity_with_loan_change": 0.0,
            "commission": None}
        # A CONCLUSIVE cc reply (a concrete margin figure) so a
        # resolved sentinel row can pass the cc what-if gate.
        self.whatif_cc_reply = whatif_cc_reply or {
            "ok": True, "init_margin_change": -120.0,
            "maint_margin_change": -60.0, "equity_with_loan_change": 0.0,
            "commission": None}
        self.transmit_result = transmit_result or {
            "order_id": 6001, "perm_id": 888, "status": "PreSubmitted",
            "error": None, "avg_fill_price": None}
        self.last_status_by_perm = {}
        self.last_fill_by_perm = {}
        self.whatif_specs = []
        self.whatif_cc_specs = []
        self.transmit_specs = []
        self.details_calls = []
        self.spec_calls = []

    def fetch_positions(self):
        if self.positions_map is None:
            return None
        return {k: dict(v) for k, v in self.positions_map.items()}

    def fetch_open_orders(self):
        return [dict(o) for o in self.open_orders]

    def fetch_contract_details(self, con_id):
        self.details_calls.append(con_id)
        if self.details is None:
            return None
        return [dict(d) for d in self.details]

    def fetch_contract_details_by_spec(self, symbol, expiry, strike,
                                       right):
        self.spec_calls.append((symbol, expiry, strike, right))
        if self.spec_details is None:
            return None
        return [dict(d) for d in self.spec_details]

    def whatif(self, spec):
        self.whatif_specs.append(copy.deepcopy(spec))
        return dict(self.whatif_reply)

    def whatif_cc(self, spec):
        self.whatif_cc_specs.append(copy.deepcopy(spec))
        return dict(self.whatif_cc_reply)

    def transmit(self, spec):
        self.transmit_specs.append(copy.deepcopy(spec))
        return dict(self.transmit_result)


def make_executor(tmp, kubera=None, gateway=None, mode="simulate",
                  live_file=False):
    cfg = make_cfg(tmp, mode=mode, live_file=live_file)
    arm(cfg)
    return setu_exec.Executor(cfg, kubera or FakeKubera(),
                              gateway or FakeGatewayV060())


def events_of(kubera, kind=None):
    return [(r, k, d) for (r, k, d) in kubera.events
            if kind is None or k == kind]


# ---------------------------------------------------------------------------
# sell_shares gates
# ---------------------------------------------------------------------------

class SellTifGateTests(unittest.TestCase):
    def test_gtc_passes(self):
        ok, detail = setu_exec.gate_sell_tif(dict(SELL_ROW))
        self.assertTrue(ok)
        self.assertEqual(detail["tif"], "GTC")

    def test_anything_else_is_bad_tif(self):
        for bad in ("DAY", "", "WEEK", "IOC", None):
            ok, detail = setu_exec.gate_sell_tif(
                dict(SELL_ROW, tif=bad))
            self.assertFalse(ok, bad)
            self.assertEqual(detail["gate"], "bad_tif")


class SellContractGateTests(unittest.TestCase):
    def test_failed_read_and_empty_are_not_found(self):
        for details in (None, []):
            ok, detail = setu_exec.gate_sell_contract(
                dict(SELL_ROW), details)
            self.assertFalse(ok)
            self.assertEqual(detail["gate"], "contract_not_found")

    def test_ambiguous_is_mismatch(self):
        ok, detail = setu_exec.gate_sell_contract(
            dict(SELL_ROW), [dict(STK_VERIFIED), dict(STK_VERIFIED)])
        self.assertFalse(ok)
        self.assertEqual(detail["gate"], "contract_mismatch")

    def test_option_contract_is_mismatch(self):
        opt = dict(STK_VERIFIED, sec_type="OPT")
        ok, detail = setu_exec.gate_sell_contract(dict(SELL_ROW), [opt])
        self.assertFalse(ok)
        self.assertEqual(detail["gate"], "contract_mismatch")
        self.assertIn("sec_type", detail["reason"])

    def test_wrong_symbol_is_mismatch(self):
        other = dict(STK_VERIFIED, symbol="IREN")
        ok, detail = setu_exec.gate_sell_contract(dict(SELL_ROW),
                                                  [other])
        self.assertFalse(ok)
        self.assertEqual(detail["gate"], "contract_mismatch")

    def test_verified_stock_passes(self):
        ok, detail = setu_exec.gate_sell_contract(
            dict(SELL_ROW), [dict(STK_VERIFIED)])
        self.assertTrue(ok)
        self.assertEqual(detail["verified"]["con_id"], IONQ_STK)


class SellPositionGateTests(unittest.TestCase):
    def test_missing_position_refuses(self):
        ok, detail = setu_exec.gate_sell_position(
            dict(SELL_ROW), None, [])
        self.assertFalse(ok)
        self.assertEqual(detail["gate"], "position_missing")

    def test_zero_and_short_refuse(self):
        for qty in (0.0, -50.0):
            ok, detail = setu_exec.gate_sell_position(
                dict(SELL_ROW), stock_position(qty=qty), [])
            self.assertFalse(ok, qty)
            self.assertEqual(detail["gate"], "position_missing")

    def test_full_fit_passes(self):
        ok, detail = setu_exec.gate_sell_position(
            dict(SELL_ROW), stock_position(qty=200.0), [])
        self.assertTrue(ok)
        self.assertEqual(detail["effective_qty"], 100.0)
        self.assertFalse(detail["reduced"])
        self.assertEqual(detail["working_sells"], 0.0)

    def test_reduced_to_holding(self):
        ok, detail = setu_exec.gate_sell_position(
            dict(SELL_ROW, quantity=250.0), stock_position(qty=200.0),
            [])
        self.assertTrue(ok)
        self.assertEqual(detail["effective_qty"], 200.0)
        self.assertTrue(detail["reduced"])
        self.assertEqual(detail["position_qty"], 200.0)

    def test_oversell_counts_existing_working_sell(self):
        # 150 already working + 100 new > 200 long.
        ok, detail = setu_exec.gate_sell_position(
            dict(SELL_ROW), stock_position(qty=200.0),
            [sell_order(qty=150.0)])
        self.assertFalse(ok)
        self.assertEqual(detail["gate"], "oversell")
        self.assertIn("150", detail["reason"])
        self.assertIn("200", detail["reason"])

    def test_exact_fit_with_existing_sell_passes(self):
        ok, detail = setu_exec.gate_sell_position(
            dict(SELL_ROW), stock_position(qty=200.0),
            [sell_order(qty=100.0)])
        self.assertTrue(ok)
        self.assertEqual(detail["working_sells"], 100.0)

    def test_terminal_and_buy_and_other_stock_not_counted(self):
        orders = [
            sell_order(qty=500.0, status="Filled"),
            sell_order(qty=500.0, action="BUY"),
            sell_order(qty=500.0, con_id=IREN_STK),
            sell_order(qty=500.0, order_ref="sell-row-1"),
        ]
        ok, detail = setu_exec.gate_sell_position(
            dict(SELL_ROW), stock_position(qty=200.0), orders)
        self.assertTrue(ok)
        self.assertEqual(detail["working_sells"], 0.0)


class SellPriceGateTests(unittest.TestCase):
    def test_nonpositive_refuses(self):
        for bad in (0.0, -1.0, None):
            ok, detail = setu_exec.gate_sell_price(
                dict(SELL_ROW, target_limit=bad), 0.01)
            self.assertFalse(ok, bad)
            self.assertEqual(detail["gate"], "price")

    def test_tick_rounding_flagged(self):
        ok, detail = setu_exec.gate_sell_price(
            dict(SELL_ROW, target_limit=40.004), 0.01)
        self.assertTrue(ok)
        self.assertEqual(detail["limit"], 40.00)
        self.assertTrue(detail["limit_rounded"])

    def test_clean_price_not_flagged(self):
        ok, detail = setu_exec.gate_sell_price(dict(SELL_ROW), 0.01)
        self.assertTrue(ok)
        self.assertEqual(detail["limit"], 40.00)
        self.assertFalse(detail["limit_rounded"])


class SellSpecTests(unittest.TestCase):
    def test_frozen_shape(self):
        spec = setu_exec.build_sell_order_spec(
            dict(SELL_ROW), 100.0, 40.0, what_if=True)
        contract, order = spec["contract"], spec["order"]
        self.assertEqual(contract["secType"], "STK")
        self.assertEqual(contract["conId"], IONQ_STK)
        self.assertEqual(order["action"], "SELL")
        self.assertEqual(order["orderType"], "LMT")
        self.assertEqual(order["tif"], "GTC")
        self.assertEqual(order["totalQuantity"], 100.0)
        self.assertEqual(order["lmtPrice"], 40.0)
        self.assertFalse(order["outsideRth"])
        self.assertEqual(order["orderRef"], "sell-row-1")
        self.assertTrue(order["whatIf"])


# ---------------------------------------------------------------------------
# sell_shares flow
# ---------------------------------------------------------------------------

class SellFlowTests(unittest.TestCase):
    def test_simulate_would_place_stk_spec(self):
        with tempfile.TemporaryDirectory() as tmp:
            kubera, gw = FakeKubera(), FakeGatewayV060()
            make_executor(tmp, kubera, gw).cycle()
            would = events_of(kubera, "would_place")
            self.assertEqual(len(would), 1)
            detail = would[0][2]
            self.assertEqual(detail["kind"], "sell_shares")
            self.assertEqual(detail["effective_qty"], 100.0)
            order, contract = (detail["order"]["order"],
                               detail["order"]["contract"])
            self.assertEqual(contract["secType"], "STK")
            self.assertEqual(order["action"], "SELL")
            self.assertEqual(order["lmtPrice"], 40.00)
            self.assertEqual(order["orderRef"], "sell-row-1")
            self.assertEqual(len(gw.whatif_specs), 1)
            self.assertTrue(gw.whatif_specs[0]["order"]["whatIf"])
            self.assertEqual(gw.whatif_cc_specs, [])
            self.assertEqual(gw.transmit_specs, [])

    def test_reduced_placement(self):
        with tempfile.TemporaryDirectory() as tmp:
            kubera = FakeKubera(rows=[dict(SELL_ROW, quantity=250.0)])
            gw = FakeGatewayV060()
            make_executor(tmp, kubera, gw).cycle()
            would = events_of(kubera, "would_place")
            self.assertEqual(len(would), 1)
            detail = would[0][2]
            self.assertEqual(detail["effective_qty"], 200.0)
            self.assertTrue(detail["reduced"])
            self.assertEqual(detail["position_qty"], 200.0)
            self.assertEqual(
                detail["order"]["order"]["totalQuantity"], 200.0)

    def test_oversell_refused_in_simulate(self):
        with tempfile.TemporaryDirectory() as tmp:
            kubera = FakeKubera()
            gw = FakeGatewayV060(open_orders=[sell_order(qty=150.0)])
            make_executor(tmp, kubera, gw).cycle()
            would = events_of(kubera, "would_place")
            self.assertEqual(len(would), 1)
            self.assertTrue(would[0][2]["simulate_gate_failed"])
            self.assertEqual(would[0][2]["gate"], "oversell")
            self.assertEqual(would[0][2]["kind"], "sell_shares")
            self.assertEqual(gw.whatif_specs, [])
            self.assertEqual(gw.transmit_specs, [])

    def test_bad_tif_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            kubera = FakeKubera(rows=[dict(SELL_ROW, tif="DAY")])
            gw = FakeGatewayV060()
            make_executor(tmp, kubera, gw).cycle()
            would = events_of(kubera, "would_place")
            self.assertEqual(len(would), 1)
            self.assertEqual(would[0][2]["gate"], "bad_tif")
            self.assertEqual(gw.details_calls, [])

    def test_bypass_stamped_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            kubera = FakeKubera(
                rows=[dict(SELL_ROW, approval_mode="bypass")])
            gw = FakeGatewayV060()
            make_executor(tmp, kubera, gw).cycle()
            would = events_of(kubera, "would_place")
            self.assertEqual(len(would), 1)
            self.assertEqual(would[0][2]["gate"],
                             "bypass_not_supported")
            self.assertEqual(gw.transmit_specs, [])

    def test_live_transmits_once_and_tracks(self):
        with tempfile.TemporaryDirectory() as tmp:
            kubera, gw = FakeKubera(), FakeGatewayV060()
            ex = make_executor(tmp, kubera, gw, mode="live",
                               live_file=True)
            ex.cycle()
            placed = events_of(kubera, "placed")
            self.assertEqual(len(placed), 1)
            self.assertEqual(placed[0][2]["kind"], "sell_shares")
            self.assertEqual(len(gw.transmit_specs), 1)
            self.assertFalse(
                gw.transmit_specs[0]["order"]["whatIf"])
            rec = ex.tracker.get("sell-row-1")
            self.assertIsNotNone(rec)
            self.assertEqual(rec["kind"], "sell_shares")
            # Second cycle: the tracked row is never re-placed.
            ex.cycle()
            self.assertEqual(len(gw.transmit_specs), 1)
            self.assertEqual(len(events_of(kubera, "placed")), 1)


# ---------------------------------------------------------------------------
# share-parent wing gates
# ---------------------------------------------------------------------------

class ShareWingParentGateTests(unittest.TestCase):
    def test_missing_parent_refuses(self):
        ok, detail = setu_exec.gate_share_wing_parent(
            dict(SHARE_WING_ROW), {}, 100.0)
        self.assertFalse(ok)
        self.assertEqual(detail["gate"], "parent_position_missing")

    def test_not_long_parent_refuses(self):
        for qty in (0.0, -100.0):
            positions = {IREN_STK: stock_position(IREN_STK, "IREN",
                                                  qty)}
            ok, detail = setu_exec.gate_share_wing_parent(
                dict(SHARE_WING_ROW), positions, 100.0)
            self.assertFalse(ok, qty)
            self.assertEqual(detail["gate"],
                             "parent_position_missing")

    def test_multiplier_arithmetic_from_resolved_contract(self):
        # 18 contracts x 100 = 1800 required; 1000 shares refuse.
        positions = {IREN_STK: stock_position(IREN_STK, "IREN",
                                             1000.0)}
        ok, detail = setu_exec.gate_share_wing_parent(
            dict(SHARE_WING_ROW), positions, 100.0)
        self.assertFalse(ok)
        self.assertEqual(detail["gate"], "parent_qty_insufficient")
        self.assertIn("1800", detail["reason"])
        self.assertIn("1000", detail["reason"])
        # The same holding passes against a x50 contract: 900.
        ok, detail = setu_exec.gate_share_wing_parent(
            dict(SHARE_WING_ROW), positions, 50.0)
        self.assertTrue(ok)
        self.assertEqual(detail["required_shares"], 900.0)

    def test_exact_fit_passes(self):
        positions = {IREN_STK: stock_position(IREN_STK, "IREN",
                                             1800.0)}
        ok, detail = setu_exec.gate_share_wing_parent(
            dict(SHARE_WING_ROW), positions, 100.0)
        self.assertTrue(ok)
        self.assertEqual(detail["long_shares"], 1800.0)


class ShareWingRulesGateTests(unittest.TestCase):
    def parent(self, price=24.18):
        return stock_position(IREN_STK, "IREN", 3700.0, price)

    def test_call_wing_violates(self):
        verified = dict(WING_VERIFIED, right="C")
        ok, detail = setu_exec.gate_share_wing_rules(
            dict(SHARE_WING_ROW), verified, self.parent())
        self.assertFalse(ok)
        self.assertEqual(detail["gate"], "wing_rule_violation")
        self.assertIn("put", detail["reason"])

    def test_strike_below_spot_passes(self):
        ok, detail = setu_exec.gate_share_wing_rules(
            dict(SHARE_WING_ROW), dict(WING_VERIFIED), self.parent())
        self.assertTrue(ok)
        self.assertEqual(detail["spot"], 24.18)

    def test_strike_at_or_above_spot_violates(self):
        for strike in (24.18, 30.0):
            verified = dict(WING_VERIFIED, strike=strike)
            ok, detail = setu_exec.gate_share_wing_rules(
                dict(SHARE_WING_ROW), verified, self.parent())
            self.assertFalse(ok, strike)
            self.assertEqual(detail["gate"], "wing_rule_violation")
            self.assertIn(f"{strike:g}", detail["reason"])
            self.assertIn("24.18", detail["reason"])

    def test_missing_spot_refuses_rather_than_guessing(self):
        parent = self.parent()
        del parent["market_price"]
        ok, detail = setu_exec.gate_share_wing_rules(
            dict(SHARE_WING_ROW), dict(WING_VERIFIED), parent)
        self.assertFalse(ok)
        self.assertEqual(detail["gate"], "wing_rule_violation")
        self.assertIn("no market price", detail["reason"])


# ---------------------------------------------------------------------------
# share-parent wing flow
# ---------------------------------------------------------------------------

def share_wing_positions(shares=3700.0, price=24.18):
    return {IREN_STK: stock_position(IREN_STK, "IREN", shares, price)}


class ShareWingFlowTests(unittest.TestCase):
    def test_simulate_resolves_and_would_place(self):
        with tempfile.TemporaryDirectory() as tmp:
            kubera = FakeKubera(rows=[dict(SHARE_WING_ROW)])
            gw = FakeGatewayV060(positions=share_wing_positions())
            make_executor(tmp, kubera, gw).cycle()
            would = events_of(kubera, "would_place")
            self.assertEqual(len(would), 1)
            detail = would[0][2]
            self.assertEqual(detail["kind"], "wing_open")
            self.assertEqual(detail["parent_kind"], "shares")
            self.assertEqual(detail["wing_con_id"], WING_CON_ID)
            self.assertEqual(gw.spec_calls,
                             [("IREN", "2027-01-15", 20.0, "P")])
            order, contract = (detail["order"]["order"],
                               detail["order"]["contract"])
            self.assertEqual(order["action"], "BUY")
            self.assertEqual(order["lmtPrice"], 1.20)
            self.assertEqual(contract["conId"], WING_CON_ID)
            self.assertEqual(gw.whatif_cc_specs, [])
            self.assertEqual(gw.transmit_specs, [])

    def test_strike_above_spot_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            kubera = FakeKubera(
                rows=[dict(SHARE_WING_ROW, strike=30.0)])
            gw = FakeGatewayV060(
                positions=share_wing_positions(),
                spec_details=[dict(WING_VERIFIED, strike=30.0)])
            make_executor(tmp, kubera, gw).cycle()
            would = events_of(kubera, "would_place")
            self.assertEqual(len(would), 1)
            self.assertTrue(would[0][2]["simulate_gate_failed"])
            self.assertEqual(would[0][2]["gate"],
                             "wing_rule_violation")
            self.assertIn("30", would[0][2]["reason"])
            self.assertIn("24.18", would[0][2]["reason"])
            self.assertEqual(gw.whatif_specs, [])

    def test_parent_qty_insufficient_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            kubera = FakeKubera(rows=[dict(SHARE_WING_ROW)])
            gw = FakeGatewayV060(
                positions=share_wing_positions(shares=1000.0))
            make_executor(tmp, kubera, gw).cycle()
            would = events_of(kubera, "would_place")
            self.assertEqual(len(would), 1)
            self.assertEqual(would[0][2]["gate"],
                             "parent_qty_insufficient")
            self.assertEqual(gw.whatif_specs, [])

    def test_parent_missing_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            kubera = FakeKubera(rows=[dict(SHARE_WING_ROW)])
            gw = FakeGatewayV060(positions={})
            make_executor(tmp, kubera, gw).cycle()
            would = events_of(kubera, "would_place")
            self.assertEqual(len(would), 1)
            self.assertEqual(would[0][2]["gate"],
                             "parent_position_missing")

    def test_bypass_stamped_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            kubera = FakeKubera(rows=[dict(
                SHARE_WING_ROW, approval_mode="bypass")])
            gw = FakeGatewayV060(positions=share_wing_positions())
            make_executor(tmp, kubera, gw).cycle()
            would = events_of(kubera, "would_place")
            self.assertEqual(len(would), 1)
            self.assertEqual(would[0][2]["gate"],
                             "bypass_not_supported")
            self.assertEqual(gw.spec_calls, [])


# ---------------------------------------------------------------------------
# cc_open con_id sentinel
# ---------------------------------------------------------------------------

class CcResolveGateTests(unittest.TestCase):
    def test_failed_read_is_not_found(self):
        ok, detail = setu_exec.resolve_cc_contract(dict(CC_ROW), None)
        self.assertFalse(ok)
        self.assertEqual(detail["gate"], "contract_not_found")

    def test_no_exact_match_is_not_found(self):
        wrong = dict(CC_VERIFIED, strike=300.0)
        for details in ([], [wrong]):
            ok, detail = setu_exec.resolve_cc_contract(
                dict(CC_ROW), details)
            self.assertFalse(ok)
            self.assertEqual(detail["gate"], "contract_not_found")

    def test_duplicate_entries_for_one_con_id_resolve(self):
        ok, detail = setu_exec.resolve_cc_contract(
            dict(CC_ROW), [dict(CC_VERIFIED), dict(CC_VERIFIED)])
        self.assertTrue(ok)
        self.assertEqual(detail["resolved_con_id"], CC_CON_ID)

    def test_two_distinct_con_ids_are_mismatch(self):
        other = dict(CC_VERIFIED, con_id=CC_CON_ID + 1)
        ok, detail = setu_exec.resolve_cc_contract(
            dict(CC_ROW), [dict(CC_VERIFIED), other])
        self.assertFalse(ok)
        self.assertEqual(detail["gate"], "contract_mismatch")


def cc_positions():
    return {MRVL_STK: {"con_id": MRVL_STK, "symbol": "MRVL",
                       "sec_type": "STK", "right": "",
                       "quantity": 400.0, "avg_cost": 254.23}}


class CcSentinelFlowTests(unittest.TestCase):
    def test_sentinel_resolves_and_would_place(self):
        with tempfile.TemporaryDirectory() as tmp:
            kubera = FakeKubera(rows=[dict(CC_ROW)])
            gw = FakeGatewayV060(positions=cc_positions(),
                                 details=[dict(CC_VERIFIED)],
                                 spec_details=[dict(CC_VERIFIED)])
            make_executor(tmp, kubera, gw).cycle()
            would = events_of(kubera, "would_place")
            self.assertEqual(len(would), 1)
            detail = would[0][2]
            self.assertEqual(detail["kind"], "cc_open")
            # The resolution is in the event detail, and the
            # pipeline ran with the resolved id from there on.
            self.assertEqual(detail["resolved_con_id"], CC_CON_ID)
            self.assertEqual(
                detail["order"]["contract"]["conId"], CC_CON_ID)
            self.assertEqual(gw.spec_calls,
                             [("MRVL", "2026-11-27", 295.0, "C")])
            self.assertEqual(gw.details_calls, [CC_CON_ID])
            # The conclusive cc what-if still guards the row.
            self.assertEqual(len(gw.whatif_cc_specs), 1)
            self.assertEqual(gw.whatif_specs, [])
            self.assertEqual(gw.transmit_specs, [])

    def test_sentinel_not_found_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            kubera = FakeKubera(rows=[dict(CC_ROW)])
            gw = FakeGatewayV060(positions=cc_positions(),
                                 spec_details=[])
            make_executor(tmp, kubera, gw).cycle()
            would = events_of(kubera, "would_place")
            self.assertEqual(len(would), 1)
            self.assertTrue(would[0][2]["simulate_gate_failed"])
            self.assertEqual(would[0][2]["gate"],
                             "contract_not_found")
            self.assertEqual(gw.whatif_cc_specs, [])

    def test_sentinel_multi_match_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            kubera = FakeKubera(rows=[dict(CC_ROW)])
            gw = FakeGatewayV060(
                positions=cc_positions(),
                spec_details=[dict(CC_VERIFIED),
                              dict(CC_VERIFIED, con_id=CC_CON_ID + 1)])
            make_executor(tmp, kubera, gw).cycle()
            would = events_of(kubera, "would_place")
            self.assertEqual(len(would), 1)
            self.assertEqual(would[0][2]["gate"],
                             "contract_mismatch")

    def test_real_con_id_never_touches_the_resolver(self):
        # The pin: a Molang-staged row with a real con_id takes
        # the v0.4 path untouched — the by-spec resolver is not
        # called and no resolved_con_id appears in the detail.
        with tempfile.TemporaryDirectory() as tmp:
            kubera = FakeKubera(
                rows=[dict(CC_ROW, con_id=CC_CON_ID)])
            gw = FakeGatewayV060(positions=cc_positions(),
                                 details=[dict(CC_VERIFIED)],
                                 spec_details=[])
            make_executor(tmp, kubera, gw).cycle()
            would = events_of(kubera, "would_place")
            self.assertEqual(len(would), 1)
            self.assertNotIn("resolved_con_id", would[0][2])
            self.assertEqual(gw.spec_calls, [])
            self.assertEqual(gw.details_calls, [CC_CON_ID])

    def test_sentinel_gate_failure_still_carries_resolved_id(self):
        # A refusal PAST resolution (here the conclusive what-if)
        # is a would_place event too — the resolved id rides it,
        # exactly as it rides the success events.
        with tempfile.TemporaryDirectory() as tmp:
            kubera = FakeKubera(rows=[dict(CC_ROW)])
            gw = FakeGatewayV060(
                positions=cc_positions(),
                details=[dict(CC_VERIFIED)],
                spec_details=[dict(CC_VERIFIED)],
                whatif_cc_reply={"ok": False,
                                 "error": "IB error 201: rejected"})
            make_executor(tmp, kubera, gw).cycle()
            would = events_of(kubera, "would_place")
            self.assertEqual(len(would), 1)
            self.assertTrue(would[0][2]["simulate_gate_failed"])
            self.assertEqual(would[0][2]["gate"], "margin_impact")
            self.assertEqual(would[0][2]["resolved_con_id"],
                             CC_CON_ID)

    def test_sentinel_live_places_with_resolved_id(self):
        with tempfile.TemporaryDirectory() as tmp:
            kubera = FakeKubera(rows=[dict(CC_ROW)])
            gw = FakeGatewayV060(positions=cc_positions(),
                                 details=[dict(CC_VERIFIED)],
                                 spec_details=[dict(CC_VERIFIED)])
            ex = make_executor(tmp, kubera, gw, mode="live",
                               live_file=True)
            ex.cycle()
            placed = events_of(kubera, "placed")
            self.assertEqual(len(placed), 1)
            self.assertEqual(placed[0][2]["resolved_con_id"],
                             CC_CON_ID)
            self.assertEqual(len(gw.transmit_specs), 1)
            self.assertEqual(
                gw.transmit_specs[0]["contract"]["conId"], CC_CON_ID)


# ---------------------------------------------------------------------------
# Gateway: market price merged into the positions read (v0.6)
# ---------------------------------------------------------------------------

class FakeIBClientPrices:
    """Stands in for ibapi's EClient inside a real ExecGateway,
    driving the real wrapper callbacks, with or without the
    account-updates portfolio snapshot."""

    def __init__(self, gw, with_updates=True):
        self.gw = gw
        self.with_updates = with_updates
        self.unsubscribed = False

    def reqAccountUpdates(self, subscribe, acctCode):
        if subscribe and self.with_updates:
            contract = SimpleNamespace(conId=IONQ_STK)
            self.gw.wrapper.updatePortfolio(
                contract, 200.0, 30.65, 6130.0, 3205.0, -30.0, 0.0,
                "U19377323")
            self.gw.wrapper.accountDownloadEnd("U19377323")
        if not subscribe:
            self.unsubscribed = True

    def reqPositions(self):
        contract = SimpleNamespace(conId=IONQ_STK, multiplier="",
                                   secType="STK", symbol="IONQ",
                                   right="")
        self.gw.wrapper.position("U19377323", contract, 200.0,
                                 3205.0)
        self.gw.wrapper.positionEnd()

    def cancelPositions(self):
        pass


class GatewayMarketPriceTests(unittest.TestCase):
    def test_positions_read_carries_market_price(self):
        with tempfile.TemporaryDirectory() as tmp:
            gw = setu_exec.ExecGateway(make_cfg(tmp))
            gw.client = FakeIBClientPrices(gw)
            positions = gw.fetch_positions(timeout=2.0)
            self.assertIsNotNone(positions)
            self.assertEqual(positions[IONQ_STK]["market_price"],
                             30.65)
            self.assertEqual(positions[IONQ_STK]["quantity"], 200.0)

    def test_positions_read_without_updates_still_completes(self):
        with tempfile.TemporaryDirectory() as tmp:
            gw = setu_exec.ExecGateway(make_cfg(tmp))
            gw.client = FakeIBClientPrices(gw, with_updates=False)
            positions = gw.fetch_positions(timeout=5.0)
            self.assertIsNotNone(positions)
            self.assertNotIn("market_price", positions[IONQ_STK])


if __name__ == "__main__":
    unittest.main()
