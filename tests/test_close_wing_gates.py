"""Tests for setu-exec v0.5 close_now and wing_open rows.

Synthetic data only, with fake Kubera/Gateway doubles in the same
style as tests/test_cc_open_gates.py. No real Gateway, no real
orders.

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

PARENT_CON_ID = 922423616  # IONQ 2026-10-30 38P (the rehearsal parent)
WING_CON_ID = 815001       # the resolved 30P wing (placeholder)

CLOSE_ROW = {
    "id": "close-row-1",
    "account_id": "U19377323",
    "con_id": PARENT_CON_ID,
    "symbol": "IONQ",
    "local_symbol": "IONQ  261030P00038000",
    "expiry": "2026-10-30",
    "strike": 38.0,
    "option_type": "Put",
    "quantity": 5.0,
    "premium": None,
    "target_limit": 3.00,
    "tif": "GTC",
    "action": "place",
    "kind": "close_now",
    "approval_mode": "manual",
    "source": "manual",
}

WING_ROW = {
    "id": "wing-row-1",
    "account_id": "U19377323",
    "con_id": 0,
    "symbol": "IONQ",
    "local_symbol": "",
    "expiry": "2026-10-30",
    "strike": 30.0,
    "option_type": "Put",
    "quantity": 5.0,
    "premium": None,
    "target_limit": 0.80,
    "tif": "GTC",
    "action": "place",
    "kind": "wing_open",
    "approval_mode": "manual",
    "source": "manual",
    "parent_con_id": PARENT_CON_ID,
    "parent_symbol": "IONQ",
    "parent_expiry": "2026-10-30",
    "parent_strike": 38.0,
    "parent_option_type": "Put",
    "parent_quantity": 5.0,
}

WING_VERIFIED = {
    "con_id": WING_CON_ID,
    "symbol": "IONQ",
    "sec_type": "OPT",
    "expiry": "20261030",
    "strike": 30.0,
    "right": "P",
    "multiplier": "100",
    "min_tick": 0.01,
}


def parent_position(qty=-5.0):
    return {"con_id": PARENT_CON_ID, "symbol": "IONQ",
            "sec_type": "OPT", "right": "P", "quantity": qty,
            "avg_cost": 0.9931}


def base_positions():
    return {PARENT_CON_ID: parent_position()}


def buy_order(con_id=PARENT_CON_ID, **over):
    order = {
        "con_id": con_id,
        "symbol": "IONQ",
        "expiry": "20261030",
        "strike": 38.0,
        "right": "P",
        "action": "BUY",
        "total_quantity": 5.0,
        "limit_price": 0.40,
        "order_ref": "",
        "status": "Submitted",
        "perm_id": 515151,
        "order_id": 9901,
        "filled": 0.0,
        "remaining": 5.0,
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
        self.rows = rows if rows is not None else [dict(CLOSE_ROW)]
        self.kill = kill
        self.events = []

    def get_control(self):
        return {"account_id": "U19377323", "kill_switch": self.kill}

    def poll_approved(self):
        return [dict(r) for r in self.rows]

    def post_event(self, row_id, kind, detail):
        self.events.append((row_id, kind, detail))
        return {"status": "success"}


class FakeGatewayNew:
    """Serves the v0.5 paths: positions/open orders, contract
    details by con_id, contract details by spec, the STANDARD
    what-if (whatif_cc calls are counted to prove the new kinds
    never use the cc variant), and transmit."""

    def __init__(self, positions="default", open_orders=None,
                 details="default", spec_details="default",
                 whatif_reply=None, transmit_result=None):
        if positions == "default":
            positions = base_positions()
        self.positions_map = positions  # None means: reads fail
        self.open_orders = open_orders if open_orders is not None else []
        self.details = ([dict(WING_VERIFIED)] if details == "default"
                        else details)
        self.spec_details = ([dict(WING_VERIFIED)]
                             if spec_details == "default"
                             else spec_details)
        self.whatif_reply = whatif_reply or {
            "ok": True, "init_margin_change": -25.0,
            "maint_margin_change": -10.0, "equity_with_loan_change": 0.0,
            "commission": None}
        self.transmit_result = transmit_result or {
            "order_id": 6001, "perm_id": 888, "status": "PreSubmitted",
            "error": None, "avg_fill_price": None}
        self.last_status_by_perm = {}
        self.last_fill_by_perm = {}
        self.whatif_specs = []
        self.whatif_cc_calls = 0
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
        self.whatif_cc_calls += 1
        return dict(self.whatif_reply)

    def transmit(self, spec):
        self.transmit_specs.append(copy.deepcopy(spec))
        return dict(self.transmit_result)


def make_executor(tmp, kubera=None, gateway=None, mode="simulate",
                  live_file=False):
    cfg = make_cfg(tmp, mode=mode, live_file=live_file)
    arm(cfg)
    return setu_exec.Executor(cfg, kubera or FakeKubera(),
                              gateway or FakeGatewayNew())


def events_of(kubera, kind=None):
    return [(r, k, d) for (r, k, d) in kubera.events
            if kind is None or k == kind]


# ---------------------------------------------------------------------------
# close_now gates
# ---------------------------------------------------------------------------

class CloseTifGateTests(unittest.TestCase):
    def test_gtc_and_day_pass(self):
        for tif in ("GTC", "DAY"):
            ok, detail = setu_exec.gate_close_tif(dict(CLOSE_ROW, tif=tif))
            self.assertTrue(ok)
            self.assertEqual(detail["tif"], tif)

    def test_anything_else_is_bad_tif(self):
        for bad in ("", "WEEK", "IOC", "GTD", None):
            ok, detail = setu_exec.gate_close_tif(
                dict(CLOSE_ROW, tif=bad))
            self.assertFalse(ok)
            self.assertEqual(detail["gate"], "bad_tif")


class ClosePositionGateTests(unittest.TestCase):
    def test_full_short_passes(self):
        ok, detail = setu_exec.gate_close_position(
            CLOSE_ROW, parent_position(-5.0))
        self.assertTrue(ok)
        self.assertEqual(detail["effective_qty"], 5.0)
        self.assertFalse(detail["reduced"])

    def test_smaller_short_reduces(self):
        ok, detail = setu_exec.gate_close_position(
            CLOSE_ROW, parent_position(-3.0))
        self.assertTrue(ok)
        self.assertEqual(detail["effective_qty"], 3.0)
        self.assertTrue(detail["reduced"])
        self.assertEqual(detail["position_qty"], -3.0)

    def test_missing_position_refuses(self):
        ok, detail = setu_exec.gate_close_position(CLOSE_ROW, None)
        self.assertFalse(ok)
        self.assertEqual(detail["gate"], "position_missing")

    def test_not_short_refuses(self):
        for qty in (0.0, 2.0):
            ok, detail = setu_exec.gate_close_position(
                CLOSE_ROW, parent_position(qty))
            self.assertFalse(ok)
            self.assertEqual(detail["gate"], "position_missing")


class CloseDuplicateGateTests(unittest.TestCase):
    def test_resting_buy_blocks_and_names_the_fix(self):
        ok, detail = setu_exec.gate_close_duplicate(
            CLOSE_ROW, [buy_order()])
        self.assertFalse(ok)
        self.assertEqual(detail["gate"], "duplicate_open_order")
        self.assertIn("must be cancelled first", detail["reason"])
        self.assertIn("over-buy", detail["reason"])

    def test_buy_at_any_price_blocks(self):
        ok, _ = setu_exec.gate_close_duplicate(
            CLOSE_ROW, [buy_order(limit_price=9.99)])
        self.assertFalse(ok)

    def test_sell_on_same_contract_ok(self):
        ok, _ = setu_exec.gate_close_duplicate(
            CLOSE_ROW, [buy_order(action="SELL")])
        self.assertTrue(ok)

    def test_buy_on_other_contract_ok(self):
        ok, _ = setu_exec.gate_close_duplicate(
            CLOSE_ROW, [buy_order(con_id=123456)])
        self.assertTrue(ok)

    def test_terminal_buy_does_not_block(self):
        ok, _ = setu_exec.gate_close_duplicate(
            CLOSE_ROW, [buy_order(status="Filled")])
        self.assertTrue(ok)

    def test_order_ref_blocks(self):
        ok, detail = setu_exec.gate_close_duplicate(
            CLOSE_ROW, [buy_order(con_id=123456,
                                  order_ref="close-row-1")])
        self.assertFalse(ok)
        self.assertEqual(detail["gate"], "duplicate_open_order")


class ClosePriceGateTests(unittest.TestCase):
    def test_positive_limit_passes(self):
        ok, detail = setu_exec.gate_close_price(CLOSE_ROW)
        self.assertTrue(ok)
        self.assertEqual(detail["limit"], 3.00)
        self.assertFalse(detail["limit_rounded"])

    def test_off_tick_limit_rounded(self):
        ok, detail = setu_exec.gate_close_price(
            dict(CLOSE_ROW, target_limit=3.004))
        self.assertTrue(ok)
        self.assertEqual(detail["limit"], 3.00)
        self.assertTrue(detail["limit_rounded"])

    def test_non_positive_refused(self):
        for bad in (0.0, -0.50, None):
            ok, detail = setu_exec.gate_close_price(
                dict(CLOSE_ROW, target_limit=bad))
            self.assertFalse(ok)
            self.assertEqual(detail["gate"], "price")


class CloseSpecTests(unittest.TestCase):
    def test_frozen_shape_gtc(self):
        spec = setu_exec.build_close_order_spec(
            CLOSE_ROW, 5.0, 3.00, "GTC", what_if=False)
        c, o = spec["contract"], spec["order"]
        self.assertEqual(c["conId"], PARENT_CON_ID)
        self.assertEqual(c["right"], "P")
        self.assertEqual(c["secType"], "OPT")
        self.assertEqual(c["exchange"], "SMART")
        self.assertEqual(o["action"], "BUY")
        self.assertEqual(o["orderType"], "LMT")
        self.assertEqual(o["tif"], "GTC")
        self.assertFalse(o["outsideRth"])
        self.assertEqual(o["totalQuantity"], 5.0)
        self.assertEqual(o["lmtPrice"], 3.00)
        self.assertEqual(o["orderRef"], "close-row-1")
        self.assertFalse(o["whatIf"])

    def test_day_tif_carried(self):
        spec = setu_exec.build_close_order_spec(
            CLOSE_ROW, 5.0, 3.00, "DAY", what_if=True)
        self.assertEqual(spec["order"]["tif"], "DAY")
        self.assertTrue(spec["order"]["whatIf"])


# ---------------------------------------------------------------------------
# wing_open gates
# ---------------------------------------------------------------------------

class WingParentGateTests(unittest.TestCase):
    def test_parent_short_covers(self):
        ok, detail = setu_exec.gate_wing_parent(
            WING_ROW, base_positions())
        self.assertTrue(ok)
        self.assertEqual(detail["parent_short"], 5.0)

    def test_parent_missing(self):
        ok, detail = setu_exec.gate_wing_parent(WING_ROW, {})
        self.assertFalse(ok)
        self.assertEqual(detail["gate"], "parent_position_missing")

    def test_parent_not_short_is_missing(self):
        ok, detail = setu_exec.gate_wing_parent(
            WING_ROW, {PARENT_CON_ID: parent_position(5.0)})
        self.assertFalse(ok)
        self.assertEqual(detail["gate"], "parent_position_missing")

    def test_parent_smaller_than_wing(self):
        ok, detail = setu_exec.gate_wing_parent(
            WING_ROW, {PARENT_CON_ID: parent_position(-3.0)})
        self.assertFalse(ok)
        self.assertEqual(detail["gate"], "parent_qty_insufficient")
        self.assertIn("3", detail["reason"])
        self.assertIn("5", detail["reason"])


class WingContractGateTests(unittest.TestCase):
    def test_exact_match_passes(self):
        ok, detail = setu_exec.gate_wing_contract(
            dict(WING_ROW, con_id=WING_CON_ID), [dict(WING_VERIFIED)])
        self.assertTrue(ok)
        self.assertEqual(detail["verified"]["con_id"], WING_CON_ID)

    def test_failed_read_is_not_found(self):
        ok, detail = setu_exec.gate_wing_contract(
            dict(WING_ROW, con_id=WING_CON_ID), None)
        self.assertFalse(ok)
        self.assertEqual(detail["gate"], "contract_not_found")

    def test_empty_is_not_found(self):
        ok, detail = setu_exec.gate_wing_contract(
            dict(WING_ROW, con_id=WING_CON_ID), [])
        self.assertFalse(ok)
        self.assertEqual(detail["gate"], "contract_not_found")

    def test_two_is_mismatch(self):
        ok, detail = setu_exec.gate_wing_contract(
            dict(WING_ROW, con_id=WING_CON_ID),
            [dict(WING_VERIFIED), dict(WING_VERIFIED)])
        self.assertFalse(ok)
        self.assertEqual(detail["gate"], "contract_mismatch")

    def test_field_mismatch(self):
        ok, detail = setu_exec.gate_wing_contract(
            dict(WING_ROW, con_id=WING_CON_ID),
            [dict(WING_VERIFIED, strike=31.0)])
        self.assertFalse(ok)
        self.assertEqual(detail["gate"], "contract_mismatch")
        self.assertIn("strike", detail["reason"])


class WingResolveTests(unittest.TestCase):
    def test_one_exact_match_resolves(self):
        noise = dict(WING_VERIFIED, con_id=999, strike=31.0)
        ok, detail = setu_exec.resolve_wing_contract(
            WING_ROW, [noise, dict(WING_VERIFIED)])
        self.assertTrue(ok)
        self.assertEqual(detail["resolved_con_id"], WING_CON_ID)
        self.assertEqual(detail["verified"]["con_id"], WING_CON_ID)

    def test_same_con_id_twice_still_resolves(self):
        ok, detail = setu_exec.resolve_wing_contract(
            WING_ROW, [dict(WING_VERIFIED), dict(WING_VERIFIED)])
        self.assertTrue(ok)
        self.assertEqual(detail["resolved_con_id"], WING_CON_ID)

    def test_no_match_is_not_found(self):
        noise = dict(WING_VERIFIED, con_id=999, strike=31.0)
        ok, detail = setu_exec.resolve_wing_contract(WING_ROW, [noise])
        self.assertFalse(ok)
        self.assertEqual(detail["gate"], "contract_not_found")
        ok, detail = setu_exec.resolve_wing_contract(WING_ROW, [])
        self.assertFalse(ok)
        self.assertEqual(detail["gate"], "contract_not_found")

    def test_failed_read_is_not_found(self):
        ok, detail = setu_exec.resolve_wing_contract(WING_ROW, None)
        self.assertFalse(ok)
        self.assertEqual(detail["gate"], "contract_not_found")

    def test_two_distinct_matches_is_mismatch(self):
        other = dict(WING_VERIFIED, con_id=WING_CON_ID + 7)
        ok, detail = setu_exec.resolve_wing_contract(
            WING_ROW, [dict(WING_VERIFIED), other])
        self.assertFalse(ok)
        self.assertEqual(detail["gate"], "contract_mismatch")


class WingRulesTests(unittest.TestCase):
    def test_put_wing_further_otm_same_expiry_passes(self):
        ok, _ = setu_exec.gate_wing_rules(WING_ROW, dict(WING_VERIFIED))
        self.assertTrue(ok)

    def test_put_wing_later_expiry_passes(self):
        ok, _ = setu_exec.gate_wing_rules(
            WING_ROW, dict(WING_VERIFIED, expiry="20261106"))
        self.assertTrue(ok)

    def test_put_wing_itm_strike_violates(self):
        ok, detail = setu_exec.gate_wing_rules(
            WING_ROW, dict(WING_VERIFIED, strike=40.0))
        self.assertFalse(ok)
        self.assertEqual(detail["gate"], "wing_rule_violation")
        self.assertIn("further OTM", detail["reason"])

    def test_put_wing_at_parent_strike_violates(self):
        ok, detail = setu_exec.gate_wing_rules(
            WING_ROW, dict(WING_VERIFIED, strike=38.0))
        self.assertFalse(ok)
        self.assertEqual(detail["gate"], "wing_rule_violation")

    def test_wing_earlier_expiry_violates(self):
        ok, detail = setu_exec.gate_wing_rules(
            WING_ROW, dict(WING_VERIFIED, expiry="20261023"))
        self.assertFalse(ok)
        self.assertEqual(detail["gate"], "wing_rule_violation")
        self.assertIn("expiry", detail["reason"])

    def test_type_mismatch_violates(self):
        # The wing leg matches the ROW (a put) but the PARENT is a
        # call: staging inconsistency the transmit-time re-check
        # exists to catch.
        row = dict(WING_ROW, parent_option_type="Call",
                   parent_strike=38.0)
        ok, detail = setu_exec.gate_wing_rules(row, dict(WING_VERIFIED))
        self.assertFalse(ok)
        self.assertEqual(detail["gate"], "wing_rule_violation")
        self.assertIn("same type", detail["reason"])

    def test_call_wing_rules(self):
        row = dict(WING_ROW, option_type="Call",
                   parent_option_type="Call", parent_strike=100.0,
                   strike=110.0)
        verified = dict(WING_VERIFIED, right="C", strike=110.0)
        ok, _ = setu_exec.gate_wing_rules(row, verified)
        self.assertTrue(ok)
        ok, detail = setu_exec.gate_wing_rules(
            row, dict(verified, strike=90.0))
        self.assertFalse(ok)
        self.assertEqual(detail["gate"], "wing_rule_violation")


class WingDuplicateGateTests(unittest.TestCase):
    def test_working_buy_on_wing_blocks(self):
        ok, detail = setu_exec.gate_wing_duplicate(
            WING_ROW, [buy_order(con_id=WING_CON_ID)], WING_CON_ID)
        self.assertFalse(ok)
        self.assertEqual(detail["gate"], "duplicate_open_order")

    def test_buy_on_parent_does_not_block(self):
        ok, _ = setu_exec.gate_wing_duplicate(
            WING_ROW, [buy_order(con_id=PARENT_CON_ID)], WING_CON_ID)
        self.assertTrue(ok)

    def test_sell_on_wing_ok(self):
        ok, _ = setu_exec.gate_wing_duplicate(
            WING_ROW, [buy_order(con_id=WING_CON_ID, action="SELL")],
            WING_CON_ID)
        self.assertTrue(ok)

    def test_order_ref_blocks(self):
        ok, detail = setu_exec.gate_wing_duplicate(
            WING_ROW, [buy_order(con_id=123, order_ref="wing-row-1")],
            WING_CON_ID)
        self.assertFalse(ok)
        self.assertEqual(detail["gate"], "duplicate_open_order")


class WingPriceGateTests(unittest.TestCase):
    def test_on_tick_passes(self):
        ok, detail = setu_exec.gate_wing_price(WING_ROW, 0.01)
        self.assertTrue(ok)
        self.assertEqual(detail["limit"], 0.80)

    def test_rounded_on_resolved_tick(self):
        ok, detail = setu_exec.gate_wing_price(
            dict(WING_ROW, target_limit=0.805), 0.01)
        self.assertTrue(ok)
        self.assertEqual(detail["limit"], 0.81)
        self.assertTrue(detail["limit_rounded"])

    def test_non_positive_refused(self):
        for bad in (0.0, -0.25, None):
            ok, detail = setu_exec.gate_wing_price(
                dict(WING_ROW, target_limit=bad), 0.01)
            self.assertFalse(ok)
            self.assertEqual(detail["gate"], "price")


class WingSpecTests(unittest.TestCase):
    def test_built_from_verified_contract(self):
        spec = setu_exec.build_wing_order_spec(
            WING_ROW, dict(WING_VERIFIED), 0.80, what_if=False)
        c, o = spec["contract"], spec["order"]
        self.assertEqual(c["conId"], WING_CON_ID)
        self.assertEqual(c["symbol"], "IONQ")
        self.assertEqual(c["lastTradeDateOrContractMonth"], "20261030")
        self.assertEqual(c["strike"], 30.0)
        self.assertEqual(c["right"], "P")
        self.assertEqual(c["multiplier"], "100")
        self.assertEqual(o["action"], "BUY")
        self.assertEqual(o["orderType"], "LMT")
        self.assertEqual(o["tif"], "GTC")
        self.assertFalse(o["outsideRth"])
        self.assertEqual(o["totalQuantity"], 5.0)
        self.assertEqual(o["lmtPrice"], 0.80)
        self.assertEqual(o["orderRef"], "wing-row-1")


# ---------------------------------------------------------------------------
# Executor flows
# ---------------------------------------------------------------------------

class CloseFlowTests(unittest.TestCase):
    def test_simulate_would_place(self):
        with tempfile.TemporaryDirectory() as tmp:
            kubera, gw = FakeKubera(), FakeGatewayNew()
            make_executor(tmp, kubera, gw).cycle()
            would = events_of(kubera, "would_place")
            self.assertEqual(len(would), 1)
            detail = would[0][2]
            self.assertEqual(detail["kind"], "close_now")
            self.assertEqual(detail["effective_qty"], 5.0)
            order, contract = (detail["order"]["order"],
                               detail["order"]["contract"])
            self.assertEqual(order["action"], "BUY")
            self.assertEqual(order["tif"], "GTC")
            self.assertEqual(order["lmtPrice"], 3.00)
            self.assertEqual(order["orderRef"], "close-row-1")
            self.assertEqual(contract["conId"], PARENT_CON_ID)
            # Standard what-if only; never the cc variant, never a
            # transmission in simulate.
            self.assertEqual(len(gw.whatif_specs), 1)
            self.assertTrue(gw.whatif_specs[0]["order"]["whatIf"])
            self.assertEqual(gw.whatif_cc_calls, 0)
            self.assertEqual(gw.transmit_specs, [])

    def test_simulate_day_tif_transmitted_as_day(self):
        with tempfile.TemporaryDirectory() as tmp:
            kubera = FakeKubera(rows=[dict(CLOSE_ROW, tif="DAY")])
            gw = FakeGatewayNew()
            make_executor(tmp, kubera, gw).cycle()
            would = events_of(kubera, "would_place")
            self.assertEqual(len(would), 1)
            self.assertEqual(would[0][2]["order"]["order"]["tif"],
                             "DAY")

    def test_reduced_qty_placement(self):
        with tempfile.TemporaryDirectory() as tmp:
            kubera = FakeKubera()
            gw = FakeGatewayNew(
                positions={PARENT_CON_ID: parent_position(-3.0)})
            make_executor(tmp, kubera, gw).cycle()
            would = events_of(kubera, "would_place")
            self.assertEqual(len(would), 1)
            detail = would[0][2]
            self.assertEqual(detail["effective_qty"], 3.0)
            self.assertTrue(detail["reduced"])
            self.assertEqual(detail["position_qty"], -3.0)
            self.assertEqual(
                detail["order"]["order"]["totalQuantity"], 3.0)

    def test_duplicate_resting_buy_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            kubera = FakeKubera()
            gw = FakeGatewayNew(open_orders=[buy_order()])
            make_executor(tmp, kubera, gw).cycle()
            would = events_of(kubera, "would_place")
            self.assertEqual(len(would), 1)
            self.assertTrue(would[0][2]["simulate_gate_failed"])
            self.assertEqual(would[0][2]["gate"],
                             "duplicate_open_order")
            self.assertIn("must be cancelled first",
                          would[0][2]["reason"])
            self.assertEqual(gw.whatif_specs, [])
            self.assertEqual(gw.transmit_specs, [])

    def test_bad_tif_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            kubera = FakeKubera(rows=[dict(CLOSE_ROW, tif="WEEK")])
            gw = FakeGatewayNew()
            make_executor(tmp, kubera, gw).cycle()
            would = events_of(kubera, "would_place")
            self.assertEqual(len(would), 1)
            self.assertEqual(would[0][2]["gate"], "bad_tif")

    def test_position_missing_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            kubera = FakeKubera()
            gw = FakeGatewayNew(positions={})
            make_executor(tmp, kubera, gw).cycle()
            would = events_of(kubera, "would_place")
            self.assertEqual(len(would), 1)
            self.assertEqual(would[0][2]["gate"], "position_missing")

    def test_standard_whatif_tolerates_null_figures(self):
        # The STANDARD placement gate, not the cc conclusive
        # variant: a figure-less what-if reply passes here.
        with tempfile.TemporaryDirectory() as tmp:
            kubera = FakeKubera()
            gw = FakeGatewayNew(whatif_reply={
                "ok": True, "init_margin_change": None,
                "maint_margin_change": None,
                "equity_with_loan_change": None, "commission": None})
            make_executor(tmp, kubera, gw).cycle()
            would = events_of(kubera, "would_place")
            self.assertEqual(len(would), 1)
            self.assertNotIn("simulate_gate_failed", would[0][2])

    def test_whatif_margin_breach_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            kubera = FakeKubera()
            gw = FakeGatewayNew(whatif_reply={
                "ok": True, "init_margin_change": 500.0,
                "maint_margin_change": 0.0,
                "equity_with_loan_change": 0.0, "commission": None})
            make_executor(tmp, kubera, gw).cycle()
            would = events_of(kubera, "would_place")
            self.assertEqual(len(would), 1)
            self.assertEqual(would[0][2]["gate"], "whatif")
            self.assertEqual(gw.transmit_specs, [])

    def test_bypass_refused_both_modes(self):
        row = dict(CLOSE_ROW, approval_mode="bypass")
        with tempfile.TemporaryDirectory() as tmp:
            kubera = FakeKubera(rows=[row])
            gw = FakeGatewayNew()
            make_executor(tmp, kubera, gw).cycle()
            would = events_of(kubera, "would_place")
            self.assertEqual(len(would), 1)
            self.assertEqual(would[0][2]["gate"],
                             "bypass_not_supported")
            self.assertEqual(gw.transmit_specs, [])
        with tempfile.TemporaryDirectory() as tmp:
            kubera = FakeKubera(rows=[row])
            gw = FakeGatewayNew()
            make_executor(tmp, kubera, gw, mode="live",
                          live_file=True).cycle()
            failed = events_of(kubera, "gate_failed")
            self.assertEqual(len(failed), 1)
            self.assertEqual(failed[0][2]["gate"],
                             "bypass_not_supported")
            self.assertEqual(gw.transmit_specs, [])

    def test_reads_incomplete_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            kubera = FakeKubera()
            gw = FakeGatewayNew(positions=None)
            ex = make_executor(tmp, kubera, gw)
            ex.process_close_row(dict(CLOSE_ROW), None, [])
            would = events_of(kubera, "would_place")
            self.assertEqual(len(would), 1)
            self.assertEqual(would[0][2]["gate"], "reads_incomplete")

    def test_live_transmits_once_second_cycle_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            kubera, gw = FakeKubera(), FakeGatewayNew()
            ex = make_executor(tmp, kubera, gw, mode="live",
                               live_file=True)
            ex.cycle()
            self.assertEqual(len(gw.transmit_specs), 1)
            spec = gw.transmit_specs[0]
            self.assertFalse(spec["order"]["whatIf"])
            self.assertEqual(spec["order"]["action"], "BUY")
            self.assertEqual(spec["order"]["tif"], "GTC")
            placed = events_of(kubera, "placed")
            self.assertEqual(len(placed), 1)
            self.assertEqual(placed[0][2]["kind"], "close_now")
            rec = ex.tracker.get("close-row-1")
            self.assertIsNotNone(rec)
            self.assertEqual(rec["kind"], "close_now")
            self.assertEqual(ex.tracker.placements_today(), 1)
            ex.cycle()
            self.assertEqual(len(gw.transmit_specs), 1)


class WingFlowTests(unittest.TestCase):
    def test_simulate_resolves_con_id_zero(self):
        with tempfile.TemporaryDirectory() as tmp:
            kubera = FakeKubera(rows=[dict(WING_ROW)])
            gw = FakeGatewayNew()
            make_executor(tmp, kubera, gw).cycle()
            would = events_of(kubera, "would_place")
            self.assertEqual(len(would), 1)
            detail = would[0][2]
            self.assertEqual(detail["kind"], "wing_open")
            self.assertEqual(detail["wing_con_id"], WING_CON_ID)
            self.assertEqual(
                detail["order"]["contract"]["conId"], WING_CON_ID)
            self.assertEqual(detail["order"]["order"]["action"],
                             "BUY")
            self.assertEqual(detail["order"]["order"]["tif"], "GTC")
            self.assertEqual(detail["order"]["order"]["lmtPrice"],
                             0.80)
            # Resolution went through the by-spec read, never the
            # con_id read; nothing transmitted in simulate.
            self.assertEqual(gw.spec_calls,
                             [("IONQ", "2026-10-30", 30.0, "P")])
            self.assertEqual(gw.details_calls, [])
            self.assertEqual(gw.whatif_cc_calls, 0)
            self.assertEqual(gw.transmit_specs, [])

    def test_simulate_given_con_id_verified_not_resolved(self):
        with tempfile.TemporaryDirectory() as tmp:
            kubera = FakeKubera(
                rows=[dict(WING_ROW, con_id=WING_CON_ID)])
            gw = FakeGatewayNew()
            make_executor(tmp, kubera, gw).cycle()
            would = events_of(kubera, "would_place")
            self.assertEqual(len(would), 1)
            self.assertEqual(would[0][2]["wing_con_id"], WING_CON_ID)
            self.assertEqual(gw.details_calls, [WING_CON_ID])
            self.assertEqual(gw.spec_calls, [])

    def test_resolution_not_found_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            kubera = FakeKubera(rows=[dict(WING_ROW)])
            gw = FakeGatewayNew(spec_details=[])
            make_executor(tmp, kubera, gw).cycle()
            would = events_of(kubera, "would_place")
            self.assertEqual(len(would), 1)
            self.assertTrue(would[0][2]["simulate_gate_failed"])
            self.assertEqual(would[0][2]["gate"],
                             "contract_not_found")
            self.assertEqual(gw.transmit_specs, [])

    def test_resolution_multi_match_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            kubera = FakeKubera(rows=[dict(WING_ROW)])
            gw = FakeGatewayNew(spec_details=[
                dict(WING_VERIFIED),
                dict(WING_VERIFIED, con_id=WING_CON_ID + 7)])
            make_executor(tmp, kubera, gw).cycle()
            would = events_of(kubera, "would_place")
            self.assertEqual(len(would), 1)
            self.assertEqual(would[0][2]["gate"],
                             "contract_mismatch")

    def test_rule_violation_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            # Row and verified contract agree on a 40P wing against
            # the 38P parent: identity passes, the RULE gate speaks
            # (a 40P wing is not further OTM than its parent).
            kubera = FakeKubera(rows=[dict(WING_ROW,
                                           con_id=WING_CON_ID,
                                           strike=40.0)])
            gw = FakeGatewayNew(
                details=[dict(WING_VERIFIED, strike=40.0)])
            make_executor(tmp, kubera, gw).cycle()
            would = events_of(kubera, "would_place")
            self.assertEqual(len(would), 1)
            self.assertEqual(would[0][2]["gate"],
                             "wing_rule_violation")

    def test_parent_insufficient_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            kubera = FakeKubera(rows=[dict(WING_ROW)])
            gw = FakeGatewayNew(
                positions={PARENT_CON_ID: parent_position(-3.0)})
            make_executor(tmp, kubera, gw).cycle()
            would = events_of(kubera, "would_place")
            self.assertEqual(len(would), 1)
            self.assertEqual(would[0][2]["gate"],
                             "parent_qty_insufficient")

    def test_parent_missing_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            kubera = FakeKubera(rows=[dict(WING_ROW)])
            gw = FakeGatewayNew(positions={})
            make_executor(tmp, kubera, gw).cycle()
            would = events_of(kubera, "would_place")
            self.assertEqual(len(would), 1)
            self.assertEqual(would[0][2]["gate"],
                             "parent_position_missing")

    def test_duplicate_on_wing_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            kubera = FakeKubera(rows=[dict(WING_ROW)])
            gw = FakeGatewayNew(
                open_orders=[buy_order(con_id=WING_CON_ID)])
            make_executor(tmp, kubera, gw).cycle()
            would = events_of(kubera, "would_place")
            self.assertEqual(len(would), 1)
            self.assertEqual(would[0][2]["gate"],
                             "duplicate_open_order")

    def test_bypass_refused_both_modes(self):
        row = dict(WING_ROW, approval_mode="bypass")
        with tempfile.TemporaryDirectory() as tmp:
            kubera = FakeKubera(rows=[row])
            gw = FakeGatewayNew()
            make_executor(tmp, kubera, gw).cycle()
            would = events_of(kubera, "would_place")
            self.assertEqual(len(would), 1)
            self.assertEqual(would[0][2]["gate"],
                             "bypass_not_supported")
            self.assertEqual(gw.spec_calls, [])
            self.assertEqual(gw.transmit_specs, [])
        with tempfile.TemporaryDirectory() as tmp:
            kubera = FakeKubera(rows=[row])
            gw = FakeGatewayNew()
            make_executor(tmp, kubera, gw, mode="live",
                          live_file=True).cycle()
            failed = events_of(kubera, "gate_failed")
            self.assertEqual(len(failed), 1)
            self.assertEqual(failed[0][2]["gate"],
                             "bypass_not_supported")
            self.assertEqual(gw.transmit_specs, [])

    def test_reads_incomplete_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            kubera = FakeKubera(rows=[dict(WING_ROW)])
            gw = FakeGatewayNew(positions=None)
            ex = make_executor(tmp, kubera, gw)
            ex.process_wing_row(dict(WING_ROW), None, [])
            would = events_of(kubera, "would_place")
            self.assertEqual(len(would), 1)
            self.assertEqual(would[0][2]["gate"], "reads_incomplete")

    def test_live_transmits_once_second_cycle_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            kubera = FakeKubera(rows=[dict(WING_ROW)])
            gw = FakeGatewayNew()
            ex = make_executor(tmp, kubera, gw, mode="live",
                               live_file=True)
            ex.cycle()
            self.assertEqual(len(gw.transmit_specs), 1)
            spec = gw.transmit_specs[0]
            self.assertFalse(spec["order"]["whatIf"])
            self.assertEqual(spec["contract"]["conId"], WING_CON_ID)
            placed = events_of(kubera, "placed")
            self.assertEqual(len(placed), 1)
            self.assertEqual(placed[0][2]["kind"], "wing_open")
            self.assertEqual(placed[0][2]["wing_con_id"], WING_CON_ID)
            rec = ex.tracker.get("wing-row-1")
            self.assertIsNotNone(rec)
            self.assertEqual(rec["kind"], "wing_open")
            self.assertEqual(ex.tracker.placements_today(), 1)
            ex.cycle()
            self.assertEqual(len(gw.transmit_specs), 1)


class NormaliseV050Tests(unittest.TestCase):
    def test_new_fields_carried(self):
        raw = {
            "id": "w1", "account_id": "U19377323", "con_id": 0,
            "symbol": "IONQ", "expiry": "2026-10-30", "strike": 30,
            "option_type": "Put", "qty": 5, "target_limit": 0.8,
            "tif": "day", "kind": "wing_open",
            "parent_con_id": PARENT_CON_ID, "parent_symbol": "IONQ",
            "parent_expiry": "2026-10-30", "parent_strike": 38,
            "parent_option_type": "Put", "parent_quantity": 5,
        }
        row = setu_exec.normalise_row(raw)
        self.assertEqual(row["quantity"], 5.0)   # from "qty"
        self.assertEqual(row["tif"], "DAY")      # upper-cased
        self.assertEqual(row["parent_con_id"], PARENT_CON_ID)
        self.assertEqual(row["parent_symbol"], "IONQ")
        self.assertEqual(row["parent_strike"], 38.0)
        self.assertEqual(row["parent_option_type"], "Put")
        self.assertEqual(row["parent_quantity"], 5.0)


# ---------------------------------------------------------------------------
# Gateway-level: the by-spec details read
# ---------------------------------------------------------------------------

class StubSpecClient:
    """EClient double answering reqContractDetails for a by-spec
    request: records the request contract and replies with one
    detail carrying a real con_id (or fails / stays silent)."""

    def __init__(self, wrapper, mode="ok", resolved=WING_CON_ID):
        self.wrapper = wrapper
        self.mode = mode
        self.resolved = resolved
        self.calls = []

    def reqContractDetails(self, req_id, contract):
        self.calls.append(contract)
        if self.mode == "silent":
            return
        if self.mode == "error":
            self.wrapper.error(req_id, 162,
                               "Historical Market Data Service error", "")
            return
        detail = SimpleNamespace(
            contract=SimpleNamespace(
                conId=self.resolved, symbol="IONQ", secType="OPT",
                lastTradeDateOrContractMonth="20261030", strike=30.0,
                right="P", multiplier="100"),
            minTick=0.01)
        self.wrapper.contractDetails(req_id, detail)
        self.wrapper.contractDetailsEnd(req_id)


class GatewayBySpecDetailsTests(unittest.TestCase):
    def _gateway(self, tmp, mode="ok"):
        gw = setu_exec.ExecGateway(make_cfg(tmp))
        stub = StubSpecClient(gw.wrapper, mode=mode)
        gw.client = stub
        return gw, stub

    def test_by_spec_read_requests_and_parses(self):
        with tempfile.TemporaryDirectory() as tmp:
            gw, stub = self._gateway(tmp)
            details = gw.fetch_contract_details_by_spec(
                "IONQ", "2026-10-30", 30.0, "P")
            self.assertEqual(len(details), 1)
            self.assertEqual(details[0]["con_id"], WING_CON_ID)
            self.assertEqual(details[0]["strike"], 30.0)
            self.assertEqual(details[0]["right"], "P")
            sent = stub.calls[0]
            self.assertEqual(sent.conId, 0)
            self.assertEqual(sent.symbol, "IONQ")
            self.assertEqual(sent.secType, "OPT")
            self.assertEqual(sent.exchange, "SMART")
            self.assertEqual(sent.lastTradeDateOrContractMonth,
                             "20261030")
            self.assertEqual(sent.strike, 30.0)
            self.assertEqual(sent.right, "P")

    def test_by_spec_error_returns_none_without_poisoning(self):
        with tempfile.TemporaryDirectory() as tmp:
            gw, _ = self._gateway(tmp, mode="error")
            self.assertIsNone(gw.fetch_contract_details_by_spec(
                "IONQ", "2026-10-30", 30.0, "P"))
            self.assertIsNone(gw.read_error)

    def test_by_spec_timeout_returns_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            gw, _ = self._gateway(tmp, mode="silent")
            self.assertIsNone(gw.fetch_contract_details_by_spec(
                "IONQ", "2026-10-30", 30.0, "P", timeout=0.2))


if __name__ == "__main__":
    unittest.main()
