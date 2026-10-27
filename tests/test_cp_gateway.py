"""Tests for setu-exec v0.9: the Client Portal Gateway transport
for the web cancel route.

The cancel machinery (gates, identity match, verification) is
shared with the OAuth transport and covered by
tests/test_web_cancel.py. This file covers what v0.9 adds: the
mode-selecting credentials loader, the CpGatewayClient session
semantics (authenticated state is the arbiter), the executor's
per-cycle session tickle, one end-to-end Class B flow over the
CP transport, and the probe's Client Portal branch.

Synthetic data only; fake transports/clients throughout.

Run: python3 -m unittest discover tests
"""
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import setu_exec  # noqa: E402
from test_web_cancel import (  # noqa: E402
    ACCOUNT, WEB_ROW, FakeWebClient, FlowGateway, FlowKubera,
    class_b_order, make_flow_cfg, web_entry,
)
import webapi_probe  # noqa: E402


def write_cp_config(tmp, **overrides):
    data = {"mode": "client_portal"}
    data.update(overrides)
    path = Path(tmp) / f"webapi-{ACCOUNT}.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    path.chmod(0o600)
    return path


class CpLoaderTests(unittest.TestCase):
    def load(self, tmp, enforce=False):
        return setu_exec.load_webapi_credentials(
            {"_config_dir": tmp}, ACCOUNT, enforce_permissions=enforce)

    def test_default_base_url(self):
        with tempfile.TemporaryDirectory() as tmp:
            write_cp_config(tmp)
            creds = self.load(tmp)
            self.assertIsInstance(creds, setu_exec.CpGatewayCredentials)
            self.assertEqual(creds.mode, "client_portal")
            self.assertEqual(creds.base_url,
                             "https://localhost:5000/v1/api")

    def test_explicit_base_url_trailing_slash_stripped(self):
        with tempfile.TemporaryDirectory() as tmp:
            write_cp_config(tmp, base_url="https://localhost:5001/v1/api/")
            creds = self.load(tmp)
            self.assertEqual(creds.base_url,
                             "https://localhost:5001/v1/api")

    def test_non_url_base_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            write_cp_config(tmp, base_url="localhost:5000")
            with self.assertRaises(setu_exec.WebApiError):
                self.load(tmp)

    def test_unknown_mode_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            write_cp_config(tmp, mode="carrier_pigeon")
            with self.assertRaises(setu_exec.WebApiError):
                self.load(tmp)

    def test_factory_selects_cp_client(self):
        creds = setu_exec.CpGatewayCredentials("https://localhost:5000/v1/api")
        client = setu_exec.build_webapi_client(creds)
        self.assertIsInstance(client, setu_exec.CpGatewayClient)


class CpClientTests(unittest.TestCase):
    """The fake transport speaks in (status, payload) like the
    real one and records every call."""

    def make_client(self, responder):
        calls = []

        def transport(method, url, headers, body):
            calls.append((method, url, body))
            return responder(method, url, body)

        client = setu_exec.CpGatewayClient(
            "https://localhost:5000/v1/api", transport=transport)
        return client, calls

    def test_init_proves_authenticated_state(self):
        def responder(method, url, body):
            if url.endswith("/iserver/auth/ssodh/init"):
                self.assertEqual(json.loads(body.decode("utf-8")),
                                 {"publish": True, "compete": True})
                return 200, {}
            if url.endswith("/iserver/auth/status"):
                return 200, {"authenticated": True, "competing": False}
            raise AssertionError(url)

        client, _ = self.make_client(responder)
        status = client.init_brokerage_session()
        self.assertTrue(status["authenticated"])

    def test_init_refuses_unauthenticated_session(self):
        def responder(method, url, body):
            if url.endswith("/iserver/auth/status"):
                return 200, {"authenticated": False}
            return 200, {}

        client, _ = self.make_client(responder)
        with self.assertRaises(setu_exec.WebApiError) as ctx:
            client.init_brokerage_session()
        self.assertIn("not authenticated", str(ctx.exception))

    def test_init_tolerates_ssodh_error_when_authenticated(self):
        def responder(method, url, body):
            if url.endswith("/iserver/auth/ssodh/init"):
                return 500, {"error": "session already established"}
            if url.endswith("/iserver/auth/status"):
                return 200, {"authenticated": True}
            raise AssertionError(url)

        client, _ = self.make_client(responder)
        status = client.init_brokerage_session()
        self.assertTrue(status["authenticated"])

    def test_orders_and_cancel_paths(self):
        seen = []

        def responder(method, url, body):
            seen.append((method, url))
            if url.endswith("/iserver/account/orders"):
                return 200, {"orders": [web_entry()]}
            return 200, {"msg": ["Request was submitted"]}

        client, _ = self.make_client(responder)
        orders = client.live_orders()
        self.assertEqual(len(orders), 1)
        client.cancel_order(ACCOUNT, "829711680")
        self.assertIn(
            ("DELETE",
             "https://localhost:5000/v1/api/iserver/account/"
             f"{ACCOUNT}/order/829711680"),
            seen)

    def test_http_error_raises(self):
        def responder(method, url, body):
            return 503, {"error": "gateway down"}

        client, _ = self.make_client(responder)
        with self.assertRaises(setu_exec.WebApiError):
            client.live_orders()

    def test_tickle_posts(self):
        def responder(method, url, body):
            self.assertTrue(url.endswith("/tickle"))
            return 200, {"iserver": {"authStatus":
                                     {"authenticated": True}}}

        client, _ = self.make_client(responder)
        payload = client.tickle()
        self.assertTrue(
            payload["iserver"]["authStatus"]["authenticated"])


