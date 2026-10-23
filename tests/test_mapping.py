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


def pos_contract(**kw):
    base = {"conId": 0, "symbol": "", "localSymbol": "", "secType": "STK",
            "lastTradeDateOrContractMonth": "", "strike": 0.0, "right": "",
            "multiplier": "", "currency": "USD", "primaryExchange": ""}
    base.update(kw)
    return base


def pos_values(**kw):
    base = {"position": 0.0, "marketPrice": 0.0, "marketValue": 0.0,
            "averageCost": 0.0, "unrealizedPNL": 0.0, "realizedPNL": 0.0}
    base.update(kw)
    return base


class PositionMappingTests(unittest.TestCase):
    def test_stock_position(self):
        p = setu.broker_position(
            pos_contract(conId=265598, symbol="ACME", localSymbol="ACME",
                         secType="STK", primaryExchange="NASDAQ"),
            pos_values(position=150.0, marketPrice=201.25,
                       marketValue=30187.5, averageCost=180.40,
                       unrealizedPNL=3127.5, realizedPNL=0.0))
        self.assertEqual(p["con_id"], 265598)
        self.assertEqual(p["symbol"], "ACME")
        self.assertEqual(p["local_symbol"], "ACME")
        self.assertEqual(p["sec_type"], "STK")
        self.assertIsNone(p["expiry"])
        self.assertIsNone(p["strike"])
        self.assertIsNone(p["option_type"])
        self.assertEqual(p["multiplier"], 1)
        self.assertEqual(p["currency"], "USD")
        self.assertEqual(p["exchange"], "NASDAQ")
        self.assertEqual(p["quantity"], 150.0)
        self.assertEqual(p["avg_cost"], 180.40,
                         "avg_cost is the raw per-unit value, never scaled")
        self.assertEqual(p["market_price"], 201.25)
        self.assertEqual(p["market_value"], 30187.5)
        self.assertEqual(p["unrealized_pnl"], 3127.5)

    def test_long_call(self):
        p = setu.broker_position(
            pos_contract(conId=700001, symbol="ACME", secType="OPT",
                         lastTradeDateOrContractMonth="20270115",
                         strike=55.0, right="C", multiplier="100"),
            pos_values(position=2.0, marketPrice=3.10, marketValue=620.0,
                       averageCost=250.0, unrealizedPNL=120.0))
        self.assertEqual(p["sec_type"], "OPT")
        self.assertEqual(p["expiry"], "2027-01-15")
        self.assertEqual(p["strike"], 55.0)
        self.assertEqual(p["option_type"], "Call")
        self.assertEqual(p["multiplier"], 100)
        self.assertEqual(p["quantity"], 2.0)
        self.assertEqual(p["avg_cost"], 250.0)

    def test_short_put_negative_quantity(self):
        p = setu.broker_position(
            pos_contract(conId=700002, symbol="MU", secType="OPT",
                         lastTradeDateOrContractMonth="20261120",
                         strike=120.0, right="P", multiplier="100"),
            pos_values(position=-3.0, marketPrice=1.45,
                       marketValue=-435.0, averageCost=180.0,
                       unrealizedPNL=105.0, realizedPNL=0.0))
        self.assertEqual(p["option_type"], "Put")
        self.assertEqual(p["expiry"], "2026-11-20")
        self.assertEqual(p["quantity"], -3.0, "shorts stay negative")
        self.assertEqual(p["market_value"], -435.0)

    def test_expiry_conversion(self):
        p = setu.broker_position(
            pos_contract(conId=1, symbol="X", secType="OPT",
                         lastTradeDateOrContractMonth="20261218",
                         strike=10.0, right="C", multiplier="100"),
            pos_values(position=1.0))
        self.assertEqual(p["expiry"], "2026-12-18")
        p2 = setu.broker_position(
            pos_contract(conId=2, symbol="X", secType="STK"),
            pos_values(position=1.0))
        self.assertIsNone(p2["expiry"], "no date on the contract -> null")

    def test_blank_multiplier_defaults_to_1(self):
        for blank in ("", None, 0, "0"):
            p = setu.broker_position(
                pos_contract(conId=3, symbol="X", secType="STK", multiplier=blank),
                pos_values(position=1.0))
            self.assertEqual(p["multiplier"], 1, f"multiplier {blank!r} -> 1")
        p = setu.broker_position(
            pos_contract(conId=4, symbol="X", secType="OPT", multiplier="100",
                         right="P", strike=5.0,
                         lastTradeDateOrContractMonth="20270115"),
            pos_values(position=1.0))
        self.assertEqual(p["multiplier"], 100)

    def test_right_mapping(self):
        for right, want in (("P", "Put"), ("C", "Call"), ("", None)):
            p = setu.broker_position(
                pos_contract(conId=5, symbol="X", secType="OPT", right=right,
                             strike=5.0, multiplier="100",
                             lastTradeDateOrContractMonth="20270115"),
                pos_values(position=1.0))
            self.assertEqual(p["option_type"], want)

    def test_positions_snapshot_envelope(self):
        snap = setu.positions_snapshot("U19377323", None,
                                       "2026-10-23T09:00:00+01:00", [])
        self.assertEqual(snap["ingest_source"], "setu")
        self.assertEqual(snap["account_id"], "U19377323")
        self.assertTrue(snap["complete"])
        self.assertEqual(snap["positions"], [])


