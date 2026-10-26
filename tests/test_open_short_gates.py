"""Tests for setu-exec v0.7 open_short rows — standalone
sell-to-open and the roll open-leg arm.

Synthetic data only, with fake Kubera/Gateway doubles in the same
style as tests/test_v060_gates.py. No real Gateway, no real orders.

Run: python3 -m pytest tests/test_open_short_gates.py -q
"""
import copy
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import setu_exec  # noqa: E402

PUT_CON_ID = 922423171   # IONQ 2026-11-20 30P (a real resolved id)
CALL_CON_ID = 932592066  # MRVL 2026-11-27 295C

SHORT_ROW = {
    "id": "short-row-1",
    "account_id": "U19377323",
    "con_id": PUT_CON_ID,
    "symbol": "IONQ",
    "local_symbol": "IONQ  261120P00030000",
    "expiry": "2026-11-20",
    "strike": 30.0,
    "option_type": "Put",
    "quantity": 2.0,
    "premium": None,
    "target_limit": 2.50,
    "action": "place",
    "kind": "open_short",
    "approval_mode": "manual",
    "source": "manual",
}

PARENT_ID = "close-row-9"

ROLL_ROW = {
    "id": "roll-open-1",
    "account_id": "U19377323",
    "con_id": 0,
    "symbol": "IONQ",
    "local_symbol": "",
    "expiry": "2026-11-20",
    "strike": 30.0,
    "option_type": "Put",
    "quantity": 5.0,
    "premium": None,
    "target_limit": 2.50,
    "action": "place",
    "kind": "open_short",
    "approval_mode": "manual",
    "source": "manual",
    "parent_row_id": PARENT_ID,
    "parent_state": "FILLED",
    "arm_min_net_credit": 0.50,
    "expires_at": "2999-01-01T00:00:00+00:00",
}

VERIFIED_PUT = {
    "con_id": PUT_CON_ID,
    "symbol": "IONQ",
    "sec_type": "OPT",
    "expiry": "20261120",
    "strike": 30.0,
    "right": "P",
    "multiplier": "100",
    "min_tick": 0.01,
}

VERIFIED_CALL = {
    "con_id": CALL_CON_ID,
    "symbol": "MRVL",
    "sec_type": "OPT",
    "expiry": "20261127",
    "strike": 295.0,
    "right": "C",
    "multiplier": "100",
    "min_tick": 0.01,
}

CONCLUSIVE_WHATIF = {
    "ok": True, "init_margin_change": -120.0,
    "maint_margin_change": -60.0, "equity_with_loan_change": 0.0,
    "commission": None, "late_error": None, "late_error_code": None}


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
        self.rows = rows if rows is not None else [dict(SHORT_ROW)]
        self.kill = kill
        self.events = []

    def get_control(self):
        return {"account_id": "U19377323", "kill_switch": self.kill}

    def poll_approved(self):
        return [setu_exec.normalise_row(dict(r)) for r in self.rows]

    def post_event(self, row_id, kind, detail):
        self.events.append((row_id, kind, detail))
        return {"status": "success"}


def working_order(con_id=PUT_CON_ID, action="SELL", status="Submitted",
                  order_ref="", limit_price=2.40):
    return {"con_id": con_id, "symbol": "IONQ", "expiry": "20261120",
            "strike": 30.0, "right": "P", "action": action,
            "total_quantity": 1.0, "limit_price": limit_price,
            "order_ref": order_ref, "status": status,
            "perm_id": 515151, "order_id": 7711,
            "filled": 0.0, "remaining": 1.0}


class FakeGatewayShort:
    """Serves every v0.7 path: positions/open orders, contract
    details by con_id and by spec, the quote snapshot, BOTH
    what-ifs (the standard one records its calls so the tests can
    pin that open_short never uses it) and transmit."""

    def __init__(self, positions="default", open_orders=None,
                 details="default", spec_details="default",
                 quote_reply="default", whatif_cc_reply=None,
                 transmit_result=None):
        self.positions_map = {} if positions == "default" else positions
        self.open_orders = open_orders if open_orders is not None else []
        self.details = ([dict(VERIFIED_PUT)] if details == "default"
                        else details)
        self.spec_details = ([dict(VERIFIED_PUT)]
                             if spec_details == "default" else spec_details)
        self.quote_reply = ({"bid": 4.20, "ask": 4.60, "last": None,
                             "error": None}
                            if quote_reply == "default" else quote_reply)
        self.whatif_cc_reply = whatif_cc_reply or dict(CONCLUSIVE_WHATIF)
        self.transmit_result = transmit_result or {
            "order_id": 6101, "perm_id": 999, "status": "PreSubmitted",
            "error": None, "avg_fill_price": None}
        self.last_status_by_perm = {}
        self.last_fill_by_perm = {}
        self.whatif_specs = []
        self.whatif_cc_specs = []
        self.transmit_specs = []
        self.details_calls = []
        self.spec_calls = []
        self.quote_calls = []

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

    def fetch_quote_snapshot(self, verified):
        self.quote_calls.append(int(verified["con_id"]))
        if self.quote_reply is None:
            return None
        return dict(self.quote_reply)

    def whatif(self, spec):
        self.whatif_specs.append(copy.deepcopy(spec))
        return {"ok": True, "init_margin_change": 5.0,
                "maint_margin_change": 2.0,
                "equity_with_loan_change": 0.0, "commission": None}

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
                              gateway or FakeGatewayShort())