class CpWarmUpTests(unittest.TestCase):
    """v0.9.1: the iserver orders endpoint answers the first call
    after session init with an empty stub (proven live 2026-10-27:
    0 orders, then the full blotter). live_orders must wait and
    re-fetch a bounded number of times; a non-empty list returns
    at once; a list that stays empty is returned as-is."""

    def make_client(self, responder):
        calls = []
        sleeps = []

        def transport(method, url, headers, body):
            calls.append((method, url))
            return responder(method, url, body)

        client = setu_exec.CpGatewayClient(
            "https://localhost:5000/v1/api", transport=transport,
            orders_sleeper=sleeps.append)
        return client, calls, sleeps

    def test_warm_up_retries_until_blotter_arrives(self):
        state = {"n": 0}

        def responder(method, url, body):
            state["n"] += 1
            if state["n"] == 1:
                return 200, {"orders": []}
            return 200, {"orders": [web_entry()]}

        client, calls, sleeps = self.make_client(responder)
        orders = client.live_orders()
        self.assertEqual(len(orders), 1)
        self.assertEqual(len(calls), 2)
        self.assertEqual(sleeps, [setu_exec._ORDERS_WARM_INTERVAL_S])

    def test_warm_up_gives_up_after_bounded_attempts(self):
        def responder(method, url, body):
            return 200, {"orders": []}

        client, calls, sleeps = self.make_client(responder)
        self.assertEqual(client.live_orders(), [])
        self.assertEqual(len(calls), setu_exec._ORDERS_WARM_ATTEMPTS)
        self.assertEqual(len(sleeps),
                         setu_exec._ORDERS_WARM_ATTEMPTS - 1)

    def test_nonempty_list_returns_immediately(self):
        def responder(method, url, body):
            return 200, {"orders": [web_entry()]}

        client, calls, sleeps = self.make_client(responder)
        self.assertEqual(len(client.live_orders()), 1)
        self.assertEqual(len(calls), 1)
        self.assertEqual(sleeps, [])

    def test_http_error_is_not_retried(self):
        def responder(method, url, body):
            return 401, {"error": "not authenticated"}

        client, calls, sleeps = self.make_client(responder)
        with self.assertRaises(setu_exec.WebApiError):
            client.live_orders()
        self.assertEqual(len(calls), 1)
        self.assertEqual(sleeps, [])


class FakeCpClient(FakeWebClient):
    """FakeWebClient plus the CP tickle; auth state scriptable."""

    def __init__(self, *args, authenticated=True, **kwargs):
        super().__init__(*args, **kwargs)
        self.authenticated = authenticated
        self.tickle_calls = 0

    def init_brokerage_session(self):
        self.init_calls += 1
        if not self.authenticated:
            raise setu_exec.WebApiError(
                "Client Portal Gateway session is not authenticated")
        return {"authenticated": True}

    def auth_status(self):
        return {"authenticated": self.authenticated}

    def session_accounts(self):
        return [ACCOUNT]

    def tickle(self):
        self.tickle_calls += 1
        return {"iserver": {"authStatus":
                            {"authenticated": self.authenticated}}}


def make_executor(tmp, mode, gateway):
    cfg = make_flow_cfg(tmp, mode=mode)
    kubera = FlowKubera([])
    tracker = setu_exec.PlacedTracker(cfg)
    executor = setu_exec.Executor(cfg, kubera, gateway, tracker)
    executor._web_verify_attempts = 3
    executor._web_verify_interval = 0
    return executor, kubera


