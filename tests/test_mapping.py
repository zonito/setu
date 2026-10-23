"""Unit tests for Setu's payload mapping — synthetic data only.

Run: python3 -m pytest tests/  (or python3 -m unittest discover tests)
"""
import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import setu  # noqa: E402


class FakeContract:
    def __init__(self, **kw):
        self.symbol = kw.get("symbol", "")
        self.secType = kw.get("secType", "STK")
        self.lastTradeDateOrContractMonth = kw.get("expiry", "")
        self.strike = kw.get("strike", 0.0)
        self.right = kw.get("right", "")
        self.currency = kw.get("currency", "USD")


class FakeOrder:
    def __init__(self, **kw):
        self.action = kw.get("action", "BUY")
        self.totalQuantity = kw.get("total", 0.0)
        self.lmtPrice = kw.get("lmt", 1.7976931348623157e308)
        self.tif = kw.get("tif", "GTC")
        self.permId = kw.get("permId", 0)
        self.account = kw.get("account", "U19377323")


class FakeState:
    def __init__(self, **kw):
        self.status = kw.get("status", "Submitted")
        self.filled = kw.get("filled", None)
        self.remaining = kw.get("remaining", None)


class AccountSnapshotTests(unittest.TestCase):
    def test_full_mapping(self):
        snap = setu.account_snapshot(
            "U19377323", "zonito", "USD",
            {"NetLiquidation": 374248.03, "ExcessLiquidity": 4266.93,
             "BuyingPower": 0.0, "MaintMarginReq": 547040.82,
             "UnrealizedPnL": -1200.5, "RealizedPnL": 6515.23,
             "SettledCash": None},
            "2026-10-23T08:15:22+01:00")
        self.assertEqual(snap["ingest_source"], "setu")
        self.assertTrue(snap["complete"])
        self.assertEqual(snap["values"]["net_liquidation"], 374248.03)
        self.assertEqual(snap["values"]["excess_liquidity"], 4266.93)
        self.assertEqual(snap["values"]["maintenance_margin"], 547040.82)
        self.assertIsNone(snap["values"]["settled_cash"])

    def test_partial_when_nlv_missing(self):
        snap = setu.account_snapshot("U19377323", None, "USD",
                                     {"ExcessLiquidity": 10.0},
                                     "2026-10-23T08:15:22+01:00")
        self.assertFalse(snap["complete"])
        self.assertIsNone(snap["values"]["net_liquidation"])
        self.assertIsNone(snap["values"]["buying_power"])


class ExpiryTests(unittest.TestCase):
    def test_formats(self):
        self.assertEqual(setu.expiry_iso("20261120"), "2026-11-20")
        self.assertEqual(setu.expiry_iso("202611"), "2026-11-01")
        self.assertIsNone(setu.expiry_iso(""))
        self.assertIsNone(setu.expiry_iso(None))


class BrokerOrderTests(unittest.TestCase):
    def test_short_put_buy_to_close_shape(self):
        o = setu.broker_order(
            987654,
            FakeContract(symbol="MU", secType="OPT", expiry="20261120",
                         strike=120.0, right="P"),
            FakeOrder(action="BUY", total=2.0, lmt=1.50, tif="GTC"),
            FakeState(status="Submitted", filled=0.0, remaining=2.0))
        self.assertEqual(o["order_id"], "987654")
        self.assertEqual(o["asset_type"], "option")
        self.assertEqual(o["expiry_date"], "2026-11-20")
        self.assertEqual(o["strike_price"], 120.0)
        self.assertEqual(o["option_type"], "Put")
        self.assertEqual(o["action"], "Buy")
        self.assertEqual(o["qty_total"], 2.0)
        self.assertEqual(o["qty_filled"], 0.0)
        self.assertEqual(o["qty_remain"], 2.0)
        self.assertEqual(o["limit_price"], 1.50)
        self.assertEqual(o["tif"], "GTC")
        self.assertEqual(o["status"], "Submitted")
        self.assertEqual(o["legs"], [])

    def test_equity_order_nulls_and_unset_limit(self):
        o = setu.broker_order(
            42, FakeContract(symbol="ACME", secType="STK"),
            FakeOrder(action="SELL", total=100.0),  # lmt = unset sentinel
            FakeState(status="PreSubmitted", remaining=100.0))
        self.assertEqual(o["asset_type"], "equity")
        self.assertIsNone(o["expiry_date"])
        self.assertIsNone(o["strike_price"])
        self.assertIsNone(o["option_type"])
        self.assertIsNone(o["limit_price"], "ibapi unset sentinel must map to null")
        self.assertEqual(o["action"], "Sell")
        self.assertEqual(o["qty_filled"], 0.0, "filled derived from total - remaining")

    def test_call_right(self):
        o = setu.broker_order(
            7, FakeContract(symbol="ACME", secType="OPT", expiry="20270115",
                            strike=55.0, right="C"),
            FakeOrder(action="SELL", total=1.0, lmt=2.25),
            FakeState(status="Submitted", filled=0.0, remaining=1.0))
        self.assertEqual(o["option_type"], "Call")
        self.assertEqual(o["action"], "Sell")


class OrdersSnapshotTests(unittest.TestCase):
    def test_envelope(self):
        snap = setu.orders_snapshot("U19377323", None,
                                    "2026-10-23T08:15:22+01:00", [])
        self.assertEqual(snap["ingest_source"], "setu")
        self.assertTrue(snap["complete"])
        self.assertEqual(snap["orders"], [])


class ReadOnlyTests(unittest.TestCase):
    def test_no_order_placement_calls(self):
        src = Path(setu.__file__).read_text(encoding="utf-8")
        self.assertIsNone(re.search(r"\.placeOrder\s*\(", src))
        self.assertIsNone(re.search(r"\.cancelOrder\s*\(", src))
        self.assertIsNone(re.search(r"\.exerciseOptions\s*\(", src))


if __name__ == "__main__":
    unittest.main()


class OrderStatusMergeTests(unittest.TestCase):
    def test_quantities_come_from_order_status(self):
        try:
            client = setu.GatewayClient(
                {"account_id": "U19377323", "ib_host": "127.0.0.1",
                 "ib_port": 4001, "client_id": 99})
        except Exception:
            self.skipTest("ibapi unavailable")
        mapped = {"order_id": "555", "status": "PreSubmitted",
                  "qty_total": 3.0, "qty_filled": 0.0, "qty_remain": 0.0}
        client.orders = [(("p", 555), mapped)]
        client.status_by_order = {("p", 555): ("PreSubmitted", 0.0, 3.0)}
        out = client.apply_statuses()
        self.assertEqual(out[0]["qty_remain"], 3.0)
        self.assertEqual(out[0]["qty_filled"], 0.0)

    def test_missing_status_keeps_sweep_values(self):
        try:
            client = setu.GatewayClient(
                {"account_id": "U19377323", "ib_host": "127.0.0.1",
                 "ib_port": 4001, "client_id": 99})
        except Exception:
            self.skipTest("ibapi unavailable")
        mapped = {"order_id": "556", "status": "Submitted",
                  "qty_total": 1.0, "qty_filled": 0.0, "qty_remain": 1.0}
        client.orders = [(("p", 556), mapped)]
        client.status_by_order = {}
        out = client.apply_statuses()
        self.assertEqual(out[0]["qty_remain"], 1.0)