def events_of(kubera, kind=None):
    return [(r, k, d) for (r, k, d) in kubera.events
            if kind is None or k == kind]


def seed_parent_fill(ex, avg=3.00, filled=5.0, total=5.0):
    ex.tracker.retain_fill(PARENT_ID, {
        "avg_fill_price": avg, "filled_qty": filled,
        "total_qty": total, "perm_id": 424242,
        "filled_at": datetime.now(setu_exec.LONDON).isoformat(
            timespec="seconds")})


# ---------------------------------------------------------------------------
# pure gates
# ---------------------------------------------------------------------------

class ShortContractGateTests(unittest.TestCase):
    def test_carried_con_id_verifies_put(self):
        ok, detail = setu_exec.gate_short_contract(
            dict(SHORT_ROW), [dict(VERIFIED_PUT)])
        self.assertTrue(ok)
        self.assertEqual(detail["verified"]["con_id"], PUT_CON_ID)

    def test_carried_con_id_verifies_call(self):
        row = dict(SHORT_ROW, con_id=CALL_CON_ID, symbol="MRVL",
                   expiry="2026-11-27", strike=295.0,
                   option_type="Call")
        ok, _ = setu_exec.gate_short_contract(row, [dict(VERIFIED_CALL)])
        self.assertTrue(ok)

    def test_read_failure_and_empty_refuse_not_found(self):
        for details in (None, []):
            ok, detail = setu_exec.gate_short_contract(
                dict(SHORT_ROW), details)
            self.assertFalse(ok)
            self.assertEqual(detail["gate"], "contract_not_found")

    def test_wrong_right_is_a_mismatch(self):
        ok, detail = setu_exec.gate_short_contract(
            dict(SHORT_ROW), [dict(VERIFIED_PUT, right="C")])
        self.assertFalse(ok)
        self.assertEqual(detail["gate"], "contract_mismatch")

    def test_sentinel_resolves_put_by_spec(self):
        row = dict(SHORT_ROW, con_id=0)
        ok, detail = setu_exec.resolve_short_contract(
            row, [dict(VERIFIED_PUT)])
        self.assertTrue(ok)
        self.assertEqual(detail["resolved_con_id"], PUT_CON_ID)

    def test_sentinel_dedupes_same_con_id(self):
        row = dict(SHORT_ROW, con_id=0)
        ok, _ = setu_exec.resolve_short_contract(
            row, [dict(VERIFIED_PUT), dict(VERIFIED_PUT)])
        self.assertTrue(ok)

    def test_sentinel_two_distinct_refuses_mismatch(self):
        row = dict(SHORT_ROW, con_id=0)
        ok, detail = setu_exec.resolve_short_contract(
            row, [dict(VERIFIED_PUT),
                  dict(VERIFIED_PUT, con_id=PUT_CON_ID + 1)])
        self.assertFalse(ok)
        self.assertEqual(detail["gate"], "contract_mismatch")

    def test_sentinel_zero_matches_refuses_not_found(self):
        row = dict(SHORT_ROW, con_id=0)
        for details in ([], None, [dict(VERIFIED_PUT, strike=31.0)]):
            ok, detail = setu_exec.resolve_short_contract(row, details)
            self.assertFalse(ok)
            self.assertEqual(detail["gate"], "contract_not_found")


