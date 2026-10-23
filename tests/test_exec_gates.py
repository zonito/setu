"""Tests for setu-exec's gates, modes and reconciliation.

Synthetic data only, plus one integration test against a local mock
Kubera (http.server) with a fake Gateway. No real Gateway, no real
orders — the live-placement path is exercised only against fakes.

Run: python3 -m unittest discover tests
"""
import copy
import json
import os
import re
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import setu_exec  # noqa: E402

ROW = {
    "id": "row-123",
    "account_id": "U19377323",
    "con_id": 843250654,
    "symbol": "ASTS",
    "local_symbol": "ASTS  261120P00050000",
    "expiry": "2026-11-20",
    "strike": 50.0,
    "option_type": "Put",
    "quantity": 3.0,
    "premium": 6.28,
    "target_limit": 2.51,  # tick_round(0.40 * 6.28)
}


def short_position(qty=-5.0, avg_cost=6.28, con_id=843250654):
    return {"con_id": con_id, "symbol": "ASTS", "quantity": qty,
            "avg_cost": avg_cost}


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
        # Model a correctly secured machine (the live hygiene check
        # refuses group/other-writable switch files; this sandbox's
        # umask would otherwise create the fixture at 0o660).
        os.chmod(live, 0o600)
    return cfg


def arm(cfg: dict) -> None:
    setu_exec.arm_path(cfg).write_text(setu_exec.london_today() + "\n",
                                       encoding="utf-8")


class FakeKubera:
    def __init__(self, rows=None, kill=False):
        self.rows = rows if rows is not None else [dict(ROW)]
        self.kill = kill
        self.events = []

    def get_control(self):
        return {"account_id": "U19377323", "kill_switch": self.kill}

    def poll_approved(self):
        return [dict(r) for r in self.rows]

    def post_event(self, row_id, kind, detail):
        self.events.append((row_id, kind, detail))
        return {"status": "success"}


class FakeGateway:
    def __init__(self, position="default", open_orders=None, whatif_reply=None,
                 transmit_result=None):
        if position == "default":
            position = short_position()
        self.position = position  # None means: no live positions at all
        self.open_orders = open_orders if open_orders is not None else []
        self.whatif_reply = whatif_reply or {
            "ok": True, "init_margin_change": -1500.0,
            "maint_margin_change": -900.0, "equity_with_loan_change": 0.0,
            "commission": None}
        self.transmit_result = transmit_result or {
            "order_id": 5001, "perm_id": 777, "status": "PreSubmitted",
            "error": None}
        self.last_status_by_perm = {}
        self.whatif_specs = []
        self.transmit_specs = []
        self.fetch_count = 0

    def fetch_positions(self):
        self.fetch_count += 1
        if self.position is None:
            return {}
        return {self.position["con_id"]: dict(self.position)}

    def fetch_open_orders(self):
        self.fetch_count += 1
        return [dict(o) for o in self.open_orders]

    def whatif(self, spec):
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
                              gateway or FakeGateway())


class GatePositionTests(unittest.TestCase):
    def test_missing_position_fails(self):
        ok, detail = setu_exec.gate_position(ROW, None)
        self.assertFalse(ok)
        self.assertEqual(detail["gate"], "position")

    def test_long_position_fails(self):
        ok, detail = setu_exec.gate_position(ROW, short_position(qty=4.0))
        self.assertFalse(ok)
        self.assertEqual(detail["gate"], "position")

    def test_reduced_to_live_short(self):
        ok, detail = setu_exec.gate_position(ROW, short_position(qty=-2.0))
        self.assertTrue(ok)
        self.assertEqual(detail["effective_qty"], 2.0)
        self.assertTrue(detail["reduced"])

    def test_full_size_not_reduced(self):
        ok, detail = setu_exec.gate_position(ROW, short_position(qty=-5.0))
        self.assertTrue(ok)
        self.assertEqual(detail["effective_qty"], 3.0)
        self.assertFalse(detail["reduced"])


class GateDuplicateTests(unittest.TestCase):
    def test_duplicate_by_order_ref(self):
        orders = [{"con_id": 1, "action": "SELL", "limit_price": 9.99,
                   "order_ref": "row-123", "status": "Submitted"}]
        ok, detail = setu_exec.gate_duplicate(ROW, orders, 2.51)
        self.assertFalse(ok)
        self.assertEqual(detail["gate"], "duplicate")

    def test_duplicate_by_contract_and_price(self):
        orders = [{"con_id": 843250654, "action": "BUY", "limit_price": 2.51,
                   "order_ref": "something-else", "status": "Submitted"}]
        ok, detail = setu_exec.gate_duplicate(ROW, orders, 2.51)
        self.assertFalse(ok)
        self.assertEqual(detail["gate"], "duplicate")

    def test_buy_at_any_price_on_same_contract_is_duplicate(self):
        # The 2026-10-23 live-data lesson: a hand-placed portal GTC at
        # a non-40% price must still block a staged placement.
        orders = [{"con_id": 843250654, "action": "BUY", "limit_price": 1.50,
                   "order_ref": "", "status": "Submitted"}]
        ok, detail = setu_exec.gate_duplicate(ROW, orders, 2.51)
        self.assertFalse(ok)
        self.assertEqual(detail["gate"], "duplicate")

    def test_other_side_or_contract_passes(self):
        orders = [
            {"con_id": 843250654, "action": "SELL", "limit_price": 2.51,
             "order_ref": "", "status": "Submitted"},
            {"con_id": 999, "action": "BUY", "limit_price": 2.51,
             "order_ref": "", "status": "Submitted"},
        ]
        ok, _ = setu_exec.gate_duplicate(ROW, orders, 2.51)
        self.assertTrue(ok)