class StreamFoldTests(unittest.TestCase):
    def test_stream_fills_only_nulls(self):
        merged = setu.fold_stream_values(
            {"UnrealizedPnL": None, "RealizedPnL": 100.0, "SettledCash": None},
            {"UnrealizedPnL": -42.5, "RealizedPnL": 999.0, "SettledCash": 1234.0})
        self.assertEqual(merged["UnrealizedPnL"], -42.5)
        self.assertEqual(merged["RealizedPnL"], 100.0,
                         "a real sweep value always wins over the stream")
        self.assertEqual(merged["SettledCash"], 1234.0)

    def test_stream_missing_leaves_null(self):
        merged = setu.fold_stream_values({"UnrealizedPnL": None}, {})
        self.assertIsNone(merged["UnrealizedPnL"])

    def test_settled_cash_by_date_parsing(self):
        self.assertEqual(
            setu.settled_cash_from_by_date("20261023:-99048.28"), -99048.28)
        self.assertEqual(
            setu.settled_cash_from_by_date("20261022:100.0,20261023:-5.5"), -5.5,
            "latest date wins")
        self.assertIsNone(setu.settled_cash_from_by_date(""))
        self.assertIsNone(setu.settled_cash_from_by_date(None))
        self.assertIsNone(setu.settled_cash_from_by_date("garbage"))
        self.assertIsNone(setu.settled_cash_from_by_date("20261023:notanumber"))

    def test_stream_key_spellings(self):
        try:
            client = setu.GatewayClient(
                {"account_id": "U19377323", "ib_host": "127.0.0.1",
                 "ib_port": 4001, "client_id": 99})
        except Exception:
            self.skipTest("ibapi unavailable")
        w = client.wrapper
        w.updateAccountValue("$LEDGER-UnrealizedPnL", "-237812.38", "USD", "U19377323")
        w.updateAccountValue("$LEDGER-RealizedPnL", "0.00", "USD", "U19377323")
        w.updateAccountValue("SettledCashByDate", "20261023:-99048.28", "USD", "U19377323")
        w.updateAccountValue("NetLiquidation", "411092.99", "USD", "U19377323")
        w.updateAccountValue("$LEDGER-UnrealizedPnL", "1.0", "USD", "U99999999")
        self.assertEqual(client.stream_values["UnrealizedPnL"], -237812.38)
        self.assertEqual(client.stream_values["RealizedPnL"], 0.0)
        self.assertEqual(client.stream_values["SettledCash"], -99048.28)
        self.assertNotIn("NetLiquidation", client.stream_values,
                         "only the fill tags are captured from the stream")


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