class ShortDuplicatePriceSpecTests(unittest.TestCase):
    def test_working_sell_blocks(self):
        ok, detail = setu_exec.gate_short_duplicate(
            dict(SHORT_ROW), [working_order()], PUT_CON_ID)
        self.assertFalse(ok)
        self.assertEqual(detail["gate"], "duplicate_open_order")

    def test_working_buy_does_not_block(self):
        ok, _ = setu_exec.gate_short_duplicate(
            dict(SHORT_ROW), [working_order(action="BUY")], PUT_CON_ID)
        self.assertTrue(ok)

    def test_terminal_sell_does_not_block(self):
        ok, _ = setu_exec.gate_short_duplicate(
            dict(SHORT_ROW), [working_order(status="Filled")],
            PUT_CON_ID)
        self.assertTrue(ok)

    def test_sell_on_another_contract_does_not_block(self):
        ok, _ = setu_exec.gate_short_duplicate(
            dict(SHORT_ROW), [working_order(con_id=PUT_CON_ID + 7)],
            PUT_CON_ID)
        self.assertTrue(ok)

    def test_own_order_ref_blocks(self):
        ok, detail = setu_exec.gate_short_duplicate(
            dict(SHORT_ROW),
            [working_order(action="BUY", order_ref="short-row-1")],
            PUT_CON_ID)
        self.assertFalse(ok)
        self.assertEqual(detail["gate"], "duplicate_open_order")

    def test_price_must_be_positive(self):
        for bad in (0, -1.5, None):
            ok, detail = setu_exec.gate_short_price(
                dict(SHORT_ROW, target_limit=bad), 0.01)
            self.assertFalse(ok)
            self.assertEqual(detail["gate"], "price")

    def test_price_tick_rounds_on_verified_tick(self):
        ok, detail = setu_exec.gate_short_price(
            dict(SHORT_ROW, target_limit=1.03), 0.05)
        self.assertTrue(ok)
        self.assertAlmostEqual(detail["limit"], 1.05)
        self.assertTrue(detail["limit_rounded"])

    def test_spec_is_the_frozen_sell_shape(self):
        spec = setu_exec.build_short_order_spec(
            dict(SHORT_ROW), dict(VERIFIED_PUT), 2.50, what_if=False)
        c, o = spec["contract"], spec["order"]
        self.assertEqual(c["conId"], PUT_CON_ID)
        self.assertEqual(c["right"], "P")
        self.assertEqual(c["secType"], "OPT")
        self.assertEqual(o["action"], "SELL")
        self.assertEqual(o["orderType"], "LMT")
        self.assertEqual(o["tif"], "GTC")
        self.assertFalse(o["outsideRth"])
        self.assertEqual(o["orderRef"], "short-row-1")
        self.assertEqual(o["totalQuantity"], 2.0)
        self.assertEqual(o["lmtPrice"], 2.50)
        self.assertFalse(o["whatIf"])
        spec_if = setu_exec.build_short_order_spec(
            dict(SHORT_ROW), dict(VERIFIED_PUT), 2.50, what_if=True)
        self.assertTrue(spec_if["order"]["whatIf"])

    def test_line_format(self):
        line = setu_exec.short_line(dict(SHORT_ROW), 2.0, 2.50)
        self.assertEqual(
            line, "IONQ 2026-11-20 30P SELL TO OPEN 2 @ 2.50 GTC "
                  "(row short-row-1)")


class ArmGateTests(unittest.TestCase):
    def test_missing_fill_record_refuses_unknown(self):
        ok, detail = setu_exec.gate_arm_fill(None, PARENT_ID)
        self.assertFalse(ok)
        self.assertEqual(detail["gate"], "arm_fill_unknown")

    def test_record_without_avg_price_refuses_unknown(self):
        ok, detail = setu_exec.gate_arm_fill(
            {"avg_fill_price": None, "filled_qty": 5.0,
             "total_qty": 5.0}, PARENT_ID)
        self.assertFalse(ok)
        self.assertEqual(detail["gate"], "arm_fill_unknown")

    def test_partial_parent_refuses_with_numbers(self):
        ok, detail = setu_exec.gate_arm_fill(
            {"avg_fill_price": 3.00, "filled_qty": 3.0,
             "total_qty": 5.0}, PARENT_ID)
        self.assertFalse(ok)
        self.assertEqual(detail["gate"], "arm_parent_partial")
        self.assertEqual(detail["parent_filled_qty"], 3.0)
        self.assertEqual(detail["parent_total_qty"], 5.0)
        self.assertIn("3 of 5", detail["reason"])

    def test_full_parent_passes(self):
        ok, detail = setu_exec.gate_arm_fill(
            {"avg_fill_price": 3.00, "filled_qty": 5.0,
             "total_qty": 5.0}, PARENT_ID)
        self.assertTrue(ok)
        self.assertEqual(detail["avg_fill_price"], 3.00)

    def test_quote_variants(self):
        ok, detail = setu_exec.gate_short_quote(None)
        self.assertFalse(ok)
        self.assertEqual(detail["gate"], "arm_quote_unavailable")
        ok, detail = setu_exec.gate_short_quote(
            {"bid": None, "error": "IB error 354: not subscribed"})
        self.assertFalse(ok)
        self.assertEqual(detail["gate"], "arm_quote_unavailable")
        self.assertIn("354", detail["reason"])
        ok, detail = setu_exec.gate_short_quote({"bid": 0.0})
        self.assertFalse(ok)
        ok, detail = setu_exec.gate_short_quote({"bid": 4.20})
        self.assertTrue(ok)
        self.assertEqual(detail["bid"], 4.20)

    def test_credit_at_floor_arms(self):
        ok, detail = setu_exec.gate_arm_credit(3.50, 3.00, 0.50)
        self.assertTrue(ok)
        self.assertAlmostEqual(detail["net_credit"], 0.50)

    def test_credit_below_floor_refuses_with_figures(self):
        ok, detail = setu_exec.gate_arm_credit(3.20, 3.00, 0.50)
        self.assertFalse(ok)
        self.assertEqual(detail["gate"], "arm_credit_below_floor")
        self.assertAlmostEqual(detail["net_credit"], 0.20)
        self.assertEqual(detail["floor"], 0.50)
        self.assertEqual(detail["open_bid"], 3.20)
        self.assertEqual(detail["parent_avg_fill_price"], 3.00)

    def test_zero_floor_means_any_non_negative_credit(self):
        ok, _ = setu_exec.gate_arm_credit(3.00, 3.00, None)
        self.assertTrue(ok)
        ok, detail = setu_exec.gate_arm_credit(2.90, 3.00, 0.0)
        self.assertFalse(ok)
        self.assertEqual(detail["gate"], "arm_credit_below_floor")