class GatePriceTests(unittest.TestCase):
    def test_target_mismatch_fails(self):
        row = dict(ROW, target_limit=2.60)
        ok, detail = setu_exec.gate_price(row, short_position())
        self.assertFalse(ok)
        self.assertEqual(detail["gate"], "price")

    def test_premium_avg_cost_drift_fails(self):
        ok, detail = setu_exec.gate_price(ROW, short_position(avg_cost=8.50))
        self.assertFalse(ok)
        self.assertEqual(detail["gate"], "price")
        self.assertIn("drift", detail["reason"])

    def test_small_drift_and_missing_avg_cost_pass(self):
        ok, detail = setu_exec.gate_price(ROW, short_position(avg_cost=6.60))
        self.assertTrue(ok)
        self.assertEqual(detail["limit"], 2.51)
        ok, _ = setu_exec.gate_price(ROW, short_position(avg_cost=None))
        self.assertTrue(ok)
        ok, _ = setu_exec.gate_price(ROW, None)
        self.assertTrue(ok, "price gate alone does not need the position")

    def test_happy_path_all_gates(self):
        ok, detail = setu_exec.evaluate_gates(ROW, short_position(), [])
        self.assertTrue(ok)
        self.assertEqual(detail["effective_qty"], 3.0)
        self.assertEqual(detail["limit"], 2.51)
        self.assertFalse(detail["reduced"])


class GateWhatIfTests(unittest.TestCase):
    def test_margin_increase_beyond_tolerance_fails(self):
        ok, detail = setu_exec.gate_whatif(
            {"ok": True, "init_margin_change": 75.0})
        self.assertFalse(ok)
        self.assertEqual(detail["gate"], "whatif")

    def test_margin_release_and_error(self):
        ok, _ = setu_exec.gate_whatif({"ok": True, "init_margin_change": -1500.0})
        self.assertTrue(ok)
        ok, _ = setu_exec.gate_whatif({"ok": True, "init_margin_change": None})
        self.assertTrue(ok)
        ok, detail = setu_exec.gate_whatif({"ok": False, "error": "IB error 321"})
        self.assertFalse(ok)
        self.assertEqual(detail["gate"], "whatif")


class OrderSpecTests(unittest.TestCase):
    def test_frozen_shape(self):
        spec = setu_exec.build_order_spec(ROW, 3.0, 2.51, what_if=True)
        c, o = spec["contract"], spec["order"]
        self.assertEqual(c["conId"], 843250654)
        self.assertEqual(c["secType"], "OPT")
        self.assertEqual(c["exchange"], "SMART")
        self.assertEqual(c["currency"], "USD")
        self.assertEqual(c["lastTradeDateOrContractMonth"], "20261120")
        self.assertEqual(c["strike"], 50.0)
        self.assertEqual(c["right"], "P")
        self.assertEqual(c["multiplier"], "100")
        self.assertEqual(o["action"], "BUY")
        self.assertEqual(o["orderType"], "LMT")
        self.assertEqual(o["tif"], "GTC")
        self.assertFalse(o["outsideRth"])
        self.assertEqual(o["orderRef"], "row-123")
        self.assertEqual(o["lmtPrice"], 2.51)
        self.assertEqual(o["totalQuantity"], 3.0)

    def test_ib_objects_disable_etradeonly_and_firmquoteonly(self):
        # Regression (live, 2026-10-23): ibapi defaults both to True and
        # current IBKR servers reject eTradeOnly with error 10268.
        gw = setu_exec.ExecGateway.__new__(setu_exec.ExecGateway)
        gw.account = "U19377323"
        spec = setu_exec.build_order_spec(ROW, 3.0, 2.51, what_if=True)
        _contract, order = gw._ib_objects(spec)
        self.assertFalse(order.eTradeOnly)
        self.assertFalse(order.firmQuoteOnly)

    def test_live_spec_equals_simulate_except_whatif(self):
        sim = setu_exec.build_order_spec(ROW, 2.0, 2.51, what_if=True)
        live = setu_exec.build_order_spec(ROW, 2.0, 2.51, what_if=False)
        self.assertTrue(sim["order"].pop("whatIf"))
        self.assertFalse(live["order"].pop("whatIf"))
        self.assertEqual(sim, live)

    def test_only_two_place_order_calls_both_guarded(self):
        src = Path(setu_exec.__file__).read_text(encoding="utf-8")
        self.assertEqual(len(re.findall(r"\.placeOrder\s*\(", src)), 2,
                         "whatif + transmit are the only order calls")
        self.assertIn('if not order.whatIf:', src)
        self.assertIn('if order.whatIf:', src)


