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
    """fetch_open_orders reflects cancels: once a cancel succeeds,
    the target disappears from subsequent reads. Cancels route by
    session-id sign (v0.3.3): cancel_calls is the MAIN client's
    transmissions (positive ids), bound_cancel_calls the client-0
    side-connection's (negative ids). Both honour on_transmit the
    way the real gateway does — at the moment of the actual call,
    and only when a call is really made."""

    def __init__(self, positions=None, orders=None, cancel_result=None,
                 bound_cancel_result=None, probe_visible=True):
        self.positions = positions or {}
        self.orders = list(orders or [])
        self.cancel_result = cancel_result or {"ok": True, "status": "Cancelled"}
        self.bound_cancel_result = bound_cancel_result or {
            "ok": True, "transmitted": True, "status": "Cancelled"}
        self.probe_visible = probe_visible
        self.cancel_calls = []
        self.bound_cancel_calls = []
        self.probe_calls = []

    def fetch_positions(self):
        return dict(self.positions)

    def fetch_open_orders(self):
        return list(self.orders)

    def _apply_cancel(self, order):
        self.orders = [o for o in self.orders
                       if o.get("perm_id") != order.get("perm_id")]

    def cancel_order(self, order, on_transmit=None):
        self.cancel_calls.append(order)
        if on_transmit is not None:
            on_transmit()
        if self.cancel_result.get("ok"):
            self._apply_cancel(order)
        return dict(self.cancel_result)

    def cancel_bound_order(self, order, on_transmit=None):
        self.bound_cancel_calls.append(order)
        result = dict(self.bound_cancel_result)
        if result.get("transmitted") and on_transmit is not None:
            on_transmit()
        if result.get("ok"):
            self._apply_cancel(order)
        return result

    def probe_bound_target(self, order):
        self.probe_calls.append(order)
        if self.probe_visible:
            return {"visible": True,
                    "order_id": order.get("order_id"), "error": None}
        return {"visible": False, "order_id": None,
                "error": "target not visible in the client-0 bound view"}


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
            # v0.3.3 routing: the transmission rides the client-0
            # side-connection; the main client never sees it.
            self.assertEqual(len(gw.bound_cancel_calls), 1)
            self.assertEqual(gw.bound_cancel_calls[0].get("order_id"), -4242)
            self.assertEqual(gw.cancel_calls, [])
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


