"""Tests for setu-exec v0.4 covered-call open (cc_open) rows.

Synthetic data only, with fake Kubera/Gateway doubles in the same
style as tests/test_exec_gates.py. No real Gateway, no real orders.

Run: python3 -m unittest discover tests
"""
import copy
import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import setu_exec  # noqa: E402

CC_CON_ID = 932592066
STK_CON_ID = 37040  # the underlying stock's own con_id (a placeholder)

CC_ROW = {
    "id": "cc-row-1",
    "account_id": "U19377323",
    "con_id": CC_CON_ID,
    "symbol": "MRVL",
    "local_symbol": "MRVL  261127C00295000",
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

VERIFIED = {
    "con_id": CC_CON_ID,
    "symbol": "MRVL",
    "sec_type": "OPT",
    "expiry": "20261127",
    "strike": 295.0,
    "right": "C",
    "multiplier": "100",
    "min_tick": 0.01,
}


def stock_position(shares=400.0):
    return {"con_id": STK_CON_ID, "symbol": "MRVL", "sec_type": "STK",
            "right": "", "quantity": shares, "avg_cost": 254.23}


def short_call_position(qty=-1.0, con_id=555001):
    return {"con_id": con_id, "symbol": "MRVL", "sec_type": "OPT",
            "right": "C", "quantity": qty, "avg_cost": 3.10}


def base_positions():
    return {STK_CON_ID: stock_position()}


def sell_call_order(qty=1.0, con_id=CC_CON_ID, remaining=None, **over):
    order = {
        "con_id": con_id,
        "symbol": "MRVL",
        "expiry": "20261127",
        "strike": 295.0,
        "right": "C",
        "action": "SELL",
        "total_quantity": qty,
        "limit_price": 5.50,
        "order_ref": "",
        "status": "Submitted",
        "perm_id": 424242,
        "order_id": 8811,
        "filled": 0.0,
        "remaining": qty if remaining is None else remaining,
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
        self.rows = rows if rows is not None else [dict(CC_ROW)]
        self.kill = kill
        self.events = []

    def get_control(self):
        return {"account_id": "U19377323", "kill_switch": self.kill}

    def poll_approved(self):
        return [dict(r) for r in self.rows]

    def post_event(self, row_id, kind, detail):
        self.events.append((row_id, kind, detail))
        return {"status": "success"}


class FakeGatewayCC:
    """Serves both pipelines: positions is a con_id-keyed dict,
    contract details default to the one verified MRVL contract."""

    def __init__(self, positions="default", open_orders=None,
                 details="default", whatif_reply=None,
                 transmit_result=None):
        if positions == "default":
            positions = base_positions()
        self.positions_map = positions  # None means: reads fail
        self.open_orders = open_orders if open_orders is not None else []
        self.details = ([dict(VERIFIED)] if details == "default"
                        else details)
        self.whatif_reply = whatif_reply or {
            "ok": True, "init_margin_change": -120.0,
            "maint_margin_change": -60.0, "equity_with_loan_change": 0.0,
            "commission": None}
        self.transmit_result = transmit_result or {
            "order_id": 5001, "perm_id": 777, "status": "PreSubmitted",
            "error": None, "avg_fill_price": None}
        self.last_status_by_perm = {}
        self.last_fill_by_perm = {}
        self.whatif_specs = []
        self.transmit_specs = []
        self.details_calls = []

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

    def whatif(self, spec):
        self.whatif_specs.append(copy.deepcopy(spec))
        return dict(self.whatif_reply)

    def whatif_cc(self, spec):
        # v0.4.1: the cc path's conclusive what-if. The fake serves
        # the same canned reply (which may carry late_error /
        # figure-less shapes for the refusal tests).
        self.whatif_specs.append(copy.deepcopy(spec))
        return dict(self.whatif_reply)

    def transmit(self, spec):
        self.transmit_specs.append(copy.deepcopy(spec))
        return dict(self.transmit_result)


def make_executor(tmp, kubera=None, gateway=None, mode="simulate",
                  live_file=False):
    cfg = make_cfg(tmp, mode=mode, live_file=live_file)
    arm(cfg)
    return setu_exec.Executor(cfg, kubera or FakeKubera(),
                              gateway or FakeGatewayCC())


def events_of(kubera, kind=None):
    return [(r, k, d) for (r, k, d) in kubera.events
            if kind is None or k == kind]


class CcContractGateTests(unittest.TestCase):
    def test_exact_match_passes(self):
        ok, detail = setu_exec.gate_cc_contract(CC_ROW, [dict(VERIFIED)])
        self.assertTrue(ok)
        self.assertEqual(detail["verified"]["con_id"], CC_CON_ID)

    def test_failed_read_is_not_found(self):
        ok, detail = setu_exec.gate_cc_contract(CC_ROW, None)
        self.assertFalse(ok)
        self.assertEqual(detail["gate"], "contract_not_found")

    def test_empty_reply_is_not_found(self):
        ok, detail = setu_exec.gate_cc_contract(CC_ROW, [])
        self.assertFalse(ok)
        self.assertEqual(detail["gate"], "contract_not_found")

    def test_two_contracts_is_mismatch(self):
        ok, detail = setu_exec.gate_cc_contract(
            CC_ROW, [dict(VERIFIED), dict(VERIFIED)])
        self.assertFalse(ok)
        self.assertEqual(detail["gate"], "contract_mismatch")

    def test_strike_mismatch(self):
        bad = dict(VERIFIED, strike=300.0)
        ok, detail = setu_exec.gate_cc_contract(CC_ROW, [bad])
        self.assertFalse(ok)
        self.assertEqual(detail["gate"], "contract_mismatch")
        self.assertIn("strike", detail["reason"])

    def test_right_mismatch(self):
        bad = dict(VERIFIED, right="P")
        ok, detail = setu_exec.gate_cc_contract(CC_ROW, [bad])
        self.assertFalse(ok)
        self.assertEqual(detail["gate"], "contract_mismatch")

    def test_symbol_and_expiry_mismatch(self):
        bad = dict(VERIFIED, symbol="XXXX", expiry="20261218")
        ok, detail = setu_exec.gate_cc_contract(CC_ROW, [bad])
        self.assertFalse(ok)
        self.assertEqual(detail["gate"], "contract_mismatch")


class CcCoverGateTests(unittest.TestCase):
    def cover(self, row=None, positions="default", orders=None):
        if positions == "default":
            positions = base_positions()
        return setu_exec.gate_cc_cover(
            row or dict(CC_ROW), positions, orders or [], 100.0)

    def test_full_cover_exactly(self):
        ok, detail = self.cover()
        self.assertTrue(ok)
        self.assertEqual(detail["required_shares"], 400.0)
        self.assertEqual(detail["long_shares"], 400.0)

    def test_five_contracts_refused_with_numbers(self):
        ok, detail = self.cover(row=dict(CC_ROW, quantity=5.0))
        self.assertFalse(ok)
        self.assertEqual(detail["gate"], "insufficient_cover")
        self.assertIn("500", detail["reason"])
        self.assertIn("400", detail["reason"])

    def test_resting_sell_call_commits_shares(self):
        orders = [sell_call_order(qty=1.0)]
        ok, detail = self.cover(orders=orders)
        self.assertFalse(ok)  # 4 new + 1 resting = 500 > 400
        self.assertEqual(detail["gate"], "insufficient_cover")
        ok, detail = self.cover(row=dict(CC_ROW, quantity=3.0),
                                orders=orders)
        self.assertTrue(ok)  # 3 + 1 = 400, exactly covered

    def test_short_call_position_commits_shares(self):
        positions = base_positions()
        positions[555001] = short_call_position(-1.0)
        ok, detail = self.cover(positions=positions)
        self.assertFalse(ok)
        self.assertEqual(detail["short_call_positions"], 1.0)

    def test_resting_and_position_stack(self):
        positions = base_positions()
        positions[555001] = short_call_position(-1.0)
        orders = [sell_call_order(qty=1.0)]
        ok, _ = self.cover(row=dict(CC_ROW, quantity=2.0),
                           positions=positions, orders=orders)
        self.assertTrue(ok)  # 2 + 1 + 1 = 400
        ok, detail = self.cover(row=dict(CC_ROW, quantity=3.0),
                                positions=positions, orders=orders)
        self.assertFalse(ok)  # 3 + 1 + 1 = 500

    def test_unrelated_orders_do_not_commit(self):
        orders = [sell_call_order(con_id=999, symbol="AAPL"),
                  sell_call_order(right="P"),
                  sell_call_order(status="Filled")]
        ok, detail = self.cover(orders=orders)
        self.assertTrue(ok)
        self.assertEqual(detail["resting_sell_calls"], 0.0)

    def test_no_shares_refuses(self):
        ok, detail = self.cover(positions={})
        self.assertFalse(ok)
        self.assertEqual(detail["gate"], "insufficient_cover")
        self.assertEqual(detail["long_shares"], 0.0)


class CcDuplicateGateTests(unittest.TestCase):
    def test_working_sell_on_con_id_blocks(self):
        ok, detail = setu_exec.gate_cc_duplicate(
            CC_ROW, [sell_call_order()])
        self.assertFalse(ok)
        self.assertEqual(detail["gate"], "duplicate_open_order")

    def test_sell_on_other_contract_ok(self):
        ok, _ = setu_exec.gate_cc_duplicate(
            CC_ROW, [sell_call_order(con_id=999888)])
        self.assertTrue(ok)

    def test_buy_on_same_contract_ok(self):
        ok, _ = setu_exec.gate_cc_duplicate(
            CC_ROW, [sell_call_order(action="BUY")])
        self.assertTrue(ok)

    def test_order_ref_blocks(self):
        ok, detail = setu_exec.gate_cc_duplicate(
            CC_ROW, [sell_call_order(con_id=999888,
                                     order_ref="cc-row-1")])
        self.assertFalse(ok)
        self.assertEqual(detail["gate"], "duplicate_open_order")


class CcPriceGateTests(unittest.TestCase):
    def test_on_tick_limit_unchanged(self):
        ok, detail = setu_exec.gate_cc_price(CC_ROW, 0.01)
        self.assertTrue(ok)
        self.assertEqual(detail["limit"], 6.00)
        self.assertFalse(detail["limit_rounded"])

    def test_off_tick_limit_rounded_to_min_tick(self):
        row = dict(CC_ROW, target_limit=6.003)
        ok, detail = setu_exec.gate_cc_price(row, 0.01)
        self.assertTrue(ok)
        self.assertEqual(detail["limit"], 6.00)
        self.assertTrue(detail["limit_rounded"])
        row = dict(CC_ROW, target_limit=6.03)
        ok, detail = setu_exec.gate_cc_price(row, 0.05)
        self.assertEqual(detail["limit"], 6.05)

    def test_missing_tick_falls_back_to_cent(self):
        ok, detail = setu_exec.gate_cc_price(CC_ROW, None)
        self.assertTrue(ok)
        self.assertEqual(detail["limit"], 6.00)

    def test_non_positive_refused(self):
        for bad in (0.0, -1.0, None):
            ok, detail = setu_exec.gate_cc_price(
                dict(CC_ROW, target_limit=bad), 0.01)
            self.assertFalse(ok)
            self.assertEqual(detail["gate"], "price")


class CcSpecTests(unittest.TestCase):
    def test_frozen_shape(self):
        spec = setu_exec.build_cc_order_spec(CC_ROW, 6.00, what_if=False)
        self.assertEqual(spec["contract"]["right"], "C")
        self.assertEqual(spec["contract"]["secType"], "OPT")
        self.assertEqual(spec["contract"]["exchange"], "SMART")
        self.assertEqual(spec["contract"]["currency"], "USD")
        self.assertEqual(spec["contract"]["multiplier"], "100")
        self.assertEqual(spec["order"]["action"], "SELL")
        self.assertEqual(spec["order"]["orderType"], "LMT")
        self.assertEqual(spec["order"]["tif"], "GTC")
        self.assertFalse(spec["order"]["outsideRth"])
        self.assertEqual(spec["order"]["totalQuantity"], 4.0)
        self.assertEqual(spec["order"]["lmtPrice"], 6.00)
        self.assertEqual(spec["order"]["orderRef"], "cc-row-1")
        self.assertFalse(spec["order"]["whatIf"])


class CcFlowTests(unittest.TestCase):
    def test_simulate_would_place_frozen_shape(self):
        with tempfile.TemporaryDirectory() as tmp:
            kubera, gw = FakeKubera(), FakeGatewayCC()
            make_executor(tmp, kubera, gw).cycle()
            would = events_of(kubera, "would_place")
            self.assertEqual(len(would), 1)
            detail = would[0][2]
            self.assertEqual(detail["kind"], "cc_open")
            order = detail["order"]["order"]
            contract = detail["order"]["contract"]
            self.assertEqual(order["action"], "SELL")
            self.assertEqual(order["orderType"], "LMT")
            self.assertEqual(order["tif"], "GTC")
            self.assertEqual(order["lmtPrice"], 6.00)
            self.assertEqual(order["orderRef"], "cc-row-1")
            self.assertEqual(contract["right"], "C")
            # The preview that reached the Gateway was what-if only,
            # and nothing was ever transmitted.
            self.assertEqual(len(gw.whatif_specs), 1)
            self.assertTrue(gw.whatif_specs[0]["order"]["whatIf"])
            self.assertEqual(gw.transmit_specs, [])
            self.assertEqual(gw.details_calls, [CC_CON_ID])

    def test_unknown_kind_ignored_silently(self):
        with tempfile.TemporaryDirectory() as tmp:
            row = dict(CC_ROW, id="future-row", kind="some_future_kind")
            kubera = FakeKubera(rows=[row])
            gw = FakeGatewayCC()
            make_executor(tmp, kubera, gw).cycle()
            self.assertEqual(kubera.events, [])
            self.assertEqual(gw.whatif_specs, [])
            self.assertEqual(gw.transmit_specs, [])
            self.assertEqual(gw.details_calls, [])

    def test_absent_kind_still_gtc_close(self):
        gtc_row = {
            "id": "gtc-row-1", "account_id": "U19377323",
            "con_id": 843250654, "symbol": "ASTS", "local_symbol": "",
            "expiry": "2026-11-20", "strike": 50.0, "option_type": "Put",
            "quantity": 3.0, "premium": 6.28, "target_limit": 2.51,
            "action": "place", "approval_mode": "manual",
        }  # no "kind" key at all, as pre-v0.4 Kubera rows arrive
        positions = {843250654: {
            "con_id": 843250654, "symbol": "ASTS", "sec_type": "OPT",
            "right": "P", "quantity": -5.0, "avg_cost": 6.28}}
        with tempfile.TemporaryDirectory() as tmp:
            kubera = FakeKubera(rows=[gtc_row])
            gw = FakeGatewayCC(positions=positions)
            make_executor(tmp, kubera, gw).cycle()
            would = events_of(kubera, "would_place")
            self.assertEqual(len(would), 1)
            self.assertEqual(would[0][2]["order"]["order"]["action"],
                             "BUY")
            self.assertEqual(gw.details_calls, [])  # gtc never verifies

    def test_bypass_stamped_cc_row_refused_simulate(self):
        with tempfile.TemporaryDirectory() as tmp:
            kubera = FakeKubera(rows=[dict(CC_ROW,
                                           approval_mode="bypass")])
            gw = FakeGatewayCC()
            make_executor(tmp, kubera, gw).cycle()
            would = events_of(kubera, "would_place")
            self.assertEqual(len(would), 1)
            self.assertTrue(would[0][2]["simulate_gate_failed"])
            self.assertEqual(would[0][2]["gate"],
                             "cc_bypass_not_supported")
            self.assertEqual(gw.details_calls, [])
            self.assertEqual(gw.transmit_specs, [])

    def test_bypass_stamped_cc_row_refused_live(self):
        with tempfile.TemporaryDirectory() as tmp:
            kubera = FakeKubera(rows=[dict(CC_ROW,
                                           approval_mode="bypass")])
            gw = FakeGatewayCC()
            make_executor(tmp, kubera, gw, mode="live",
                          live_file=True).cycle()
            failed = events_of(kubera, "gate_failed")
            self.assertEqual(len(failed), 1)
            self.assertEqual(failed[0][2]["gate"],
                             "cc_bypass_not_supported")
            self.assertEqual(gw.transmit_specs, [])

    def test_insufficient_cover_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            kubera = FakeKubera(rows=[dict(CC_ROW, quantity=5.0)])
            gw = FakeGatewayCC()
            make_executor(tmp, kubera, gw).cycle()
            would = events_of(kubera, "would_place")
            self.assertEqual(len(would), 1)
            self.assertTrue(would[0][2]["simulate_gate_failed"])
            self.assertEqual(would[0][2]["gate"], "insufficient_cover")
            self.assertEqual(gw.whatif_specs, [])
            self.assertEqual(gw.transmit_specs, [])

    def test_duplicate_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            kubera = FakeKubera()
            positions = base_positions()
            # 300 shares so cover passes and the duplicate gate is
            # the one that fires (a resting sell call also commits).
            positions[STK_CON_ID] = stock_position(500.0)
            gw = FakeGatewayCC(positions=positions,
                               open_orders=[sell_call_order()])
            make_executor(tmp, kubera, gw).cycle()
            would = events_of(kubera, "would_place")
            self.assertEqual(len(would), 1)
            self.assertEqual(would[0][2]["gate"], "duplicate_open_order")

    def test_contract_mismatch_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            kubera = FakeKubera()
            gw = FakeGatewayCC(details=[dict(VERIFIED, strike=300.0)])
            make_executor(tmp, kubera, gw).cycle()
            would = events_of(kubera, "would_place")
            self.assertEqual(len(would), 1)
            self.assertEqual(would[0][2]["gate"], "contract_mismatch")

    def test_contract_not_found_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            kubera = FakeKubera()
            gw = FakeGatewayCC(details=None)
            make_executor(tmp, kubera, gw).cycle()
            would = events_of(kubera, "would_place")
            self.assertEqual(len(would), 1)
            self.assertEqual(would[0][2]["gate"], "contract_not_found")

    def test_margin_impact_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            kubera = FakeKubera()
            gw = FakeGatewayCC(whatif_reply={
                "ok": True, "init_margin_change": 500.0,
                "maint_margin_change": 0.0,
                "equity_with_loan_change": 0.0, "commission": None})
            make_executor(tmp, kubera, gw).cycle()
            would = events_of(kubera, "would_place")
            self.assertEqual(len(would), 1)
            self.assertEqual(would[0][2]["gate"], "margin_impact")
            self.assertEqual(gw.transmit_specs, [])

    def test_reads_incomplete_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            kubera = FakeKubera()
            gw = FakeGatewayCC(positions=None)
            ex = make_executor(tmp, kubera, gw)
            # cycle() would abort wholesale on None reads; the row
            # path itself must also refuse partial data.
            ex.process_cc_row(dict(CC_ROW), None, [])
            would = events_of(kubera, "would_place")
            self.assertEqual(len(would), 1)
            self.assertEqual(would[0][2]["gate"], "reads_incomplete")


NULL_FIGURES_REPLY = {
    "ok": True, "init_margin_change": None, "maint_margin_change": None,
    "equity_with_loan_change": None, "commission": None}

BROKER_201 = ("IB error 201: YOUR ORDER IS NOT ACCEPTED. IN ORDER TO "
              "OBTAIN THE DESIRED POSITION YOUR EQUITY WITH LOAN VALUE "
              "[546819.78 USD] MUST EXCEED THE INITIAL MARGIN "
              "[559501.74 USD]")


class CcWhatifGateFunctionTests(unittest.TestCase):
    """gate_cc_whatif (v0.4.1): conclusive or refused."""

    def test_failed_reply_is_margin_impact(self):
        ok, detail = setu_exec.gate_cc_whatif(
            {"ok": False, "error": "IB error 321: bad contract"})
        self.assertFalse(ok)
        self.assertEqual(detail["gate"], "margin_impact")
        self.assertIn("what-if failed", detail["reason"])

    def test_null_init_margin_is_inconclusive(self):
        ok, detail = setu_exec.gate_cc_whatif(dict(NULL_FIGURES_REPLY))
        self.assertFalse(ok)
        self.assertEqual(detail["gate"], "whatif_inconclusive")
        self.assertIn("init_margin_change=None", detail["reason"])

    def test_late_error_is_margin_impact_verbatim(self):
        reply = {"ok": True, "init_margin_change": -120.0,
                 "maint_margin_change": -60.0,
                 "equity_with_loan_change": 0.0, "commission": None,
                 "late_error": BROKER_201, "late_error_code": 201}
        ok, detail = setu_exec.gate_cc_whatif(reply)
        self.assertFalse(ok)
        self.assertEqual(detail["gate"], "margin_impact")
        self.assertEqual(detail["reason"], BROKER_201)

    def test_over_tolerance_is_margin_impact(self):
        ok, detail = setu_exec.gate_cc_whatif(
            {"ok": True, "init_margin_change": 500.0,
             "maint_margin_change": 0.0, "equity_with_loan_change": 0.0,
             "commission": None, "late_error": None})
        self.assertFalse(ok)
        self.assertEqual(detail["gate"], "margin_impact")

    def test_concrete_within_tolerance_quiet_grace_passes(self):
        reply = {"ok": True, "init_margin_change": -120.0,
                 "maint_margin_change": -60.0,
                 "equity_with_loan_change": 0.0, "commission": None,
                 "late_error": None, "late_error_code": None}
        ok, detail = setu_exec.gate_cc_whatif(reply)
        self.assertTrue(ok)
        self.assertEqual(detail["whatif"], reply)


class CcWhatifProcessTests(unittest.TestCase):
    """The executor's cc path on the new what-if shapes, both modes."""

    def test_null_margin_whatif_refused_simulate(self):
        with tempfile.TemporaryDirectory() as tmp:
            kubera = FakeKubera()
            gw = FakeGatewayCC(whatif_reply=dict(NULL_FIGURES_REPLY))
            make_executor(tmp, kubera, gw).cycle()
            would = events_of(kubera, "would_place")
            self.assertEqual(len(would), 1)
            self.assertTrue(would[0][2]["simulate_gate_failed"])
            self.assertEqual(would[0][2]["gate"], "whatif_inconclusive")
            self.assertEqual(gw.transmit_specs, [])

    def test_null_margin_whatif_refused_live(self):
        with tempfile.TemporaryDirectory() as tmp:
            kubera = FakeKubera()
            gw = FakeGatewayCC(whatif_reply=dict(NULL_FIGURES_REPLY))
            make_executor(tmp, kubera, gw, mode="live",
                          live_file=True).cycle()
            failed = events_of(kubera, "gate_failed")
            self.assertEqual(len(failed), 1)
            self.assertEqual(failed[0][2]["gate"], "whatif_inconclusive")
            self.assertEqual(gw.transmit_specs, [])

    def test_late_201_refused_simulate(self):
        with tempfile.TemporaryDirectory() as tmp:
            kubera = FakeKubera()
            gw = FakeGatewayCC(whatif_reply={
                "ok": True, "init_margin_change": -120.0,
                "maint_margin_change": -60.0,
                "equity_with_loan_change": 0.0, "commission": None,
                "late_error": BROKER_201, "late_error_code": 201})
            make_executor(tmp, kubera, gw).cycle()
            would = events_of(kubera, "would_place")
            self.assertEqual(len(would), 1)
            self.assertTrue(would[0][2]["simulate_gate_failed"])
            self.assertEqual(would[0][2]["gate"], "margin_impact")
            self.assertIn("YOUR ORDER IS NOT ACCEPTED",
                          would[0][2]["reason"])
            self.assertEqual(gw.transmit_specs, [])

    def test_late_201_refused_live(self):
        with tempfile.TemporaryDirectory() as tmp:
            kubera = FakeKubera()
            gw = FakeGatewayCC(whatif_reply={
                "ok": True, "init_margin_change": -120.0,
                "maint_margin_change": -60.0,
                "equity_with_loan_change": 0.0, "commission": None,
                "late_error": BROKER_201, "late_error_code": 201})
            make_executor(tmp, kubera, gw, mode="live",
                          live_file=True).cycle()
            failed = events_of(kubera, "gate_failed")
            self.assertEqual(len(failed), 1)
            self.assertEqual(failed[0][2]["gate"], "margin_impact")
            self.assertIn("YOUR ORDER IS NOT ACCEPTED",
                          failed[0][2]["reason"])
            self.assertEqual(gw.transmit_specs, [])

    def test_concrete_quiet_whatif_still_would_place(self):
        with tempfile.TemporaryDirectory() as tmp:
            kubera = FakeKubera()
            gw = FakeGatewayCC(whatif_reply={
                "ok": True, "init_margin_change": -120.0,
                "maint_margin_change": -60.0,
                "equity_with_loan_change": 0.0, "commission": None,
                "late_error": None, "late_error_code": None})
            make_executor(tmp, kubera, gw).cycle()
            would = events_of(kubera, "would_place")
            self.assertEqual(len(would), 1)
            self.assertNotIn("simulate_gate_failed", would[0][2])
            self.assertEqual(gw.transmit_specs, [])


class CcLiveFillTests(unittest.TestCase):
    def _place_live(self, tmp):
        kubera, gw = FakeKubera(), FakeGatewayCC()
        ex = make_executor(tmp, kubera, gw, mode="live", live_file=True)
        ex.cycle()
        return ex, kubera, gw

    def test_live_place_transmits_once_and_tracks(self):
        with tempfile.TemporaryDirectory() as tmp:
            ex, kubera, gw = self._place_live(tmp)
            self.assertEqual(len(gw.transmit_specs), 1)
            spec = gw.transmit_specs[0]
            self.assertFalse(spec["order"]["whatIf"])
            self.assertEqual(spec["order"]["action"], "SELL")
            placed = events_of(kubera, "placed")
            self.assertEqual(len(placed), 1)
            self.assertEqual(placed[0][2]["kind"], "cc_open")
            rec = ex.tracker.get("cc-row-1")
            self.assertIsNotNone(rec)
            self.assertEqual(rec["kind"], "cc_open")
            self.assertEqual(ex.tracker.placements_today(), 1)
            # A second cycle with the row still APPROVED places
            # nothing more: one row, one live order, ever.
            ex.cycle()
            self.assertEqual(len(gw.transmit_specs), 1)

    def test_partial_then_complete_fill(self):
        with tempfile.TemporaryDirectory() as tmp:
            ex, kubera, gw = self._place_live(tmp)
            live = sell_call_order(qty=4.0, order_ref="cc-row-1",
                                   perm_id=777, filled=2.0, remaining=2.0)
            gw.last_fill_by_perm = {777: {"filled": 2.0, "remaining": 2.0,
                                          "avg_fill_price": 6.10}}
            kubera.events.clear()
            ex.reconcile([live])
            placed = events_of(kubera, "placed")
            self.assertEqual(len(placed), 1)
            self.assertEqual(placed[0][2]["filled_qty"], 2.0)
            self.assertEqual(placed[0][2]["avg_fill_price"], 6.10)
            self.assertIsNotNone(ex.tracker.get("cc-row-1"))
            # The fill completes; the order leaves the open list
            # and the sweep's terminal status carries it home.
            gw.last_status_by_perm = {777: "Filled"}
            kubera.events.clear()
            ex.reconcile([])
            filled = events_of(kubera, "filled")
            self.assertEqual(len(filled), 1)
            self.assertEqual(filled[0][2]["filled_qty"], 4.0)
            self.assertEqual(filled[0][2]["avg_fill_price"], 6.10)
            self.assertIsNone(ex.tracker.get("cc-row-1"))


class StubContractClient:
    """EClient double answering reqContractDetails synchronously
    through the real wrapper (or failing it, or staying silent)."""

    def __init__(self, wrapper, mode="ok"):
        self.wrapper = wrapper
        self.mode = mode
        self.calls = []

    def reqContractDetails(self, req_id, contract):
        self.calls.append((req_id, contract.conId))
        if self.mode == "silent":
            return
        if self.mode == "error":
            self.wrapper.error(req_id, 162,
                               "Historical Market Data Service error", "")
            return
        detail = SimpleNamespace(
            contract=SimpleNamespace(
                conId=contract.conId, symbol="MRVL", secType="OPT",
                lastTradeDateOrContractMonth="20261127", strike=295.0,
                right="C", multiplier="100"),
            minTick=0.01)
        self.wrapper.contractDetails(req_id, detail)
        self.wrapper.contractDetailsEnd(req_id)


class GatewayContractDetailsTests(unittest.TestCase):
    def _gateway(self, tmp, mode="ok"):
        gw = setu_exec.ExecGateway(make_cfg(tmp))
        stub = StubContractClient(gw.wrapper, mode=mode)
        gw.client = stub
        return gw, stub

    def test_details_parsed(self):
        with tempfile.TemporaryDirectory() as tmp:
            gw, stub = self._gateway(tmp)
            details = gw.fetch_contract_details(CC_CON_ID)
            self.assertEqual(len(details), 1)
            d = details[0]
            self.assertEqual(d["symbol"], "MRVL")
            self.assertEqual(d["expiry"], "20261127")
            self.assertEqual(d["strike"], 295.0)
            self.assertEqual(d["right"], "C")
            self.assertEqual(d["min_tick"], 0.01)
            self.assertEqual(stub.calls[0][1], CC_CON_ID)

    def test_error_returns_none_without_poisoning_reads(self):
        with tempfile.TemporaryDirectory() as tmp:
            gw, _ = self._gateway(tmp, mode="error")
            self.assertIsNone(gw.fetch_contract_details(CC_CON_ID))
            self.assertIsNone(gw.read_error)

    def test_timeout_returns_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            gw, _ = self._gateway(tmp, mode="silent")
            self.assertIsNone(
                gw.fetch_contract_details(CC_CON_ID, timeout=0.2))


def _whatif_state(init="150.25", maint="75.0", ewl="0.0", comm="1.30"):
    return SimpleNamespace(status="PreSubmitted",
                           initMarginChange=init, maintMarginChange=maint,
                           equityWithLoanChange=ewl, commission=comm)


class StubWhatifClient:
    """EClient double for whatif_cc: answers placeOrder through
    the real wrapper with a scripted what-if state, optionally
    firing a trailing error on a timer (tied to the placed order
    id, or to a different id)."""

    def __init__(self, wrapper, state=None, late=None):
        self.wrapper = wrapper
        self.state = state if state is not None else _whatif_state()
        # late = (delay_seconds, "same"|"other", code, message)
        self.late = late
        self.placed = []

    def placeOrder(self, order_id, contract, order):
        self.placed.append(order_id)
        self.wrapper.openOrder(order_id, contract, order, self.state)
        if self.late is not None:
            delay, which, code, message = self.late
            target = order_id if which == "same" else order_id + 999
            timer = threading.Timer(
                delay, lambda: self.wrapper.error(target, code,
                                                  message, ""))
            timer.daemon = True
            timer.start()


class GatewayCcWhatifTests(unittest.TestCase):
    """whatif_cc (v0.4.1) against the real wrapper: the trailing
    grace captures a refusal tied to the what-if order id — and
    only that id."""

    def _gateway(self, tmp, client):
        gw = setu_exec.ExecGateway(make_cfg(tmp))
        gw.client = client(gw.wrapper)
        return gw

    def _spec(self):
        return setu_exec.build_cc_order_spec(CC_ROW, 6.00, what_if=True)

    def test_quiet_grace_concrete_figures_pass(self):
        with tempfile.TemporaryDirectory() as tmp:
            gw = self._gateway(tmp, lambda w: StubWhatifClient(w))
            start = time.monotonic()
            reply = gw.whatif_cc(self._spec(), grace=0.3)
            elapsed = time.monotonic() - start
            self.assertTrue(reply["ok"])
            self.assertEqual(reply["init_margin_change"], 150.25)
            self.assertIsNone(reply["late_error"])
            # The grace window was actually held.
            self.assertGreaterEqual(elapsed, 0.25)

    def test_tied_201_inside_grace_is_captured(self):
        with tempfile.TemporaryDirectory() as tmp:
            late = (0.2, "same", 201,
                    "YOUR ORDER IS NOT ACCEPTED. IN ORDER TO OBTAIN "
                    "THE DESIRED POSITION YOUR EQUITY WITH LOAN VALUE "
                    "[546819.78 USD] MUST EXCEED THE INITIAL MARGIN "
                    "[559501.74 USD]")
            gw = self._gateway(
                tmp, lambda w: StubWhatifClient(w, late=late))
            reply = gw.whatif_cc(self._spec(), grace=2.0)
            self.assertTrue(reply["ok"])  # the reply itself landed
            self.assertEqual(reply["late_error_code"], 201)
            self.assertIn("YOUR ORDER IS NOT ACCEPTED",
                          reply["late_error"])
            self.assertIn("559501.74", reply["late_error"])

    def test_other_order_201_does_not_affect_verdict(self):
        with tempfile.TemporaryDirectory() as tmp:
            late = (0.2, "other", 201, "YOUR ORDER IS NOT ACCEPTED.")
            gw = self._gateway(
                tmp, lambda w: StubWhatifClient(w, late=late))
            reply = gw.whatif_cc(self._spec(), grace=0.8)
            self.assertTrue(reply["ok"])
            self.assertEqual(reply["init_margin_change"], 150.25)
            self.assertIsNone(reply["late_error"])

    def test_blank_figures_come_back_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            blank = _whatif_state(init="", maint="", ewl="", comm="")
            gw = self._gateway(
                tmp, lambda w: StubWhatifClient(w, state=blank))
            reply = gw.whatif_cc(self._spec(), grace=0.2)
            self.assertTrue(reply["ok"])
            self.assertIsNone(reply["init_margin_change"])

    def test_whatif_cc_refuses_a_non_whatif_spec(self):
        with tempfile.TemporaryDirectory() as tmp:
            gw = self._gateway(tmp, lambda w: StubWhatifClient(w))
            spec = setu_exec.build_cc_order_spec(CC_ROW, 6.00,
                                                 what_if=False)
            with self.assertRaises(AssertionError):
                gw.whatif_cc(spec, grace=0.1)


if __name__ == "__main__":
    unittest.main()