class ModeAndArmTests(unittest.TestCase):
    def test_live_refuses_without_enable_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = make_cfg(tmp, mode="live", live_file=False)
            ex = setu_exec.Executor(cfg, FakeKubera(), FakeGateway())
            self.assertIsNotNone(ex.mode_refusal)
            self.assertEqual(ex.run_once(), 2)
            kubera = FakeKubera()
            ex2 = setu_exec.Executor(cfg, kubera, FakeGateway())
            ex2.cycle()
            self.assertEqual(kubera.events, [], "a refused executor touches nothing")

    def test_live_allowed_with_enable_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = make_cfg(tmp, mode="live", live_file=True)
            mode, refusal = setu_exec.resolve_mode(cfg)
            self.assertEqual(mode, "live")
            self.assertIsNone(refusal)

    def test_simulate_is_default_and_needs_no_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = make_cfg(tmp, mode="simulate")
            mode, refusal = setu_exec.resolve_mode(cfg)
            self.assertEqual((mode, refusal), ("simulate", None))

    def test_arm_must_be_today(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = make_cfg(tmp)
            self.assertFalse(setu_exec.is_armed(cfg))
            setu_exec.arm_path(cfg).write_text("1999-01-01\n", encoding="utf-8")
            self.assertFalse(setu_exec.is_armed(cfg),
                             "yesterday's arm never works")
            arm(cfg)
            self.assertTrue(setu_exec.is_armed(cfg))

    def test_unarmed_cycle_does_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = make_cfg(tmp)  # no arm file
            kubera, gw = FakeKubera(), FakeGateway()
            setu_exec.Executor(cfg, kubera, gw).cycle()
            self.assertEqual(kubera.events, [])
            self.assertEqual(gw.fetch_count, 0)

    def test_kill_switch_idles(self):
        with tempfile.TemporaryDirectory() as tmp:
            kubera, gw = FakeKubera(kill=True), FakeGateway()
            ex = make_executor(tmp, kubera=kubera, gateway=gw)
            ex.cycle()
            self.assertEqual(kubera.events, [])
            self.assertEqual(gw.fetch_count, 0)


class SimulateFlowTests(unittest.TestCase):
    def test_simulate_posts_would_place_and_never_transmits(self):
        with tempfile.TemporaryDirectory() as tmp:
            kubera, gw = FakeKubera(), FakeGateway()
            ex = make_executor(tmp, kubera=kubera, gateway=gw)
            ex.cycle()
            kinds = [k for _id, k, _d in kubera.events]
            self.assertEqual(kinds, ["placing", "would_place"])
            self.assertEqual(gw.transmit_specs, [],
                             "simulate mode must never transmit")
            self.assertEqual(len(gw.whatif_specs), 1)
            self.assertTrue(gw.whatif_specs[0]["order"]["whatIf"])
            detail = kubera.events[1][2]
            self.assertEqual(detail["order"]["order"]["orderRef"], "row-123")
            self.assertEqual(detail["order"]["order"]["lmtPrice"], 2.51)
            self.assertEqual(detail["effective_qty"], 3.0)
            self.assertEqual(detail["whatif"]["init_margin_change"], -1500.0)

    def test_gate_failure_posts_gate_failed(self):
        with tempfile.TemporaryDirectory() as tmp:
            kubera = FakeKubera()
            gw = FakeGateway(position=None)  # no live position at all
            ex = make_executor(tmp, kubera=kubera, gateway=gw)
            ex.cycle()
            kinds = [k for _id, k, _d in kubera.events]
            self.assertEqual(kinds, ["placing", "gate_failed"])
            self.assertEqual(kubera.events[1][2]["gate"], "position")
            self.assertEqual(gw.whatif_specs, [])


class LiveFlowTests(unittest.TestCase):
    def test_live_places_and_tracks(self):
        with tempfile.TemporaryDirectory() as tmp:
            kubera, gw = FakeKubera(), FakeGateway()
            ex = make_executor(tmp, kubera=kubera, gateway=gw,
                               mode="live", live_file=True)
            ex.cycle()
            kinds = [k for _id, k, _d in kubera.events]
            self.assertEqual(kinds, ["placing", "placed"])
            self.assertEqual(len(gw.transmit_specs), 1)
            self.assertFalse(gw.transmit_specs[0]["order"]["whatIf"])
            placed = kubera.events[1][2]
            self.assertEqual(placed["broker_order_id"], 5001)
            self.assertEqual(placed["perm_id"], 777)
            rec = ex.tracker.get("row-123")
            self.assertIsNotNone(rec)
            self.assertTrue(rec["confirmed"])
            self.assertEqual(rec["perm_id"], 777)

    def test_reconcile_after_restart_by_order_ref(self):
        """Crash after transmit, before the placed event: the state
        file + a live open order carrying the row's orderRef must stop
        any second placement."""
        with tempfile.TemporaryDirectory() as tmp:
            cfg = make_cfg(tmp, mode="live", live_file=True)
            arm(cfg)
            tracker = setu_exec.PlacedTracker(cfg)
            tracker.add("row-123", {
                "order_ref": "row-123", "account": "U19377323",
                "confirmed": False, "broker_order_id": None,
                "perm_id": None, "last_status": "", "order": {}})
            # "Restart": fresh executor + tracker reloaded from disk.
            kubera = FakeKubera()  # row still APPROVED in Kubera
            gw = FakeGateway(open_orders=[{
                "con_id": 843250654, "action": "BUY", "limit_price": 2.51,
                "order_ref": "row-123", "status": "Submitted",
                "perm_id": 777}])
            ex = setu_exec.Executor(cfg, kubera, gw)
            ex.cycle()
            self.assertEqual(gw.transmit_specs, [],
                             "a working order with this orderRef must never be duplicated")
            self.assertEqual(kubera.events, [],
                             "no new claim while the placed order is tracked")
            rec = ex.tracker.get("row-123")
            self.assertIsNotNone(rec)
            self.assertEqual(rec["perm_id"], 777,
                             "reconciliation adopts the live perm id")

    def test_reconcile_reports_fill_and_drops(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = make_cfg(tmp)
            arm(cfg)
            tracker = setu_exec.PlacedTracker(cfg)
            tracker.add("row-123", {
                "order_ref": "row-123", "account": "U19377323",
                "confirmed": True, "broker_order_id": 5001,
                "perm_id": 777, "last_status": "Filled", "order": {}})
            kubera = FakeKubera(rows=[])
            gw = FakeGateway(open_orders=[])
            ex = setu_exec.Executor(cfg, kubera, gw)
            ex.cycle()
            kinds = [k for _id, k, _d in kubera.events]
            self.assertEqual(kinds, ["filled"])
            self.assertIsNone(ex.tracker.get("row-123"))


class MockKubera:
    """A real HTTP mock of the staged-orders API for the integration
    test: control, APPROVED poll, and an event sink."""

    def __init__(self):
        self.events = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _json(self, code, body):
                data = json.dumps(body).encode("utf-8")
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                if self.path.startswith("/api/v1/broker-staged-orders/control"):
                    self._json(200, {"account_id": "U19377323",
                                     "kill_switch": False})
                elif self.path.startswith("/api/v1/broker-staged-orders"):
                    self._json(200, {"status": "success", "orders": [ROW]})
                else:
                    self._json(404, {"detail": "not found"})

            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(length) or b"{}")
                outer.events.append((self.path, body,
                                     self.headers.get("X-Kubera-Ingest-Token")))
                self._json(200, {"status": "success"})

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever,
                                       daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.server.shutdown()
        self.server.server_close()


class IntegrationTests(unittest.TestCase):
    def test_poll_claim_would_place_over_http(self):
        with tempfile.TemporaryDirectory() as tmp, MockKubera() as mock:
            cfg = make_cfg(tmp)
            cfg["kubera_base"] = f"http://127.0.0.1:{mock.port}"
            arm(cfg)
            kubera = setu_exec.KuberaClient(cfg)
            gw = FakeGateway()
            setu_exec.Executor(cfg, kubera, gw).cycle()
            kinds = [body["kind"] for _p, body, _t in mock.events]
            self.assertEqual(kinds, ["placing", "would_place"])
            path, body, token = mock.events[1]
            self.assertTrue(path.endswith("/broker-staged-orders/row-123/event"))
            self.assertEqual(token, "test-token")
            self.assertTrue(body["detail"]["order"]["order"]["whatIf"])
            self.assertEqual(body["detail"]["effective_qty"], 3.0)


# ---------------------------------------------------------------------------
# v0.2.0 guardrail hardening tests
# ---------------------------------------------------------------------------

from datetime import datetime, timedelta  # noqa: E402
from types import SimpleNamespace  # noqa: E402
from unittest import mock  # noqa: E402

ROW2 = {
    "id": "row-456",
    "account_id": "U19377323",
    "con_id": 222,
    "symbol": "XYZ",
    "local_symbol": "XYZ   261120P00010000",
    "expiry": "2026-11-20",
    "strike": 10.0,
    "option_type": "Put",
    "quantity": 2.0,
    "premium": 5.0,
    "target_limit": 2.0,  # tick_round(0.40 * 5.0)
}


def short_position2(qty=-4.0, avg_cost=5.0):
    return {"con_id": 222, "symbol": "XYZ", "quantity": qty,
            "avg_cost": avg_cost}


class ControlStubKubera(FakeKubera):
    def __init__(self, control=None, exc=None, **kw):
        super().__init__(**kw)
        self._control = control
        self._exc = exc

    def get_control(self):
        if self._exc is not None:
            raise self._exc
        return self._control


class ControlFailClosedTests(unittest.TestCase):
    """Item 1: ANY control-read failure or malformed reply idles the
    cycle. Nothing is fetched, claimed, or placed."""

    def _run(self, kubera):
        with tempfile.TemporaryDirectory() as tmp:
            gw = FakeGateway()
            ex = make_executor(tmp, kubera=kubera, gateway=gw)
            ex.cycle()
            return kubera, gw

    def test_control_exception_idles(self):
        kubera, gw = self._run(ControlStubKubera(
            exc=setu_exec.KuberaError("boom")))
        self.assertEqual(kubera.events, [])
        self.assertEqual(gw.fetch_count, 0)

    def test_control_missing_kill_switch_key_idles(self):
        kubera, gw = self._run(ControlStubKubera(control={}))
        self.assertEqual(kubera.events, [])
        self.assertEqual(gw.fetch_count, 0)

    def test_control_non_bool_kill_switch_idles(self):
        kubera, gw = self._run(ControlStubKubera(control={"kill_switch": 1}))
        self.assertEqual(kubera.events, [])
        self.assertEqual(gw.fetch_count, 0)

    def test_control_non_dict_idles(self):
        kubera, gw = self._run(ControlStubKubera(control=["kill_switch"]))
        self.assertEqual(kubera.events, [])
        self.assertEqual(gw.fetch_count, 0)

    def test_control_false_proceeds(self):
        kubera, gw = self._run(ControlStubKubera(
            control={"account_id": "U19377323", "kill_switch": False}))
        kinds = [k for _id, k, _d in kubera.events]
        self.assertEqual(kinds, ["placing", "would_place"])


class PollStubKubera(FakeKubera):
    def poll_approved(self):
        raise setu_exec.KuberaError("poll boom")


class PollFailClosedTests(unittest.TestCase):
    """Item 1 (poll half): a failed poll means no work that cycle."""

    def test_failed_poll_places_nothing_in_live_mode(self):
        with tempfile.TemporaryDirectory() as tmp:
            kubera, gw = PollStubKubera(), FakeGateway()
            ex = make_executor(tmp, kubera=kubera, gateway=gw,
                               mode="live", live_file=True)
            ex.cycle()
            self.assertEqual(kubera.events, [])
            self.assertEqual(gw.whatif_specs, [])
            self.assertEqual(gw.transmit_specs, [])

    def test_malformed_poll_row_is_a_failed_poll(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = make_cfg(tmp)
            client = setu_exec.KuberaClient(cfg)
            client._request = lambda *a, **k: {"orders": [dict(ROW), "junk"]}
            with self.assertRaises(setu_exec.KuberaError):
                client.poll_approved()

    def test_poll_body_without_rows_is_empty_not_an_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = make_cfg(tmp)
            client = setu_exec.KuberaClient(cfg)
            client._request = lambda *a, **k: {"unexpected": True}
            self.assertEqual(client.poll_approved(), [])


class FakeIBClient:
    """Stands in for ibapi's EClient inside a real ExecGateway,
    driving the real wrapper callbacks — complete or truncated."""

    def __init__(self, gw, complete_positions=True, complete_orders=True,
                 error_on_positions=False):
        self.gw = gw
        self.complete_positions = complete_positions
        self.complete_orders = complete_orders
        self.error_on_positions = error_on_positions

    def reqPositions(self):
        contract = SimpleNamespace(conId=843250654, multiplier="100",
                                   secType="OPT", symbol="ASTS")
        self.gw.wrapper.position("U19377323", contract, -5.0, 628.0)
        if self.error_on_positions:
            self.gw.wrapper.error(-1, 1100, "Connectivity lost")
        if self.complete_positions:
            self.gw.wrapper.positionEnd()

    def cancelPositions(self):
        pass

    def reqAllOpenOrders(self):
        contract = SimpleNamespace(conId=999)
        order = SimpleNamespace(account="U19377323", permId=0,
                                lmtPrice=None, orderRef="", action="BUY")
        state = SimpleNamespace(status="Submitted")
        self.gw.wrapper.openOrder(7, contract, order, state)
        if self.complete_orders:
            self.gw.wrapper.openOrderEnd()


class ReadCompletionTests(unittest.TestCase):
    """Item 2: a Gateway read counts only when its completion
    callback fires (positionEnd / openOrderEnd). Truncated or
    erroring reads return None so the cycle aborts."""

    def _gateway(self, tmp, **kw):
        gw = setu_exec.ExecGateway(make_cfg(tmp))
        gw.client = FakeIBClient(gw, **kw)
        return gw

    def test_complete_reads_return_data(self):
        with tempfile.TemporaryDirectory() as tmp:
            gw = self._gateway(tmp)
            positions = gw.fetch_positions(timeout=2.0)
            self.assertIn(843250654, positions)
            self.assertEqual(positions[843250654]["avg_cost"], 6.28)
            orders = gw.fetch_open_orders(timeout=2.0)
            self.assertEqual(len(orders), 1)

    def test_truncated_positions_read_is_invalid(self):
        with tempfile.TemporaryDirectory() as tmp:
            gw = self._gateway(tmp, complete_positions=False)
            self.assertIsNone(gw.fetch_positions(timeout=0.6))

    def test_truncated_orders_read_is_invalid(self):
        with tempfile.TemporaryDirectory() as tmp:
            gw = self._gateway(tmp, complete_orders=False)
            self.assertIsNone(gw.fetch_open_orders(timeout=0.6))

    def test_error_mid_read_aborts_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            gw = self._gateway(tmp, complete_positions=False,
                               error_on_positions=True)
            self.assertIsNone(gw.fetch_positions(timeout=5.0))
            self.assertIsNotNone(gw.read_error)


class IncompleteGateway(FakeGateway):
    def fetch_positions(self):
        return None


class ReadsIncompleteRowTests(unittest.TestCase):
    """Item 2 (executor half): incomplete reads abort the cycle
    before any claim; a row handed partial reads is refused with
    the reads_incomplete gate, never evaluated."""

    def test_cycle_aborts_before_poll_on_incomplete_reads(self):
        with tempfile.TemporaryDirectory() as tmp:
            kubera, gw = FakeKubera(), IncompleteGateway()
            ex = make_executor(tmp, kubera=kubera, gateway=gw)
            ex.cycle()
            self.assertEqual(kubera.events, [])
            self.assertEqual(gw.whatif_specs, [])
            self.assertEqual(gw.transmit_specs, [])

    def test_process_row_refuses_partial_reads(self):
        with tempfile.TemporaryDirectory() as tmp:
            kubera, gw = FakeKubera(), FakeGateway()
            ex = make_executor(tmp, kubera=kubera, gateway=gw)
            ex.process_row(dict(ROW), None, [])
            kinds = [k for _id, k, _d in kubera.events]
            self.assertEqual(kinds, ["placing", "gate_failed"])
            self.assertEqual(kubera.events[1][2]["gate"], "reads_incomplete")
            self.assertEqual(gw.whatif_specs, [])


class LimitPremiumGateTests(unittest.TestCase):
    """Item 3: limit >= premium is corrupted input; refuse, naming
    both numbers."""

    def test_limit_below_premium_passes(self):
        ok, _ = setu_exec.gate_limit_below_premium(ROW, 2.51)
        self.assertTrue(ok)

    def test_limit_equal_premium_refuses(self):
        ok, detail = setu_exec.gate_limit_below_premium(ROW, 6.28)
        self.assertFalse(ok)
        self.assertEqual(detail["gate"], "limit_premium")
        self.assertIn("6.28", detail["reason"])

    def test_limit_above_premium_refuses(self):
        row = dict(ROW, premium=2.0)
        ok, detail = setu_exec.gate_limit_below_premium(row, 2.51)
        self.assertFalse(ok)
        self.assertIn("2.51", detail["reason"])
        self.assertIn("2.0", detail["reason"])

    def test_missing_numbers_refuse(self):
        ok, _ = setu_exec.gate_limit_below_premium(dict(ROW, premium=None), 2.51)
        self.assertFalse(ok)
        ok, _ = setu_exec.gate_limit_below_premium(ROW, None)
        self.assertFalse(ok)


class RunawayBrakeTests(unittest.TestCase):
    """Item 4: 60s spacing + 25/day from the state file. Accident
    insurance on live transmissions; simulate is untouched."""

    def test_spacing_blocks_second_live_placement(self):
        with tempfile.TemporaryDirectory() as tmp:
            kubera, gw = FakeKubera(rows=[]), FakeGateway()
            ex = make_executor(tmp, kubera=kubera, gateway=gw,
                               mode="live", live_file=True)
            ex.process_row(dict(ROW), {843250654: short_position()}, [])
            ex.process_row(dict(ROW2), {222: short_position2()}, [])
            self.assertEqual(len(gw.transmit_specs), 1)
            failures = [d for _id, k, d in kubera.events if k == "gate_failed"]
            self.assertEqual(len(failures), 1)
            self.assertEqual(failures[0]["gate"], "runaway_brake")
            self.assertIn("spacing", failures[0]["reason"])

    def test_daily_cap_blocks_live_placement(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = make_cfg(tmp, mode="live", live_file=True)
            arm(cfg)
            tracker = setu_exec.PlacedTracker(cfg)
            base = datetime.now(setu_exec.LONDON).replace(
                hour=0, minute=1, second=0, microsecond=0)
            tracker.transmissions = [
                (base + timedelta(seconds=61 * i)).isoformat(timespec="seconds")
                for i in range(setu_exec.RUNAWAY_DAILY_CAP)]
            tracker.save()
            kubera, gw = FakeKubera(rows=[]), FakeGateway()
            ex = setu_exec.Executor(cfg, kubera, gw)
            ex.process_row(dict(ROW), {843250654: short_position()}, [])
            self.assertEqual(gw.transmit_specs, [])
            failures = [d for _id, k, d in kubera.events if k == "gate_failed"]
            self.assertEqual(failures[0]["gate"], "runaway_brake")

    def test_brake_check_spacing_and_cap_reasons(self):
        now = datetime(2026, 10, 23, 12, 0, tzinfo=setu_exec.LONDON)
        with tempfile.TemporaryDirectory() as tmp:
            tracker = setu_exec.PlacedTracker(make_cfg(tmp))
            tracker.record_transmission(now - timedelta(seconds=30))
            ok, reason = tracker.brake_check(now)
            self.assertFalse(ok)
            self.assertIn("spacing", reason)
            ok, _ = tracker.brake_check(now + timedelta(seconds=31))
            self.assertTrue(ok)
            for i in range(setu_exec.RUNAWAY_DAILY_CAP):
                tracker.record_transmission(now - timedelta(seconds=3600 + i))
            ok, reason = tracker.brake_check(now)
            self.assertFalse(ok)
            self.assertIn("cap", reason)

    def test_placements_today_counts_only_today(self):
        now = datetime(2026, 10, 23, 12, 0, tzinfo=setu_exec.LONDON)
        with tempfile.TemporaryDirectory() as tmp:
            tracker = setu_exec.PlacedTracker(make_cfg(tmp))
            tracker.record_transmission(now - timedelta(days=1))
            tracker.record_transmission(now - timedelta(hours=2))
            self.assertEqual(tracker.placements_today(now), 1)

    def test_legacy_state_file_without_brake_section_loads(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = make_cfg(tmp)
            setu_exec.state_path(cfg).write_text(json.dumps(
                {"placed": {"row-9": {"order_ref": "row-9"}}}),
                encoding="utf-8")
            tracker = setu_exec.PlacedTracker(cfg)
            self.assertIsNotNone(tracker.get("row-9"))
            self.assertEqual(tracker.placements_today(), 0)
            ok, _ = tracker.brake_check()
            self.assertTrue(ok)

    def test_simulate_ignores_the_brake(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = make_cfg(tmp)
            arm(cfg)
            tracker = setu_exec.PlacedTracker(cfg)
            tracker.record_transmission()  # a transmission seconds ago
            kubera, gw = FakeKubera(), FakeGateway()
            ex = setu_exec.Executor(cfg, kubera, gw, tracker=tracker)
            ex.cycle()
            kinds = [k for _id, k, _d in kubera.events]
            self.assertEqual(kinds, ["placing", "would_place"])


class PushoverTests(unittest.TestCase):
    """Item 5: live mode notifies placements and gate refusals via
    Varuna's Pushover route; simulate never notifies; a failed
    notify changes nothing."""

    def test_request_shape_and_host_derivation(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = make_cfg(tmp)
            cfg["kubera_base"] = "http://100.125.239.56:8000"
            url, headers, payload = setu_exec.build_pushover_request(
                cfg, "T", "M")
            self.assertEqual(
                url, "http://100.125.239.56:8400/v1/vendors/pushover/send")
            self.assertEqual(headers["X-Varuna-Source"], "Molang")
            self.assertEqual(payload, {"title": "T", "message": "M",
                                       "priority": 1})

    def test_notify_base_override(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = make_cfg(tmp)
            cfg["notify_base"] = "http://notify.example:9999/"
            url, _h, _p = setu_exec.build_pushover_request(cfg, "T", "M")
            self.assertEqual(
                url, "http://notify.example:9999/v1/vendors/pushover/send")

    def test_failed_notify_is_swallowed(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = make_cfg(tmp)
            with mock.patch.object(setu_exec.urllib.request, "urlopen",
                                   side_effect=OSError("down")):
                self.assertFalse(setu_exec.send_pushover(cfg, "T", "M"))

    def test_successful_notify_posts_payload(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = make_cfg(tmp)
            captured = {}

            class Resp:
                def __enter__(self):
                    return self

                def __exit__(self, *a):
                    return False

                def read(self):
                    return b"{}"

            def fake_urlopen(req, timeout=None):
                captured["url"] = req.full_url
                captured["body"] = json.loads(req.data.decode())
                captured["source"] = req.get_header("X-varuna-source")
                return Resp()

            with mock.patch.object(setu_exec.urllib.request, "urlopen",
                                   fake_urlopen):
                self.assertTrue(setu_exec.send_pushover(cfg, "T", "M"))
            self.assertEqual(captured["body"]["priority"], 1)
            self.assertEqual(captured["source"], "Molang")

    def test_live_placement_notifies(self):
        with tempfile.TemporaryDirectory() as tmp:
            calls = []
            with mock.patch.object(
                    setu_exec, "send_pushover",
                    lambda cfg, t, m: calls.append((t, m)) or True):
                kubera, gw = FakeKubera(), FakeGateway()
                ex = make_executor(tmp, kubera=kubera, gateway=gw,
                                   mode="live", live_file=True)
                ex.cycle()
            self.assertEqual(len(calls), 1)
            self.assertIn("U19377323", calls[0][1])
            self.assertIn("ASTS", calls[0][1])
            self.assertIn("PLACED", calls[0][1])

    def test_live_gate_refusal_notifies(self):
        with tempfile.TemporaryDirectory() as tmp:
            calls = []
            with mock.patch.object(
                    setu_exec, "send_pushover",
                    lambda cfg, t, m: calls.append((t, m)) or True):
                kubera = FakeKubera()
                gw = FakeGateway(position=None)
                ex = make_executor(tmp, kubera=kubera, gateway=gw,
                                   mode="live", live_file=True)
                ex.cycle()
            self.assertEqual(len(calls), 1)
            self.assertIn("position", calls[0][1])

    def test_simulate_never_notifies(self):
        with tempfile.TemporaryDirectory() as tmp:
            calls = []
            with mock.patch.object(
                    setu_exec, "send_pushover",
                    lambda cfg, t, m: calls.append((t, m)) or True):
                kubera, gw = FakeKubera(), FakeGateway()
                ex = make_executor(tmp, kubera=kubera, gateway=gw)
                ex.cycle()
                # and a refusal in simulate stays silent too
                kubera2 = FakeKubera()
                ex2 = make_executor(tmp, kubera=kubera2,
                                    gateway=FakeGateway(position=None))
                ex2.cycle()
            self.assertEqual(calls, [])


class FileHygieneTests(unittest.TestCase):
    """Item 6: live mode refuses while executor.json or
    executor.live is group/other-writable."""

    def test_loose_executor_json_refuses_live(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = make_cfg(tmp, mode="live", live_file=True)
            loose = Path(tmp, "executor.json")
            loose.write_text('{"mode": "live"}', encoding="utf-8")
            os.chmod(loose, 0o666)
            _mode, refusal = setu_exec.resolve_mode(cfg)
            self.assertIsNotNone(refusal)
            self.assertIn("executor.json", refusal)

    def test_loose_live_file_refuses_live(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = make_cfg(tmp, mode="live", live_file=True)
            os.chmod(Path(tmp, "executor.live"), 0o777)
            _mode, refusal = setu_exec.resolve_mode(cfg)
            self.assertIsNotNone(refusal)
            self.assertIn("executor.live", refusal)

    def test_tight_files_pass(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = make_cfg(tmp, mode="live", live_file=True)
            tight = Path(tmp, "executor.json")
            tight.write_text('{"mode": "live"}', encoding="utf-8")
            os.chmod(tight, 0o600)
            _mode, refusal = setu_exec.resolve_mode(cfg)
            self.assertIsNone(refusal)

    def test_simulate_ignores_loose_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = make_cfg(tmp, mode="simulate")
            loose = Path(tmp, "executor.json")
            loose.write_text('{"mode": "simulate"}', encoding="utf-8")
            os.chmod(loose, 0o666)
            mode, refusal = setu_exec.resolve_mode(cfg)
            self.assertEqual((mode, refusal), ("simulate", None))

    def test_cycle_recheck_catches_mid_run_loosening(self):
        with tempfile.TemporaryDirectory() as tmp:
            kubera, gw = FakeKubera(), FakeGateway()
            ex = make_executor(tmp, kubera=kubera, gateway=gw,
                               mode="live", live_file=True)
            os.chmod(Path(tmp, "executor.live"), 0o777)  # loosened after start
            ex.cycle()
            self.assertEqual(kubera.events, [])
            self.assertEqual(gw.fetch_count, 0)


class _StopLoop(Exception):
    pass


class ConnectedGateway(FakeGateway):
    def is_connected(self):
        return True

    def connect(self):
        return True

    def disconnect(self):
        pass


class PostureLineTests(unittest.TestCase):
    """Item 7: one posture line per cycle in run mode carrying
    mode, armed state, kill state and placements today."""

    def test_posture_line_once_per_cycle(self):
        with tempfile.TemporaryDirectory() as tmp:
            lines = []
            kubera, gw = FakeKubera(), ConnectedGateway()
            ex = make_executor(tmp, kubera=kubera, gateway=gw)
            with mock.patch.object(setu_exec, "log", lines.append), \
                    mock.patch.object(setu_exec.time, "sleep",
                                      side_effect=_StopLoop):
                with self.assertRaises(_StopLoop):
                    ex.run_forever()
            posture = [ln for ln in lines if ln.startswith("posture:")]
            self.assertEqual(len(posture), 1)
            self.assertIn("mode=simulate", posture[0])
            self.assertIn("armed=True", posture[0])
            self.assertIn("kill_switch=False", posture[0])
            self.assertIn("placements_today=0", posture[0])


if __name__ == "__main__":
    unittest.main()