class BoundRoutingFlowTests(unittest.TestCase):
    """v0.3.3: cancels route by the sign of the resolved session
    id — negative (bound manual orders) ride a client-0
    side-connection, positive ids stay on the main client — and
    the runaway brake counts exactly one transmission per actual
    cancelOrder call, whichever connection carries it."""

    def test_live_positive_id_stays_on_main_client(self):
        with tempfile.TemporaryDirectory() as tmp:
            gw = FakeGateway(orders=[live_order()])  # order_id 8811
            ex, kub, gw = make_executor(tmp, mode="live", gateway=gw)
            ex.process_cancel_row(dict(CANCEL_ROW), {}, gw.fetch_open_orders())
            self.assertEqual(len(gw.cancel_calls), 1)
            self.assertEqual(gw.bound_cancel_calls, [])
            self.assertEqual(gw.probe_calls, [])
            self.assertIn("executed", kub.kinds())
            self.assertEqual(ex.tracker.placements_today(), 1)

    def test_live_bound_side_not_visible_transmits_nothing_no_brake(self):
        with tempfile.TemporaryDirectory() as tmp:
            order = live_order(order_id=-4242)
            gw = FakeGateway(
                orders=[order],
                bound_cancel_result={
                    "ok": False, "transmitted": False,
                    "error": "target order not visible in the client-0 "
                             "bound view (perm 349464471: session id "
                             "absent, expected -4242); nothing transmitted"})
            ex, kub, gw = make_executor(tmp, mode="live", gateway=gw)
            ex.tracker.cancel_streak = 2
            ex.process_cancel_row(dict(CANCEL_ROW), {}, gw.fetch_open_orders())
            self.assertIn("failed", kub.kinds())
            self.assertNotIn("executed", kub.kinds())
            detail = [d for _, k, d in kub.events if k == "failed"][0]
            self.assertIn("not visible in the client-0 bound view",
                          detail.get("reason", ""))
            # The side session WAS consulted, but no cancelOrder was
            # issued: no brake slot, no spacing clock — and the
            # failure still resets the streak, like any execution
            # failure.
            self.assertEqual(len(gw.bound_cancel_calls), 1)
            self.assertEqual(gw.cancel_calls, [])
            self.assertEqual(ex.tracker.placements_today(), 0)
            ok, _ = ex.tracker.brake_check()
            self.assertTrue(ok)
            self.assertEqual(ex.tracker.cancel_streak, 0)
            self.assertIsNone(ex.tracker.get_cancel(CANCEL_ROW["id"]))

    def test_live_bound_transmitted_refusal_counts_once_and_resets(self):
        with tempfile.TemporaryDirectory() as tmp:
            order = live_order(order_id=-4242)
            gw = FakeGateway(
                orders=[order],
                bound_cancel_result={
                    "ok": False, "transmitted": True,
                    "error": "IB error 10147: OrderId -4242 that needs "
                             "to be cancelled is not found."})
            ex, kub, gw = make_executor(tmp, mode="live", gateway=gw)
            ex.tracker.cancel_streak = 3
            ex.process_cancel_row(dict(CANCEL_ROW), {}, gw.fetch_open_orders())
            self.assertIn("failed", kub.kinds())
            self.assertEqual(ex.tracker.placements_today(), 1)
            self.assertEqual(ex.tracker.cancel_streak, 0)

    def test_simulate_bound_reports_route_and_side_confirmation(self):
        with tempfile.TemporaryDirectory() as tmp:
            order = live_order(order_id=-4242)
            ex, kub, gw = make_executor(
                tmp, gateway=FakeGateway(orders=[order]))
            ex.process_cancel_row(dict(CANCEL_ROW), {}, gw.fetch_open_orders())
            self.assertIn("would_execute", kub.kinds())
            self.assertNotIn("failed", kub.kinds())
            detail = [d for _, k, d in kub.events if k == "would_execute"][0]
            self.assertEqual(detail.get("route"), "client0_side_connection")
            self.assertTrue(detail.get("side_session_visible"))
            self.assertEqual(detail.get("side_session_order_id"), -4242)
            self.assertEqual(detail.get("order_id"), -4242)
            # The probe ran; nothing was transmitted anywhere.
            self.assertEqual(len(gw.probe_calls), 1)
            self.assertEqual(gw.cancel_calls, [])
            self.assertEqual(gw.bound_cancel_calls, [])

    def test_simulate_bound_probe_not_visible_is_nonterminal(self):
        with tempfile.TemporaryDirectory() as tmp:
            order = live_order(order_id=-4242)
            ex, kub, gw = make_executor(
                tmp, gateway=FakeGateway(orders=[order], probe_visible=False))
            ex.process_cancel_row(dict(CANCEL_ROW), {}, gw.fetch_open_orders())
            kinds = kub.kinds()
            self.assertIn("would_execute", kinds)
            self.assertNotIn("failed", kinds)
            self.assertNotIn("gate_failed", kinds)
            detail = [d for _, k, d in kub.events if k == "would_execute"][0]
            self.assertTrue(detail.get("simulate_gate_failed"))
            self.assertEqual(detail.get("gate"), "target")
            self.assertEqual(gw.bound_cancel_calls, [])


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


