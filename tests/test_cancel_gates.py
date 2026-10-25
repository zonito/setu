"""Tests for setu-exec v0.3 cancel gates and cancel flow.

Synthetic data only, with fake Kubera/Gateway doubles in the same
style as tests/test_exec_gates.py. No real Gateway, no real orders.

Run: python3 -m unittest discover tests
"""
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import setu_exec  # noqa: E402

PERM = 349464471

CANCEL_ROW = {
    "id": "cancel-row-1",
    "account_id": "U19377323",
    "con_id": 111222333,
    "symbol": "IREN",
    "local_symbol": "IREN  261030P00037000",
    "expiry": "2026-10-30",
    "strike": 37.0,
    "option_type": "Put",
    "quantity": 0.0,
    "premium": None,
    "target_limit": None,
    "action": "cancel",
    "target_order_id": str(PERM),
    "target_action": "Buy",
    "target_quantity": 10.0,
    "target_limit_price": 0.45,
    "reason": "orphan_order per coverage feed",
    "source": "orphan_scan",
    "approval_mode": "manual",
}


def live_order(**over):
    order = {
        "con_id": 111222333,
        "symbol": "IREN",
        "expiry": "20261030",
        "strike": 37.0,
        "right": "P",
        "action": "Buy",
        "limit_price": 0.45,
        "order_ref": "",
        "status": "PreSubmitted",
        "perm_id": PERM,
        "order_id": 8811,
        "filled": 0.0,
        "remaining": 10.0,
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
        Path(tmp, "executor.live").write_text("live\n", encoding="utf-8")
    return cfg


class FakeKubera:
    def __init__(self, rows):
        self.rows = rows
        self.events = []

    def get_control(self):
        return {"kill_switch": False}

    def poll_approved(self):
        return list(self.rows)

    def post_event(self, row_id, kind, detail):
        self.events.append((row_id, kind, detail))
        return {}

    def kinds(self):
        return [k for _, k, _ in self.events]


class FakeGateway:
    """fetch_open_orders reflects cancels: once cancel_order()
    succeeds, the target disappears from subsequent reads."""

    def __init__(self, positions=None, orders=None, cancel_result=None):
        self.positions = positions or {}
        self.orders = list(orders or [])
        self.cancel_result = cancel_result or {"ok": True, "status": "Cancelled"}
        self.cancel_calls = []

    def fetch_positions(self):
        return dict(self.positions)

    def fetch_open_orders(self):
        return list(self.orders)

    def cancel_order(self, order):
        self.cancel_calls.append(order)
        if self.cancel_result.get("ok"):
            self.orders = [o for o in self.orders
                           if o.get("perm_id") != order.get("perm_id")]
        return dict(self.cancel_result)


def make_executor(tmp, mode="simulate", kubera=None, gateway=None):
    cfg = make_cfg(tmp, mode=mode, live_file=(mode == "live"))
    kubera = kubera or FakeKubera([])
    gateway = gateway or FakeGateway()
    tracker = setu_exec.PlacedTracker(cfg)
    return setu_exec.Executor(cfg, kubera, gateway, tracker), kubera, gateway


class CancelGateTests(unittest.TestCase):
    def test_happy_path_ok(self):
        outcome, detail = setu_exec.evaluate_cancel_gates(
            CANCEL_ROW, {}, [live_order()], set())
        self.assertEqual(outcome, "ok")
        self.assertEqual(detail["order"]["perm_id"], PERM)

    def test_absent_target_is_already_gone_not_refusal(self):
        outcome, detail = setu_exec.evaluate_cancel_gates(
            CANCEL_ROW, {}, [], set())
        self.assertEqual(outcome, "already_gone")
        self.assertEqual(detail["gate"], "target")

    def test_missing_target_id_refuses(self):
        row = dict(CANCEL_ROW, target_order_id="")
        outcome, _ = setu_exec.evaluate_cancel_gates(row, {}, [live_order()], set())
        self.assertEqual(outcome, "refuse")

    def test_perm_fallback_to_session_order_id(self):
        order = live_order(perm_id=0)
        outcome, _ = setu_exec.evaluate_cancel_gates(
            dict(CANCEL_ROW, target_order_id="8811"), {}, [order], set())
        self.assertEqual(outcome, "ok")

    def test_identity_con_id_mismatch_refuses(self):
        outcome, detail = setu_exec.evaluate_cancel_gates(
            CANCEL_ROW, {}, [live_order(con_id=999)], set())
        self.assertEqual(outcome, "refuse")
        self.assertEqual(detail["gate"], "identity")

    def test_identity_action_mismatch_refuses(self):
        outcome, detail = setu_exec.evaluate_cancel_gates(
            CANCEL_ROW, {}, [live_order(action="Sell")], set())
        self.assertEqual(outcome, "refuse")
        self.assertEqual(detail["gate"], "identity")

    def test_partial_fill_refuses(self):
        outcome, detail = setu_exec.evaluate_cancel_gates(
            CANCEL_ROW, {}, [live_order(filled=3.0, remaining=7.0)], set())
        self.assertEqual(outcome, "refuse")
        self.assertEqual(detail["gate"], "unfilled")

    def test_unverifiable_fill_refuses(self):
        outcome, detail = setu_exec.evaluate_cancel_gates(
            CANCEL_ROW, {}, [live_order(filled=None)], set())
        self.assertEqual(outcome, "refuse")
        self.assertEqual(detail["gate"], "unfilled")

    def test_orphan_premise_position_appeared_refuses(self):
        positions = {111222333: {"con_id": 111222333, "quantity": -2.0,
                                 "avg_cost": 1.0}}
        outcome, detail = setu_exec.evaluate_cancel_gates(
            CANCEL_ROW, positions, [live_order()], set())
        self.assertEqual(outcome, "refuse")
        self.assertEqual(detail["gate"], "premise")

    def test_manual_row_skips_premise(self):
        row = dict(CANCEL_ROW, source="manual")
        positions = {111222333: {"con_id": 111222333, "quantity": -2.0,
                                 "avg_cost": 1.0}}
        outcome, _ = setu_exec.evaluate_cancel_gates(
            row, positions, [live_order()], set())
        self.assertEqual(outcome, "ok")

    def test_scanner_row_never_cancels_own_order(self):
        order = live_order(order_ref="row-777")
        outcome, detail = setu_exec.evaluate_cancel_gates(
            CANCEL_ROW, {}, [order], {"row-777"})
        self.assertEqual(outcome, "refuse")
        self.assertEqual(detail["gate"], "own_order")

    def test_manual_row_may_cancel_own_order(self):
        row = dict(CANCEL_ROW, source="manual")
        order = live_order(order_ref="row-777")
        outcome, _ = setu_exec.evaluate_cancel_gates(
            row, {}, [order], {"row-777"})
        self.assertEqual(outcome, "ok")

    # -- con_id 0 rows (Kubera-raised: the feed has no con_id) --------
    def test_zero_con_id_matches_descriptively(self):
        row = dict(CANCEL_ROW, con_id=0)
        outcome, _ = setu_exec.evaluate_cancel_gates(
            row, {}, [live_order()], set())
        self.assertEqual(outcome, "ok")

    def test_zero_con_id_strike_mismatch_refuses(self):
        row = dict(CANCEL_ROW, con_id=0)
        outcome, detail = setu_exec.evaluate_cancel_gates(
            row, {}, [live_order(strike=38.0)], set())
        self.assertEqual(outcome, "refuse")
        self.assertEqual(detail["gate"], "identity")

    def test_zero_con_id_symbol_mismatch_refuses(self):
        row = dict(CANCEL_ROW, con_id=0)
        outcome, detail = setu_exec.evaluate_cancel_gates(
            row, {}, [live_order(symbol="RKLB")], set())
        self.assertEqual(outcome, "refuse")
        self.assertEqual(detail["gate"], "identity")

    def test_zero_con_id_right_mismatch_refuses(self):
        row = dict(CANCEL_ROW, con_id=0)
        outcome, detail = setu_exec.evaluate_cancel_gates(
            row, {}, [live_order(right="C")], set())
        self.assertEqual(outcome, "refuse")
        self.assertEqual(detail["gate"], "identity")

    def test_zero_con_id_premise_uses_live_con_id(self):
        row = dict(CANCEL_ROW, con_id=0)
        positions = {111222333: {"con_id": 111222333, "quantity": 100.0,
                                 "avg_cost": 40.0}}
        outcome, detail = setu_exec.evaluate_cancel_gates(
            row, positions, [live_order()], set())
        self.assertEqual(outcome, "refuse")
        self.assertEqual(detail["gate"], "premise")


class NormaliseCancelRowTests(unittest.TestCase):
    def test_legacy_row_defaults_to_place(self):
        raw = {"id": "x", "con_id": 5, "symbol": "ASTS"}
        row = setu_exec.normalise_row(raw)
        self.assertEqual(row["action"], "place")
        self.assertEqual(row["target_order_id"], "")

    def test_cancel_fields_preserved(self):
        row = setu_exec.normalise_row(dict(CANCEL_ROW))
        self.assertEqual(row["action"], "cancel")
        self.assertEqual(row["target_order_id"], str(PERM))
        self.assertEqual(row["source"], "orphan_scan")


class CancelFlowTests(unittest.TestCase):
    def test_simulate_reports_would_execute_and_transmits_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            ex, kub, gw = make_executor(
                tmp, gateway=FakeGateway(orders=[live_order()]))
            ex.process_cancel_row(dict(CANCEL_ROW), {}, gw.fetch_open_orders())
            self.assertIn("would_execute", kub.kinds())
            self.assertEqual(gw.cancel_calls, [])
            self.assertIsNone(ex.tracker.get_cancel(CANCEL_ROW["id"]))

    def test_simulate_gate_failure_is_not_terminal(self):
        with tempfile.TemporaryDirectory() as tmp:
            bad = live_order(filled=2.0, remaining=8.0)
            ex, kub, gw = make_executor(tmp, gateway=FakeGateway(orders=[bad]))
            ex.process_cancel_row(dict(CANCEL_ROW), {}, gw.fetch_open_orders())
            kinds = kub.kinds()
            self.assertIn("would_execute", kinds)
            self.assertNotIn("gate_failed", kinds)
            detail = [d for _, k, d in kub.events if k == "would_execute"][0]
            self.assertTrue(detail.get("simulate_gate_failed"))

    def test_already_gone_event(self):
        with tempfile.TemporaryDirectory() as tmp:
            ex, kub, gw = make_executor(tmp, gateway=FakeGateway(orders=[]))
            ex.process_cancel_row(dict(CANCEL_ROW), {}, [])
            self.assertIn("already_gone", kub.kinds())
            self.assertEqual(gw.cancel_calls, [])

    def test_live_cancel_executes_verifies_and_counts_streak(self):
        with tempfile.TemporaryDirectory() as tmp:
            gw = FakeGateway(orders=[live_order()])
            ex, kub, gw = make_executor(tmp, mode="live", gateway=gw)
            ex.process_cancel_row(dict(CANCEL_ROW), {}, gw.fetch_open_orders())
            self.assertEqual(len(gw.cancel_calls), 1)
            self.assertIn("executed", kub.kinds())
            self.assertIsNone(ex.tracker.get_cancel(CANCEL_ROW["id"]))
            self.assertEqual(ex.tracker.cancel_streak, 1)
            ev = [d for _, k, d in kub.events if k == "executed"][0]
            self.assertTrue(ev.get("verified_absent"))

    def test_live_gateway_refusal_fails_and_resets_streak(self):
        with tempfile.TemporaryDirectory() as tmp:
            gw = FakeGateway(
                orders=[live_order()],
                cancel_result={"ok": False,
                               "error": "IB error 10147: order not cancellable"})
            ex, kub, gw = make_executor(tmp, mode="live", gateway=gw)
            ex.tracker.cancel_streak = 3
            ex.process_cancel_row(dict(CANCEL_ROW), {}, gw.fetch_open_orders())
            self.assertIn("failed", kub.kinds())
            self.assertNotIn("executed", kub.kinds())
            self.assertEqual(ex.tracker.cancel_streak, 0)
            self.assertIsNone(ex.tracker.get_cancel(CANCEL_ROW["id"]))

    def test_reconcile_recovers_transmitted_cancel(self):
        with tempfile.TemporaryDirectory() as tmp:
            ex, kub, gw = make_executor(tmp, mode="live",
                                        gateway=FakeGateway(orders=[]))
            ex.tracker.add_cancel(CANCEL_ROW["id"], {
                "target_order_id": str(PERM), "perm_id": PERM,
                "con_id": 111222333, "transmitted": True})
            ex.reconcile_cancels([])
            self.assertIn("executed", kub.kinds())
            self.assertEqual(ex.tracker.cancel_streak, 1)
            self.assertIsNone(ex.tracker.get_cancel(CANCEL_ROW["id"]))

    def test_reconcile_untransmitted_vanish_is_already_gone(self):
        with tempfile.TemporaryDirectory() as tmp:
            ex, kub, gw = make_executor(tmp, mode="live",
                                        gateway=FakeGateway(orders=[]))
            ex.tracker.add_cancel(CANCEL_ROW["id"], {
                "target_order_id": str(PERM), "perm_id": PERM,
                "con_id": 111222333, "transmitted": False})
            ex.reconcile_cancels([])
            self.assertIn("already_gone", kub.kinds())
            self.assertEqual(ex.tracker.cancel_streak, 0)


class BoundOrderFlowTests(unittest.TestCase):
    """v0.3.2: bound automatic orders carry session order ids
    (negative on Gateways with the negative-binding setting), and
    the runaway brake counts only actual transmissions."""

    def test_simulate_bound_negative_id_would_execute_carries_id(self):
        with tempfile.TemporaryDirectory() as tmp:
            order = live_order(order_id=-4242)
            ex, kub, gw = make_executor(
                tmp, gateway=FakeGateway(orders=[order]))
            ex.process_cancel_row(dict(CANCEL_ROW), {}, gw.fetch_open_orders())
            self.assertIn("would_execute", kub.kinds())
            self.assertNotIn("failed", kub.kinds())
            detail = [d for _, k, d in kub.events if k == "would_execute"][0]
            self.assertEqual(detail.get("order_id"), -4242)
            self.assertEqual(detail.get("perm_id"), PERM)
            self.assertEqual(gw.cancel_calls, [])

    def test_live_bound_negative_id_transmits_and_counts_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            order = live_order(order_id=-4242)
            gw = FakeGateway(orders=[order])
            ex, kub, gw = make_executor(tmp, mode="live", gateway=gw)
            ex.process_cancel_row(dict(CANCEL_ROW), {}, gw.fetch_open_orders())
            self.assertEqual(len(gw.cancel_calls), 1)
            self.assertEqual(gw.cancel_calls[0].get("order_id"), -4242)
            self.assertIn("executed", kub.kinds())
            self.assertEqual(ex.tracker.cancel_streak, 1)
            # The one actual transmission is counted, and it starts
            # the 60s spacing clock.
            self.assertEqual(ex.tracker.placements_today(), 1)
            ok, reason = ex.tracker.brake_check()
            self.assertFalse(ok)
            self.assertIn("transmission", reason)

    def test_live_unbound_zero_id_fails_without_consuming_brake(self):
        with tempfile.TemporaryDirectory() as tmp:
            order = live_order(order_id=0)
            gw = FakeGateway(orders=[order])
            ex, kub, gw = make_executor(tmp, mode="live", gateway=gw)
            ex.tracker.cancel_streak = 2
            ex.process_cancel_row(dict(CANCEL_ROW), {}, gw.fetch_open_orders())
            self.assertIn("failed", kub.kinds())
            detail = [d for _, k, d in kub.events if k == "failed"][0]
            self.assertEqual(detail.get("reason"),
                             "target order carries no session order id")
            # Nothing was transmitted: the gateway was never called,
            # no daily slot was consumed, no spacing clock started,
            # and the streak still resets (it is an execution
            # failure, like a Gateway refusal).
            self.assertEqual(gw.cancel_calls, [])
            self.assertEqual(ex.tracker.placements_today(), 0)
            ok, _ = ex.tracker.brake_check()
            self.assertTrue(ok)
            self.assertEqual(ex.tracker.cancel_streak, 0)
            self.assertIsNone(ex.tracker.get_cancel(CANCEL_ROW["id"]))

    def test_simulate_unbound_zero_id_is_nonterminal_gate_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            order = live_order(order_id=0)
            ex, kub, gw = make_executor(
                tmp, gateway=FakeGateway(orders=[order]))
            ex.process_cancel_row(dict(CANCEL_ROW), {}, gw.fetch_open_orders())
            kinds = kub.kinds()
            self.assertIn("would_execute", kinds)
            self.assertNotIn("failed", kinds)
            self.assertNotIn("gate_failed", kinds)
            detail = [d for _, k, d in kub.events if k == "would_execute"][0]
            self.assertTrue(detail.get("simulate_gate_failed"))
            self.assertEqual(detail.get("gate"), "target")
            self.assertEqual(gw.cancel_calls, [])


class StubClient:
    """Minimal EClient double for ExecGateway-level tests: records
    binding and cancel calls, completes cancels synchronously."""

    def __init__(self, wrapper):
        self.wrapper = wrapper
        self.auto_open_calls = []
        self.cancel_calls = []

    def connect(self, host, port, client_id):
        self.wrapper.nextValidId(1000)

    def run(self):
        return None

    def disconnect(self):
        pass

    def isConnected(self):
        return True

    def reqAutoOpenOrders(self, flag):
        self.auto_open_calls.append(flag)

    def cancelOrder(self, order_id, manual_order_cancel_time=""):
        self.cancel_calls.append(order_id)
        self.wrapper.orderStatus(order_id, "Cancelled", 0.0, 10.0, 0.0,
                                 permId=PERM)


class StubBindingClient:
    """The client-0 side-connection double: records how it was
    used and immediately shows a bound (negative-id) order."""

    def __init__(self, wrapper):
        self.wrapper = wrapper
        self.connect_client_ids = []
        self.auto_open_calls = []
        self.disconnected = False

    def connect(self, host, port, client_id):
        self.connect_client_ids.append(client_id)
        self.wrapper.nextValidId(1)

    def run(self):
        return None

    def disconnect(self):
        self.disconnected = True

    def reqAutoOpenOrders(self, flag):
        self.auto_open_calls.append(flag)
        self.wrapper.openOrder(-5, None, None, None)

    def reqAllOpenOrders(self):
        pass


class GatewayBindingTests(unittest.TestCase):
    def _gateway(self, tmp):
        cfg = make_cfg(tmp)
        gw = setu_exec.ExecGateway(cfg)
        stub = StubClient(gw.wrapper)
        gw.client = stub
        return gw, stub

    def test_connect_asserts_binding_via_side_connection(self):
        with tempfile.TemporaryDirectory() as tmp:
            gw, stub = self._gateway(tmp)
            calls = []
            gw.ensure_binding = lambda: calls.append(True)
            self.assertTrue(gw.connect())
            self.assertEqual(calls, [True])

    def test_ensure_binding_uses_client_zero_and_disconnects(self):
        with tempfile.TemporaryDirectory() as tmp:
            gw, stub = self._gateway(tmp)
            holder = {}

            def make_client(wrapper):
                holder["client"] = StubBindingClient(wrapper)
                return holder["client"]

            gw._make_binding_client = make_client
            gw.ensure_binding()
            binding = holder["client"]
            self.assertEqual(binding.connect_client_ids, [0])
            self.assertEqual(binding.auto_open_calls, [True])
            self.assertTrue(binding.disconnected)

    def test_cancel_order_accepts_negative_bound_id(self):
        with tempfile.TemporaryDirectory() as tmp:
            gw, stub = self._gateway(tmp)
            result = gw.cancel_order({"order_id": -5, "perm_id": PERM})
            self.assertTrue(result.get("ok"))
            self.assertEqual(stub.cancel_calls, [-5])

    def test_cancel_order_zero_id_refused_without_a_call(self):
        with tempfile.TemporaryDirectory() as tmp:
            gw, stub = self._gateway(tmp)
            result = gw.cancel_order({"order_id": 0, "perm_id": PERM})
            self.assertFalse(result.get("ok"))
            self.assertEqual(result.get("error"),
                             "target order carries no session order id")
            self.assertEqual(stub.cancel_calls, [])


if __name__ == "__main__":
    unittest.main()