class NormaliseRowV07Tests(unittest.TestCase):
    def test_roll_fields_carried(self):
        row = setu_exec.normalise_row(dict(ROLL_ROW))
        self.assertEqual(row["parent_row_id"], PARENT_ID)
        self.assertEqual(row["parent_state"], "FILLED")
        self.assertEqual(row["arm_min_net_credit"], 0.50)
        self.assertEqual(row["expires_at"], "2999-01-01T00:00:00+00:00")

    def test_standalone_defaults(self):
        row = setu_exec.normalise_row(dict(SHORT_ROW))
        self.assertEqual(row["parent_row_id"], "")
        self.assertEqual(row["parent_state"], "")
        self.assertIsNone(row["arm_min_net_credit"])
        self.assertEqual(row["expires_at"], "")


# ---------------------------------------------------------------------------
# executor: standalone open_short
# ---------------------------------------------------------------------------

class StandaloneExecutorTests(unittest.TestCase):
    def test_simulate_would_place_via_conclusive_whatif_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            kubera, gw = FakeKubera(), FakeGatewayShort()
            make_executor(tmp, kubera, gw).cycle()
            would = events_of(kubera, "would_place")
            self.assertEqual(len(would), 1)
            detail = would[0][2]
            self.assertEqual(detail["kind"], "open_short")
            self.assertNotIn("simulate_gate_failed", detail)
            self.assertEqual(detail["order"]["order"]["action"], "SELL")
            self.assertEqual(
                detail["order"]["contract"]["conId"], PUT_CON_ID)
            # PIN: the conclusive cc what-if ran; the standard
            # what-if was NEVER used for this kind.
            self.assertEqual(len(gw.whatif_cc_specs), 1)
            self.assertEqual(gw.whatif_specs, [])
            self.assertEqual(gw.transmit_specs, [])
            # A carried con_id never touches the by-spec resolver.
            self.assertEqual(gw.spec_calls, [])
            self.assertEqual(gw.details_calls, [PUT_CON_ID])

    def test_sentinel_resolves_and_reports_con_id(self):
        with tempfile.TemporaryDirectory() as tmp:
            row = dict(SHORT_ROW, con_id=0)
            kubera, gw = FakeKubera(rows=[row]), FakeGatewayShort()
            make_executor(tmp, kubera, gw).cycle()
            self.assertEqual(gw.spec_calls,
                             [("IONQ", "2026-11-20", 30.0, "P")])
            would = events_of(kubera, "would_place")
            self.assertEqual(len(would), 1)
            self.assertEqual(would[0][2]["resolved_con_id"], PUT_CON_ID)

    def test_sentinel_zero_matches_is_contract_not_found(self):
        with tempfile.TemporaryDirectory() as tmp:
            row = dict(SHORT_ROW, con_id=0)
            kubera = FakeKubera(rows=[row])
            gw = FakeGatewayShort(spec_details=[])
            make_executor(tmp, kubera, gw).cycle()
            would = events_of(kubera, "would_place")
            self.assertEqual(len(would), 1)
            self.assertTrue(would[0][2]["simulate_gate_failed"])
            self.assertEqual(would[0][2]["gate"], "contract_not_found")
            self.assertEqual(gw.whatif_cc_specs, [])

    def test_duplicate_sell_refuses(self):
        with tempfile.TemporaryDirectory() as tmp:
            kubera = FakeKubera()
            gw = FakeGatewayShort(open_orders=[working_order()])
            make_executor(tmp, kubera, gw).cycle()
            would = events_of(kubera, "would_place")
            self.assertEqual(len(would), 1)
            self.assertEqual(would[0][2]["gate"], "duplicate_open_order")
            self.assertEqual(gw.whatif_cc_specs, [])
            self.assertEqual(gw.transmit_specs, [])

    def test_null_figures_whatif_is_inconclusive(self):
        with tempfile.TemporaryDirectory() as tmp:
            kubera = FakeKubera()
            gw = FakeGatewayShort(whatif_cc_reply={
                "ok": True, "init_margin_change": None,
                "maint_margin_change": None,
                "equity_with_loan_change": None, "commission": None,
                "late_error": None, "late_error_code": None})
            make_executor(tmp, kubera, gw).cycle()
            would = events_of(kubera, "would_place")
            self.assertEqual(len(would), 1)
            self.assertTrue(would[0][2]["simulate_gate_failed"])
            self.assertEqual(would[0][2]["gate"], "whatif_inconclusive")
            self.assertEqual(gw.whatif_specs, [])

    def test_margin_whatif_refuses(self):
        with tempfile.TemporaryDirectory() as tmp:
            kubera = FakeKubera()
            gw = FakeGatewayShort(whatif_cc_reply={
                **CONCLUSIVE_WHATIF, "init_margin_change": 500.0})
            make_executor(tmp, kubera, gw).cycle()
            would = events_of(kubera, "would_place")
            self.assertEqual(would[0][2]["gate"], "margin_impact")

    def test_bypass_row_refuses_before_any_gateway_work(self):
        with tempfile.TemporaryDirectory() as tmp:
            row = dict(SHORT_ROW, approval_mode="bypass")
            kubera, gw = FakeKubera(rows=[row]), FakeGatewayShort()
            make_executor(tmp, kubera, gw).cycle()
            would = events_of(kubera, "would_place")
            self.assertEqual(len(would), 1)
            self.assertEqual(would[0][2]["gate"], "bypass_not_supported")
            self.assertEqual(gw.details_calls, [])
            self.assertEqual(gw.spec_calls, [])
            self.assertEqual(gw.whatif_cc_specs, [])

    def test_incomplete_reads_refuse(self):
        with tempfile.TemporaryDirectory() as tmp:
            kubera = FakeKubera()
            gw = FakeGatewayShort(positions=None)
            make_executor(tmp, kubera, gw).cycle()
            # cycle() itself aborts on incomplete reads before
            # any row is processed: no events at all.
            self.assertEqual(kubera.events, [])

    def test_existing_short_in_same_contract_does_not_block(self):
        # No position gate: an existing short is an ADD.
        with tempfile.TemporaryDirectory() as tmp:
            positions = {PUT_CON_ID: {
                "con_id": PUT_CON_ID, "symbol": "IONQ",
                "sec_type": "OPT", "right": "P", "quantity": -3.0,
                "avg_cost": 2.80}}
            kubera = FakeKubera()
            gw = FakeGatewayShort(positions=positions)
            make_executor(tmp, kubera, gw).cycle()
            would = events_of(kubera, "would_place")
            self.assertEqual(len(would), 1)
            self.assertNotIn("simulate_gate_failed", would[0][2])

    def test_live_transmits_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            kubera, gw = FakeKubera(), FakeGatewayShort()
            ex = make_executor(tmp, kubera, gw, mode="live",
                               live_file=True)
            ex.cycle()
            self.assertEqual(len(gw.transmit_specs), 1)
            self.assertEqual(gw.transmit_specs[0]["order"]["action"],
                             "SELL")
            placed = events_of(kubera, "placed")
            self.assertEqual(len(placed), 1)
            self.assertEqual(placed[0][2]["kind"], "open_short")
            self.assertIsNotNone(ex.tracker.get("short-row-1"))
            ex.cycle()
            self.assertEqual(len(gw.transmit_specs), 1)