class StubSideCancelClient:
    """Client-0 side-session double for the bound-cancel route
    (v0.3.3): answers the bound-view read from `view`
    (perm id -> session id) and completes a cancel synchronously
    through the wrapper, the way the real Gateway does."""

    def __init__(self, wrapper, view=None, cancel_error=None,
                 cancel_error_id=None):
        from types import SimpleNamespace
        self._ns = SimpleNamespace
        self.wrapper = wrapper
        self.view = {PERM: -5} if view is None else view
        self.cancel_error = cancel_error  # (code, message) or None
        # When set, the error is delivered under this reqId instead
        # of the cancelled order's own id (an UNTIED error, v0.3.4).
        self.cancel_error_id = cancel_error_id
        self.connect_client_ids = []
        self.auto_open_calls = []
        self.cancel_calls = []
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

    def reqAllOpenOrders(self):
        for perm, oid in self.view.items():
            self.wrapper.openOrder(oid, None,
                                   self._ns(permId=perm), None)
        self.wrapper.openOrderEnd()

    def cancelOrder(self, order_id, manual_order_cancel_time=""):
        self.cancel_calls.append(order_id)
        if self.cancel_error is not None:
            code, message = self.cancel_error
            rid = (self.cancel_error_id if self.cancel_error_id is not None
                   else order_id)
            self.wrapper.error(rid, code, message)
            return
        self.wrapper.orderStatus(order_id, "Cancelled", 0.0, 10.0, 0.0,
                                 permId=PERM)


class StubMain202Client:
    """Main-client double answering a cancel the way the real
    Gateway confirmed the 2026-10-25 live cancel: error 202
    ("Order Canceled") through the error callback, reqId = the
    cancelled order's session id (v0.3.4). With error_id set, the
    202 is delivered under a DIFFERENT order's id — untied — and
    the target merely reports a working status."""

    def __init__(self, wrapper, error_id=None):
        self.wrapper = wrapper
        self.error_id = error_id
        self.cancel_calls = []

    def connect(self, host, port, client_id):
        self.wrapper.nextValidId(1000)

    def run(self):
        return None

    def disconnect(self):
        pass

    def isConnected(self):
        return True

    def cancelOrder(self, order_id, manual_order_cancel_time=""):
        self.cancel_calls.append(order_id)
        rid = self.error_id if self.error_id is not None else order_id
        self.wrapper.error(rid, 202, "Order Canceled - reason:")
        if rid != order_id:
            # Nothing terminal for the target itself; give the
            # gateway's await a working status so the method
            # returns its no-confirmation verdict at once.
            self.wrapper.orderStatus(order_id, "PreSubmitted", 0.0,
                                     10.0, 0.0, permId=PERM)


