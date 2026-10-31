"""Tests for flex_convert — Activity Flex CSV -> Transaction
History statement.

Fixtures are miniature, synthetic versions of the real shapes
(field layout copied from an actual SetuTxHistory run; values
invented). The full-size proof against a real statement pair
lives outside the suite (it uses live account data).

Run: python3 -m unittest discover tests
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import flex_convert  # noqa: E402

CRTT_FIELDS = ("ClientAccountID,AccountAlias,Model,CurrencyPrimary,"
               "LevelOfDetail,FromDate,ToDate,StartingCash,EndingCash")
TRNT_FIELDS = ("ClientAccountID,AccountAlias,Model,CurrencyPrimary,"
               "FXRateToBase,AssetClass,SubCategory,Symbol,Description,"
               "TradeDate,TransactionType,Quantity,TradePrice,Proceeds,"
               "IBCommission,NetCash,Buy/Sell,Notes/Codes,MtmPnl")
CTRN_FIELDS = ("ClientAccountID,AccountAlias,Model,CurrencyPrimary,"
               "FXRateToBase,Symbol,Description,Date/Time,Amount,Type")


def flex_csv(crtt_rows=(), trnt_rows=(), ctrn_rows=()):
    lines = [f"HEADER,CRTT,{CRTT_FIELDS}"]
    lines += [f"DATA,CRTT,{r}" for r in crtt_rows]
    lines.append(f"HEADER,TRNT,{TRNT_FIELDS}")
    lines += [f"DATA,TRNT,{r}" for r in trnt_rows]
    lines.append(f"HEADER,CTRN,{CTRN_FIELDS}")
    lines += [f"DATA,CTRN,{r}" for r in ctrn_rows]
    return "\n".join(lines) + "\n"


BASE_CRTT = [
    "acct,acct,,BASE_SUMMARY,BaseCurrency,20261001,20261026,1000,900",
    "acct,acct,,USD,Currency,20261001,20261026,1000,900",
]


def convert(**kw):
    return flex_convert.convert(flex_csv(**kw), "20261001", "20261026")


def th_rows(text):
    import csv, io
    return [r for r in csv.reader(io.StringIO(text))
            if r and r[0] == "Transaction History" and r[1] == "Data"]


class EnvelopeTests(unittest.TestCase):
    def test_envelope_and_summary(self):
        text, warnings = convert(crtt_rows=BASE_CRTT)
        self.assertEqual(warnings, [])
        lines = text.splitlines()
        self.assertEqual(lines[0],
                         "Statement,Header,Field Name,Field Value")
        self.assertIn("Statement,Data,Title,Transaction History", lines)
        self.assertIn(
            'Statement,Data,Period,"October 1, 2026 - October 26, 2026"',
            lines)
        self.assertIn("Summary,Data,Base Currency,USD", lines)
        self.assertIn("Summary,Data,Starting Cash,1000", lines)
        self.assertIn("Summary,Data,Change,-100.0", lines)
        self.assertIn("Summary,Data,Ending Cash,900", lines)
        header = [ln for ln in lines
                  if ln.startswith("Transaction History,Header")][0]
        self.assertIn("Gross Amount ,", header)  # trailing space kept

    def test_missing_summary_fails(self):
        with self.assertRaises(ValueError):
            convert(crtt_rows=[])


class QuotedInputTests(unittest.TestCase):
    """Live Flex bodies are fully quoted ("HEADER","CRTT",...);
    the fixtures are not. Conversion reads through csv.reader,
    so both shapes must produce the identical statement — this
    pins that, because the 0.2.8 poll-classifier fix is only
    safe if quoted input converts byte-identically."""

    def test_quoted_input_converts_identically(self):
        import csv as _csv
        import io
        plain = flex_csv(crtt_rows=BASE_CRTT, trnt_rows=[
            "acct,acct,,USD,1,STK,COMMON,XYZ,XYZ CORP,20261020,"
            "ExchTrade,7,26.73,-187.11,-0.34257825,,BUY,,0"])
        out = io.StringIO()
        writer = _csv.writer(out, quoting=_csv.QUOTE_ALL,
                             lineterminator="\n")
        writer.writerows(_csv.reader(io.StringIO(plain)))
        quoted = out.getvalue()
        self.assertTrue(quoted.startswith('"HEADER","CRTT",'))
        plain_text, plain_warnings = flex_convert.convert(
            plain, "20261001", "20261026")
        quoted_text, quoted_warnings = flex_convert.convert(
            quoted, "20261001", "20261026")
        self.assertEqual(quoted_warnings, plain_warnings)
        self.assertEqual(quoted_text, plain_text)


class TradeTests(unittest.TestCase):
    def test_option_trade_and_float_artefact_net(self):
        # Net must be the float sum, artefact included, exactly
        # like the statement's own rendering.
        trnt = ["acct,acct,,USD,1,OPT,OPTION,DELL  261120C00510000,"
                "DELL 20NOV26 510 C,20261026,ExchTrade,-1,18.5,1850,"
                "-0.65541,1849.34459,SELL,,0"]
        rows = th_rows(convert(crtt_rows=BASE_CRTT,
                               trnt_rows=trnt)[0])
        self.assertEqual(rows[0][4], "DELL 20NOV26 510 C")
        self.assertEqual(rows[0][5], "Sell")
        self.assertEqual(rows[0][10], "1850.0")
        self.assertEqual(rows[0][12], "1849.34459")

    def test_stock_trade_commission_zero_is_dash(self):
        trnt = ["acct,acct,,USD,1,STK,COMMON,XYZ,XYZ CORP,20261026,"
                "ExchTrade,7,26.73,-187.11,-0.34257825,,BUY,,0"]
        rows = th_rows(convert(crtt_rows=BASE_CRTT,
                               trnt_rows=trnt)[0])
        self.assertEqual(rows[0][12], "-187.45257825000002")

    def test_assignment_pair_becomes_one_stock_row(self):
        trnt = [
            "acct,acct,,USD,1,OPT,OPTION,IREN  261030P00040000,"
            "IREN 30OCT26 40 P,20261026,BookTrade,10,0,0,0,0,BUY,A,0",
            "acct,acct,,USD,1,STK,COMMON,IREN,IREN LTD,20261026,"
            "BookTrade,1000,40,-40000,0,-40000,BUY,A,0",
        ]
        rows = th_rows(convert(crtt_rows=BASE_CRTT,
                               trnt_rows=trnt)[0])
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][4],
                         "Buy 1,000 IREN LTD (Assignment)")
        self.assertEqual(rows[0][5], "Assignment")
        self.assertEqual(rows[0][11], "-")

    def test_forex_component_uses_mtms(self):
        trnt = ["acct,acct,,USD,1,CASH,,GBP.USD,GBP.USD,20261008,"
                "ExchTrade,1.29,1.32135,-1.7045415,0,0,BUY,,0.002"]
        rows = th_rows(convert(crtt_rows=BASE_CRTT,
                               trnt_rows=trnt)[0])
        self.assertEqual(rows[0][5], "Forex Trade Component")
        self.assertEqual(rows[0][10], "0.002")

    def test_unmapped_trade_warns(self):
        trnt = ["acct,acct,,USD,1,STK,COMMON,XYZ,XYZ CORP,20261026,"
                "MysteryTrade,1,1,-1,0,-1,BUY,,0"]
        _text, warnings = convert(crtt_rows=BASE_CRTT, trnt_rows=trnt)
        self.assertEqual(len(warnings), 1)


class CashTests(unittest.TestCase):
    def test_dividend_family_casing_and_rate(self):
        ctrn = [
            "acct,acct,,USD,1,SKHY,SKHY(US78392B2060) CASH DIVIDEND "
            "USD 0.027566 PER SHARE (Ordinary Dividend),20261007,0.2,"
            "Dividends",
            "acct,acct,,USD,1,SKHY,SKHY(US78392B2060) CASH DIVIDEND "
            "USD 0.027566 PER SHARE - KR TAX,20261007,-0.04,"
            "Withholding Tax",
        ]
        rows = th_rows(convert(crtt_rows=BASE_CRTT,
                               ctrn_rows=ctrn)[0])
        by_type = {r[5]: r for r in rows}
        self.assertEqual(
            by_type["Dividend"][4],
            "SKHY(US78392B2060) Cash Dividend USD 0.027566 per Share "
            "(Ordinary Dividend)")
        self.assertEqual(by_type["Foreign Tax Withholding"][4],
                         "SKHY(US78392B2060) Cash Dividend USD "
                         "0.027566 per Share - KR Tax")

    def test_gbp_interest_converts_at_row_fx(self):
        ctrn = ["acct,acct,,GBP,1.3223,,GBP DEBIT INT FOR SEP-2026,"
                "20261005,-1.29,Broker Interest Paid"]
        rows = th_rows(convert(crtt_rows=BASE_CRTT,
                               ctrn_rows=ctrn)[0])
        self.assertEqual(rows[0][4], "GBP Debit Interest for Sep-2026")
        self.assertEqual(rows[0][10], "-1.705767")

    def test_opra_fee_and_vat_pair(self):
        ctrn = [
            "acct,acct,,USD,1,,Z******87:OPRA NP L1 FOR OCT 2026,"
            "20261002,-1.5,Other Fees",
            "acct,acct,,USD,1,,CANCEL[Z******87:OPRA NP L1] FOR SEP "
            "2026,20261002,1.5,Other Fees",
        ]
        rows = th_rows(convert(crtt_rows=BASE_CRTT,
                               ctrn_rows=ctrn)[0])
        descs = {(r[5], r[4]): r[10] for r in rows}
        self.assertEqual(
            descs[("Other Fee", "Z******87:OPRA NP L1 for Oct 2026")],
            "-1.5")
        self.assertEqual(
            descs[("Other Fee",
                   "Cancel[Z******87:OPRA NP L1] FOR SEP 2026")], "1.5")
        self.assertEqual(
            descs[("Sales Tax", "VAT z******87:OPRA NP L1")], "-0.3")
        self.assertEqual(
            descs[("Sales Tax",
                   "VAT Cancel[z******87:OPRA NP L1]")], "0.3")

    def test_opra_vat_lowercases_alpha_mask(self):
        # Nikhila's mask ends in letters; the statement's VAT
        # rendering lowercases the whole mask token.
        ctrn = [
            "acct,acct,,USD,1,,N******KR:OPRA NP L1 FOR OCT 2026,"
            "20261002,-1.5,Other Fees",
        ]
        rows = th_rows(convert(crtt_rows=BASE_CRTT,
                               ctrn_rows=ctrn)[0])
        descs = {(r[5], r[4]): r[10] for r in rows}
        self.assertEqual(
            descs[("Sales Tax", "VAT n******kr:OPRA NP L1")], "-0.3")

    def test_exposure_fee_drops_period_suffix(self):
        ctrn = ['acct,acct,,USD,1,,"EXPOSURE FEE, EQUITY FOR '
                '20261024 FOR OCT 2026",20261026,-22.35,Other Fees']
        rows = th_rows(convert(crtt_rows=BASE_CRTT,
                               ctrn_rows=ctrn)[0])
        self.assertEqual(rows[0][4],
                         "Exposure Fee, Equity for 20261024")

    def test_withdrawal_and_deposit(self):
        ctrn = [
            "acct,acct,,USD,1,,DISBURSEMENT INITIATED BY Love Sharma,"
            "20261002,-101,Deposits/Withdrawals",
            "acct,acct,,USD,1,,WIRE RECEIVED,20261003,500,"
            "Deposits/Withdrawals",
        ]
        rows = th_rows(convert(crtt_rows=BASE_CRTT,
                               ctrn_rows=ctrn)[0])
        by_type = {r[5]: r for r in rows}
        self.assertEqual(by_type["Withdrawal"][4],
                         "Disbursement Initiated by Love Sharma")
        self.assertEqual(by_type["Deposit"][10], "500.0")


if __name__ == "__main__":
    unittest.main()