# ---------------------------------------------------------------------------
# executor: the roll arm
# ---------------------------------------------------------------------------

class RollArmExecutorTests(unittest.TestCase):
    def _run(self, tmp, row=None, gw=None, seed=True, mode="simulate",
             live_file=False):
        kubera = FakeKubera(rows=[row or dict(ROLL_ROW)])
        gw = gw or FakeGatewayShort()
        ex = make_executor(tmp, kubera, gw, mode=mode,
                           live_file=live_file)
        if seed:
            seed_parent_fill(ex)
        ex.cycle()
        return ex, kubera, gw

    def test_parent_not_filled_is_total_silence(self):
        with tempfile.TemporaryDirectory() as tmp:
            row = dict(ROLL_ROW, parent_state="STAGED")
            ex, kubera, gw = self._run(tmp, row=row)
            self.assertEqual(kubera.events, [])
            self.assertEqual(gw.details_calls, [])
            self.assertEqual(gw.spec_calls, [])
            self.assertEqual(gw.quote_calls, [])
            self.assertEqual(gw.whatif_cc_specs, [])
            self.assertEqual(gw.transmit_specs, [])

    def test_cutoff_passed_skips_silently(self):
        with tempfile.TemporaryDirectory() as tmp:
            row = dict(ROLL_ROW, expires_at="2000-01-01T00:00:00+00:00")
            ex, kubera, gw = self._run(tmp, row=row)
            self.assertEqual(kubera.events, [])
            self.assertEqual(gw.quote_calls, [])

    def test_arm_happy_path_would_place_with_evidence(self):
        with tempfile.TemporaryDirectory() as tmp:
            ex, kubera, gw = self._run(tmp)
            would = events_of(kubera, "would_place")
            self.assertEqual(len(would), 1)
            detail = would[0][2]
            self.assertNotIn("simulate_gate_failed", detail)
            self.assertEqual(detail["kind"], "open_short")
            self.assertEqual(detail["parent_row_id"], PARENT_ID)
            self.assertEqual(detail["parent_avg_fill_price"], 3.00)
            self.assertEqual(detail["parent_filled_qty"], 5.0)
            self.assertEqual(detail["open_bid"], 4.20)
            self.assertAlmostEqual(detail["net_credit"], 1.20)
            self.assertEqual(detail["arm_min_net_credit"], 0.50)
            self.assertEqual(detail["resolved_con_id"], PUT_CON_ID)
            self.assertEqual(gw.quote_calls, [PUT_CON_ID])
            # Simulate consumes nothing: the fill record stays.
            self.assertIsNotNone(ex.tracker.get_fill(PARENT_ID))
            self.assertEqual(gw.transmit_specs, [])

    def test_below_floor_refuses_with_figures(self):
        with tempfile.TemporaryDirectory() as tmp:
            gw = FakeGatewayShort(quote_reply={
                "bid": 3.20, "ask": 3.60, "last": None, "error": None})
            ex, kubera, gw = self._run(tmp, gw=gw)
            would = events_of(kubera, "would_place")
            self.assertEqual(len(would), 1)
            detail = would[0][2]
            self.assertTrue(detail["simulate_gate_failed"])
            self.assertEqual(detail["gate"], "arm_credit_below_floor")
            self.assertIn("0.20", detail["reason"])
            self.assertIn("0.50", detail["reason"])
            self.assertEqual(gw.whatif_cc_specs, [])
            self.assertEqual(gw.transmit_specs, [])

    def test_missing_fill_record_refuses(self):
        with tempfile.TemporaryDirectory() as tmp:
            ex, kubera, gw = self._run(tmp, seed=False)
            would = events_of(kubera, "would_place")
            self.assertEqual(len(would), 1)
            self.assertEqual(would[0][2]["gate"], "arm_fill_unknown")
            self.assertEqual(gw.spec_calls, [])

    def test_partial_parent_refuses(self):
        with tempfile.TemporaryDirectory() as tmp:
            kubera = FakeKubera(rows=[dict(ROLL_ROW)])
            gw = FakeGatewayShort()
            ex = make_executor(tmp, kubera, gw)
            seed_parent_fill(ex, filled=3.0, total=5.0)
            ex.cycle()
            would = events_of(kubera, "would_place")
            self.assertEqual(len(would), 1)
            self.assertEqual(would[0][2]["gate"], "arm_parent_partial")
            self.assertEqual(gw.quote_calls, [])

    def test_quote_unavailable_refuses(self):
        with tempfile.TemporaryDirectory() as tmp:
            gw = FakeGatewayShort(quote_reply={
                "bid": None, "ask": None, "last": None,
                "error": "IB error 354: requested market data is "
                         "not subscribed"})
            ex, kubera, gw = self._run(tmp, gw=gw)
            would = events_of(kubera, "would_place")
            self.assertEqual(len(would), 1)
            self.assertEqual(would[0][2]["gate"],
                             "arm_quote_unavailable")
            self.assertEqual(gw.whatif_cc_specs, [])

    def test_live_arm_transmits_and_consumes_the_fill(self):
        with tempfile.TemporaryDirectory() as tmp:
            ex, kubera, gw = self._run(tmp, mode="live", live_file=True)
            self.assertEqual(len(gw.transmit_specs), 1)
            placed = events_of(kubera, "placed")
            self.assertEqual(len(placed), 1)
            self.assertAlmostEqual(placed[0][2]["net_credit"], 1.20)
            self.assertEqual(placed[0][2]["parent_row_id"], PARENT_ID)
            # The dependency resolved: the retained fill is gone.
            self.assertIsNone(ex.tracker.get_fill(PARENT_ID))
            ex.cycle()
            self.assertEqual(len(gw.transmit_specs), 1)

    def test_live_below_floor_is_terminal_and_consumes(self):
        with tempfile.TemporaryDirectory() as tmp:
            gw = FakeGatewayShort(quote_reply={
                "bid": 3.20, "ask": 3.60, "last": None, "error": None})
            ex, kubera, gw = self._run(tmp, gw=gw, mode="live",
                                       live_file=True)
            failed = events_of(kubera, "gate_failed")
            self.assertEqual(len(failed), 1)
            self.assertEqual(failed[0][2]["gate"],
                             "arm_credit_below_floor")
            self.assertAlmostEqual(failed[0][2]["net_credit"], 0.20)
            self.assertEqual(gw.transmit_specs, [])
            self.assertIsNone(ex.tracker.get_fill(PARENT_ID))