class CpTickleTests(unittest.TestCase):
    def test_tickle_marks_session_up(self):
        with tempfile.TemporaryDirectory() as tmp:
            write_cp_config(tmp)
            fake = FakeCpClient([web_entry()])
            executor, _ = make_executor(tmp, "live", FlowGateway([]))
            with mock.patch.object(setu_exec, "build_webapi_client",
                                   return_value=fake):
                executor._tickle_web_session()
            self.assertEqual(fake.tickle_calls, 1)
            self.assertEqual(executor._web_session_state, "up")

    def test_tickle_marks_session_down_when_unauthenticated(self):
        with tempfile.TemporaryDirectory() as tmp:
            write_cp_config(tmp)
            fake = FakeCpClient([web_entry()], authenticated=False)
            executor, _ = make_executor(tmp, "live", FlowGateway([]))
            with mock.patch.object(setu_exec, "build_webapi_client",
                                   return_value=fake):
                executor._tickle_web_session()
            self.assertEqual(executor._web_session_state, "down")

    def test_tickle_failure_marks_down_and_never_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            write_cp_config(tmp)
            fake = FakeCpClient([web_entry()])

            def boom():
                raise setu_exec.WebApiError("connection refused")

            fake.tickle = boom
            executor, _ = make_executor(tmp, "live", FlowGateway([]))
            with mock.patch.object(setu_exec, "build_webapi_client",
                                   return_value=fake):
                executor._tickle_web_session()
            self.assertEqual(executor._web_session_state, "down")

    def test_no_tickle_without_cp_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            executor, _ = make_executor(tmp, "live", FlowGateway([]))
            with mock.patch.object(
                    setu_exec, "build_webapi_client") as builder:
                executor._tickle_web_session()
            builder.assert_not_called()
            self.assertEqual(executor._web_session_state, "unknown")


class CpExecutorFlowTests(unittest.TestCase):
    def test_live_class_b_executes_over_cp_transport(self):
        with tempfile.TemporaryDirectory() as tmp:
            write_cp_config(tmp)
            gateway = FlowGateway([class_b_order()])

            def drop_from_socket_feed():
                gateway.orders = []

            fake = FakeCpClient([web_entry()],
                                on_cancel=drop_from_socket_feed)
            executor, kubera = make_executor(tmp, "live", gateway)
            with mock.patch.object(setu_exec, "build_webapi_client",
                                   return_value=fake):
                executor.process_cancel_row(
                    dict(WEB_ROW), {}, gateway.fetch_open_orders())
            executed = kubera.details("executed")
            self.assertEqual(len(executed), 1)
            self.assertEqual(executed[0]["route"], "web")
            self.assertEqual(fake.cancel_calls,
                             [(ACCOUNT, "829711680")])

    def test_live_class_b_refuses_when_cp_session_down(self):
        with tempfile.TemporaryDirectory() as tmp:
            write_cp_config(tmp)
            gateway = FlowGateway([class_b_order()])
            fake = FakeCpClient([web_entry()], authenticated=False)
            executor, kubera = make_executor(tmp, "live", gateway)
            with mock.patch.object(setu_exec, "build_webapi_client",
                                   return_value=fake):
                executor.process_cancel_row(
                    dict(WEB_ROW), {}, gateway.fetch_open_orders())
            refused = kubera.details("gate_failed")
            self.assertEqual(len(refused), 1)
            self.assertEqual(refused[0]["gate"], "web_session")
            self.assertIn("not authenticated", refused[0]["reason"])
            self.assertEqual(fake.cancel_calls, [])
            self.assertEqual(executor.tracker.placements_today(), 0)


class CpProbeTests(unittest.TestCase):
    def run_probe(self, tmp, fake, *extra_argv):
        argv = ["webapi_probe.py", "--account", ACCOUNT,
                "--config-dir", tmp, *extra_argv]
        with mock.patch.object(sys, "argv", argv), \
                mock.patch.object(setu_exec, "build_webapi_client",
                                  return_value=fake):
            return webapi_probe.main()

    def test_probe_cp_mode_finds_target(self):
        with tempfile.TemporaryDirectory() as tmp:
            write_cp_config(tmp)
            fake = FakeCpClient([web_entry()])
            code = self.run_probe(tmp, fake, "--target", "829711680")
            self.assertEqual(code, 0)

    def test_probe_cp_mode_target_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            write_cp_config(tmp)
            fake = FakeCpClient([web_entry(orderId=123)])
            code = self.run_probe(tmp, fake, "--target", "829711680")
            self.assertEqual(code, 4)

    def test_probe_cp_mode_session_down_is_exit_3(self):
        with tempfile.TemporaryDirectory() as tmp:
            write_cp_config(tmp)
            fake = FakeCpClient([], authenticated=False)
            code = self.run_probe(tmp, fake)
            self.assertEqual(code, 3)


if __name__ == "__main__":
    unittest.main()