class GatewayBoundCancelTests(unittest.TestCase):
    """v0.3.3 gateway level: the bound-order cancel route itself,
    against client doubles. The main client is the StubClient from
    the binding tests; the side session is StubSideCancelClient."""

    def _gateway(self, tmp, view=None, cancel_error=None):
        cfg = make_cfg(tmp)
        gw = setu_exec.ExecGateway(cfg)
        main = StubClient(gw.wrapper)
        gw.client = main
        holder = {}

        def make_client(wrapper):
            holder["client"] = StubSideCancelClient(
                wrapper, view=view, cancel_error=cancel_error)
            return holder["client"]

        gw._make_binding_client = make_client
        return gw, main, holder

    def test_bound_cancel_gateway_error_surfaces_verbatim(self):
        # Regression guard: the side wrapper's error() callback
        # must stay callable (an instance attribute named "error"
        # would shadow it and the refusal would surface as a
        # TypeError instead of the Gateway's words).
        with tempfile.TemporaryDirectory() as tmp:
            gw, main, holder = self._gateway(
                tmp, cancel_error=(10147, "OrderId -5 that needs to be "
                                          "cancelled is not found."))
            fired = []
            result = gw.cancel_bound_order(
                {"order_id": -5, "perm_id": PERM},
                on_transmit=lambda: fired.append(True))
            self.assertFalse(result.get("ok"))
            self.assertTrue(result.get("transmitted"))
            self.assertIn("10147", result.get("error", ""))
            self.assertEqual(holder["client"].cancel_calls, [-5])
            self.assertEqual(fired, [True])

    def test_bound_cancel_transmits_via_client0_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            gw, main, holder = self._gateway(tmp)
            fired = []
            result = gw.cancel_bound_order(
                {"order_id": -5, "perm_id": PERM},
                on_transmit=lambda: fired.append(True))
            self.assertTrue(result.get("ok"))
            self.assertTrue(result.get("transmitted"))
            side = holder["client"]
            self.assertEqual(side.connect_client_ids, [0])
            self.assertEqual(side.cancel_calls, [-5])
            self.assertTrue(side.disconnected)
            # The main client never saw a cancelOrder, and the
            # brake callback fired exactly once.
            self.assertEqual(main.cancel_calls, [])
            self.assertEqual(fired, [True])

    def test_bound_cancel_not_visible_transmits_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            gw, main, holder = self._gateway(tmp, view={})
            fired = []
            result = gw.cancel_bound_order(
                {"order_id": -5, "perm_id": PERM},
                on_transmit=lambda: fired.append(True))
            self.assertFalse(result.get("ok"))
            self.assertFalse(result.get("transmitted"))
            self.assertIn("not visible in the client-0 bound view",
                          result.get("error", ""))
            side = holder["client"]
            self.assertEqual(side.cancel_calls, [])
            self.assertTrue(side.disconnected)
            self.assertEqual(main.cancel_calls, [])
            self.assertEqual(fired, [])

    def test_bound_cancel_wrong_session_id_transmits_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            gw, main, holder = self._gateway(tmp, view={PERM: -9})
            result = gw.cancel_bound_order({"order_id": -5, "perm_id": PERM})
            self.assertFalse(result.get("ok"))
            self.assertFalse(result.get("transmitted"))
            self.assertEqual(holder["client"].cancel_calls, [])

    def test_bound_cancel_refuses_positive_id_without_a_session(self):
        with tempfile.TemporaryDirectory() as tmp:
            gw, main, holder = self._gateway(tmp)
            result = gw.cancel_bound_order({"order_id": 8811, "perm_id": PERM})
            self.assertFalse(result.get("ok"))
            self.assertFalse(result.get("transmitted"))
            self.assertNotIn("client", holder)  # no side session opened
            self.assertEqual(main.cancel_calls, [])

    def test_probe_bound_target_visible_then_absent(self):
        with tempfile.TemporaryDirectory() as tmp:
            gw, main, holder = self._gateway(tmp)
            probe = gw.probe_bound_target({"order_id": -5, "perm_id": PERM})
            self.assertTrue(probe.get("visible"))
            self.assertEqual(probe.get("order_id"), -5)
            self.assertTrue(holder["client"].disconnected)
            self.assertEqual(main.cancel_calls, [])
        with tempfile.TemporaryDirectory() as tmp:
            gw, main, holder = self._gateway(tmp, view={})
            probe = gw.probe_bound_target({"order_id": -5, "perm_id": PERM})
            self.assertFalse(probe.get("visible"))
            self.assertIsNone(probe.get("order_id"))

    def test_main_cancel_order_fires_on_transmit(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = make_cfg(tmp)
            gw = setu_exec.ExecGateway(cfg)
            stub = StubClient(gw.wrapper)
            gw.client = stub
            fired = []
            result = gw.cancel_order({"order_id": 8811, "perm_id": PERM},
                                     on_transmit=lambda: fired.append(True))
            self.assertTrue(result.get("ok"))
            self.assertEqual(stub.cancel_calls, [8811])
            self.assertEqual(fired, [True])


class GatewayCancel202Tests(unittest.TestCase):
    """v0.3.4: IB error 202 ("Order Canceled") is the Gateway's
    cancellation CONFIRMATION, delivered through the error
    callback. Tied to the target session order id it counts as
    the cancel having landed, on BOTH routes; tied to any other
    id it counts for nothing. (The 10147 refusal behaviour is
    unchanged — see test_bound_cancel_gateway_error_surfaces_verbatim.)"""

    def test_main_cancel_answered_by_202_is_a_cancellation(self):
        with tempfile.TemporaryDirectory() as tmp:
            gw = setu_exec.ExecGateway(make_cfg(tmp))
            stub = StubMain202Client(gw.wrapper)
            gw.client = stub
            fired = []
            result = gw.cancel_order({"order_id": 8811, "perm_id": PERM},
                                     on_transmit=lambda: fired.append(True))
            self.assertTrue(result.get("ok"))
            self.assertEqual(result.get("status"), "Cancelled")
            self.assertEqual(stub.cancel_calls, [8811])
            self.assertEqual(fired, [True])

    def test_main_cancel_untied_202_is_not_a_cancellation(self):
        with tempfile.TemporaryDirectory() as tmp:
            gw = setu_exec.ExecGateway(make_cfg(tmp))
            stub = StubMain202Client(gw.wrapper, error_id=777)
            gw.client = stub
            result = gw.cancel_order({"order_id": 8811, "perm_id": PERM})
            self.assertFalse(result.get("ok"))
            self.assertIn("no cancellation confirmation",
                          result.get("error", ""))
            self.assertEqual(stub.cancel_calls, [8811])

    def test_bound_cancel_answered_by_202_is_a_cancellation(self):
        # The exact wire shape of the 2026-10-25 live event: the
        # side session transmits, the Gateway answers error 202
        # under the target's own (negative) session id.
        with tempfile.TemporaryDirectory() as tmp:
            gw = setu_exec.ExecGateway(make_cfg(tmp))
            gw.client = StubClient(gw.wrapper)
            holder = {}

            def make_client(wrapper):
                holder["client"] = StubSideCancelClient(
                    wrapper, cancel_error=(202, "Order Canceled - reason:"))
                return holder["client"]

            gw._make_binding_client = make_client
            fired = []
            result = gw.cancel_bound_order(
                {"order_id": -5, "perm_id": PERM},
                on_transmit=lambda: fired.append(True))
            self.assertTrue(result.get("ok"))
            self.assertTrue(result.get("transmitted"))
            self.assertEqual(result.get("status"), "Cancelled")
            self.assertEqual(holder["client"].cancel_calls, [-5])
            self.assertTrue(holder["client"].disconnected)
            self.assertEqual(fired, [True])

    def test_bound_cancel_untied_202_is_not_a_cancellation(self):
        # A 202 naming some OTHER order settles nothing for the
        # target: the side wrapper never records it, no terminal
        # callback arrives, and the gateway reports its ordinary
        # no-confirmation failure. (This runs out the gateway's
        # real 12s confirmation wait — that IS the behaviour when
        # the Gateway never confirms.)
        with tempfile.TemporaryDirectory() as tmp:
            gw = setu_exec.ExecGateway(make_cfg(tmp))
            gw.client = StubClient(gw.wrapper)
            holder = {}

            def make_client(wrapper):
                holder["client"] = StubSideCancelClient(
                    wrapper, cancel_error=(202, "Order Canceled - reason:"),
                    cancel_error_id=-999)
                return holder["client"]

            gw._make_binding_client = make_client
            result = gw.cancel_bound_order({"order_id": -5, "perm_id": PERM})
            self.assertFalse(result.get("ok"))
            self.assertTrue(result.get("transmitted"))
            self.assertIn("no cancellation confirmation",
                          result.get("error", ""))
            self.assertEqual(holder["client"].cancel_calls, [-5])


class RealGatewayHybrid:
    """Executor-facing gateway double whose cancel methods are the
    REAL ExecGateway ones (wired to stub IB clients), while reads
    come from a local list — so a test can run the wire-level
    classification (error 202) through the whole executor flow,
    verification read included. drop_on_cancel=False models a
    cancel the Gateway confirmed but the next read still shows:
    the executor's unverified case."""

    def __init__(self, real_gw, orders, drop_on_cancel=True):
        self.real = real_gw
        self.orders = list(orders)
        self.drop_on_cancel = drop_on_cancel

    def fetch_positions(self):
        return {}

    def fetch_open_orders(self):
        return list(self.orders)

    def _apply(self, order, result):
        if result.get("ok") and self.drop_on_cancel:
            self.orders = [o for o in self.orders
                           if o.get("perm_id") != order.get("perm_id")]

    def cancel_order(self, order, on_transmit=None):
        result = self.real.cancel_order(order, on_transmit=on_transmit)
        self._apply(order, result)
        return result

    def cancel_bound_order(self, order, on_transmit=None):
        result = self.real.cancel_bound_order(
            order, on_transmit=on_transmit)
        self._apply(order, result)
        return result

    def probe_bound_target(self, order):
        return {"visible": True, "order_id": order.get("order_id"),
                "error": None}


class Cancel202FlowTests(unittest.TestCase):
    """v0.3.4 end to end: a cancel answered by error 202 runs
    through the executor's unchanged fresh-read verification —
    target absent posts `executed` and increments the streak, on
    both routes. Target still present is NOT an executed claim:
    the existing unverified path (no executed event, no streak
    increment, record left tracked for reconciliation)."""

    def _live_executor(self, tmp, gateway):
        cfg = make_cfg(tmp, mode="live", live_file=True)
        kub = FakeKubera([])
        tracker = setu_exec.PlacedTracker(cfg)
        ex = setu_exec.Executor(cfg, kub, gateway, tracker)
        return ex, kub

    def _bound_real_gateway(self, tmp):
        real = setu_exec.ExecGateway(make_cfg(tmp))
        real.client = StubClient(real.wrapper)

        def make_client(wrapper):
            return StubSideCancelClient(
                wrapper, cancel_error=(202, "Order Canceled - reason:"))

        real._make_binding_client = make_client
        return real

    def test_live_main_cancel_202_executes_and_counts_streak(self):
        with tempfile.TemporaryDirectory() as tmp:
            real = setu_exec.ExecGateway(make_cfg(tmp))
            stub = StubMain202Client(real.wrapper)
            real.client = stub
            hybrid = RealGatewayHybrid(real, [live_order()])
            ex, kub = self._live_executor(tmp, hybrid)
            ex.process_cancel_row(dict(CANCEL_ROW), {},
                                  hybrid.fetch_open_orders())
            self.assertEqual(stub.cancel_calls, [8811])
            self.assertIn("executed", kub.kinds())
            self.assertNotIn("failed", kub.kinds())
            ev = [d for _, k, d in kub.events if k == "executed"][0]
            self.assertTrue(ev.get("verified_absent"))
            self.assertEqual(ev.get("cancel_streak"), 1)
            self.assertEqual(ex.tracker.cancel_streak, 1)
            self.assertEqual(ex.tracker.placements_today(), 1)
            self.assertIsNone(ex.tracker.get_cancel(CANCEL_ROW["id"]))

    def test_live_bound_cancel_202_executes_and_counts_streak(self):
        with tempfile.TemporaryDirectory() as tmp:
            real = self._bound_real_gateway(tmp)
            hybrid = RealGatewayHybrid(real, [live_order(order_id=-5)])
            ex, kub = self._live_executor(tmp, hybrid)
            ex.process_cancel_row(dict(CANCEL_ROW), {},
                                  hybrid.fetch_open_orders())
            self.assertIn("executed", kub.kinds())
            self.assertNotIn("failed", kub.kinds())
            ev = [d for _, k, d in kub.events if k == "executed"][0]
            self.assertTrue(ev.get("verified_absent"))
            self.assertEqual(ev.get("cancel_streak"), 1)
            self.assertEqual(ex.tracker.cancel_streak, 1)
            self.assertEqual(ex.tracker.placements_today(), 1)
            self.assertIsNone(ex.tracker.get_cancel(CANCEL_ROW["id"]))

    def test_live_bound_cancel_202_still_present_is_not_executed(self):
        with tempfile.TemporaryDirectory() as tmp:
            real = self._bound_real_gateway(tmp)
            hybrid = RealGatewayHybrid(real, [live_order(order_id=-5)],
                                       drop_on_cancel=False)
            ex, kub = self._live_executor(tmp, hybrid)
            ex.tracker.cancel_streak = 2
            ex.process_cancel_row(dict(CANCEL_ROW), {},
                                  hybrid.fetch_open_orders())
            # The 202 landed, the transmission counted — but the
            # verification read still shows the target, so the
            # executor claims nothing: no executed, no failed, the
            # streak untouched either way, the record left tracked
            # for reconcile_cancels to resolve.
            self.assertNotIn("executed", kub.kinds())
            self.assertNotIn("failed", kub.kinds())
            self.assertEqual(ex.tracker.cancel_streak, 2)
            self.assertEqual(ex.tracker.placements_today(), 1)
            self.assertIsNotNone(ex.tracker.get_cancel(CANCEL_ROW["id"]))


if __name__ == "__main__":
    unittest.main()