# ---------------------------------------------------------------------------
# retained fills: tracker + reconcile
# ---------------------------------------------------------------------------

class RetainedFillTests(unittest.TestCase):
    def test_retain_get_drop_and_persistence(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = make_cfg(tmp)
            tracker = setu_exec.PlacedTracker(cfg)
            tracker.retain_fill("row-a", {
                "avg_fill_price": 1.25, "filled_qty": 2.0,
                "total_qty": 2.0, "perm_id": 777,
                "filled_at": datetime.now(setu_exec.LONDON).isoformat(
                    timespec="seconds")})
            fresh = setu_exec.PlacedTracker(cfg)
            got = fresh.get_fill("row-a")
            self.assertIsNotNone(got)
            self.assertEqual(got["avg_fill_price"], 1.25)
            fresh.drop_fill("row-a")
            self.assertIsNone(setu_exec.PlacedTracker(cfg)
                              .get_fill("row-a"))

    def test_stale_fill_is_pruned_on_load(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = make_cfg(tmp)
            tracker = setu_exec.PlacedTracker(cfg)
            old = (datetime.now(setu_exec.LONDON)
                   - timedelta(days=setu_exec.FILL_RETENTION_DAYS + 5))
            tracker.retain_fill("row-old", {
                "avg_fill_price": 1.0, "filled_qty": 1.0,
                "total_qty": 1.0, "perm_id": 1,
                "filled_at": old.isoformat(timespec="seconds")})
            self.assertIsNone(setu_exec.PlacedTracker(cfg)
                              .get_fill("row-old"))

    def _tracked_close(self, ex, row_id="close-row-9", perm=777):
        ex.tracker.add(row_id, {
            "order_ref": row_id, "account": "U19377323",
            "kind": "close_now", "confirmed": True,
            "broker_order_id": 55, "perm_id": perm, "last_status": "",
            "placed_at": "2026-10-26T10:00:00+01:00",
            "order": {"contract": {"conId": PUT_CON_ID},
                      "order": {"totalQuantity": 5.0}},
            "filled_qty": 0.0, "total_qty": None,
            "last_avg_fill_price": None})

    def test_filled_transition_retains_the_fill(self):
        with tempfile.TemporaryDirectory() as tmp:
            kubera, gw = FakeKubera(rows=[]), FakeGatewayShort()
            ex = make_executor(tmp, kubera, gw)
            self._tracked_close(ex)
            gw.last_status_by_perm = {777: "Filled"}
            gw.last_fill_by_perm = {777: {"filled": 5.0,
                                          "remaining": 0.0,
                                          "avg_fill_price": 3.00}}
            ex.reconcile([])
            self.assertIsNone(ex.tracker.get(PARENT_ID))
            fill = ex.tracker.get_fill(PARENT_ID)
            self.assertIsNotNone(fill)
            self.assertEqual(fill["avg_fill_price"], 3.00)
            self.assertEqual(fill["filled_qty"], 5.0)
            self.assertEqual(fill["total_qty"], 5.0)
            # The existing filled event is unchanged in shape.
            filled = events_of(kubera, "filled")
            self.assertEqual(len(filled), 1)
            self.assertNotIn("kind", filled[0][2])

    def test_live_sighting_accumulates_then_fill_retains(self):
        with tempfile.TemporaryDirectory() as tmp:
            kubera, gw = FakeKubera(rows=[]), FakeGatewayShort()
            ex = make_executor(tmp, kubera, gw)
            self._tracked_close(ex)
            live = working_order(action="BUY", order_ref=PARENT_ID)
            live["perm_id"] = 777
            live["filled"] = 2.0
            live["remaining"] = 3.0
            gw.last_fill_by_perm = {777: {"filled": 2.0,
                                          "remaining": 3.0,
                                          "avg_fill_price": 2.90}}
            ex.reconcile([live])
            rec = ex.tracker.get(PARENT_ID)
            self.assertIsNotNone(rec)
            self.assertEqual(rec["filled_qty"], 2.0)
            self.assertEqual(rec["last_avg_fill_price"], 2.90)
            self.assertEqual(kubera.events, [])  # accumulation is quiet
            gw.last_status_by_perm = {777: "Filled"}
            gw.last_fill_by_perm = {777: {"filled": 5.0,
                                          "remaining": 0.0,
                                          "avg_fill_price": 3.00}}
            ex.reconcile([])
            fill = ex.tracker.get_fill(PARENT_ID)
            self.assertEqual(fill["avg_fill_price"], 3.00)
            self.assertEqual(fill["filled_qty"], 5.0)


# ---------------------------------------------------------------------------
# gateway: the quote snapshot against the real wrapper
# ---------------------------------------------------------------------------

class StubMktClient:
    """EClient double answering reqMktData through the real
    wrapper: a subscription complaint followed by DELAYED ticks,
    silence, or an error-only snapshot."""

    def __init__(self, wrapper, mode="delayed"):
        self.wrapper = wrapper
        self.mode = mode
        self.calls = []
        self.cancelled = []

    def reqMktData(self, req_id, contract, generic_ticks, snapshot,
                   regulatory, options):
        self.calls.append((req_id, contract.conId, snapshot))
        if self.mode == "silent":
            return
        if self.mode == "erroronly":
            self.wrapper.error(
                req_id, 354, "Requested market data is not subscribed",
                "")
            self.wrapper.tickSnapshotEnd(req_id)
            return
        self.wrapper.error(
            req_id, 10091,
            "Requested market data requires additional subscription", "")
        self.wrapper.tickPrice(req_id, 66, 4.20, None)   # delayed bid
        self.wrapper.tickPrice(req_id, 67, 4.60, None)   # delayed ask
        self.wrapper.tickPrice(req_id, 68, 4.40, None)   # delayed last
        self.wrapper.tickSnapshotEnd(req_id)

    def cancelMktData(self, req_id):
        self.cancelled.append(req_id)


class GatewayQuoteSnapshotTests(unittest.TestCase):
    def _gateway(self, tmp, mode="delayed"):
        gw = setu_exec.ExecGateway(make_cfg(tmp))
        stub = StubMktClient(gw.wrapper, mode=mode)
        gw.client = stub
        return gw, stub

    def test_delayed_bid_captured_despite_subscription_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            gw, stub = self._gateway(tmp)
            quote = gw.fetch_quote_snapshot(dict(VERIFIED_PUT),
                                            timeout=2.0)
            self.assertIsNotNone(quote)
            self.assertEqual(quote["bid"], 4.20)
            self.assertEqual(quote["ask"], 4.60)
            self.assertEqual(quote["last"], 4.40)
            self.assertIn("10091", quote["error"])
            # The snapshot was a snapshot, and it was cancelled.
            self.assertTrue(stub.calls[0][2])
            self.assertEqual(len(stub.cancelled), 1)
            # A quote error never poisons the positions/orders reads.
            self.assertIsNone(gw.read_error)

    def test_error_only_snapshot_has_no_bid(self):
        with tempfile.TemporaryDirectory() as tmp:
            gw, _ = self._gateway(tmp, mode="erroronly")
            quote = gw.fetch_quote_snapshot(dict(VERIFIED_PUT),
                                            timeout=2.0)
            self.assertIsNotNone(quote)
            self.assertIsNone(quote["bid"])
            self.assertIn("354", quote["error"])
            self.assertIsNone(gw.read_error)

    def test_silent_snapshot_times_out_with_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            gw, _ = self._gateway(tmp, mode="silent")
            quote = gw.fetch_quote_snapshot(dict(VERIFIED_PUT),
                                            timeout=0.2)
            self.assertIsNotNone(quote)
            self.assertIsNone(quote["bid"])
            self.assertIsNone(quote["error"])


if __name__ == "__main__":
    unittest.main()
