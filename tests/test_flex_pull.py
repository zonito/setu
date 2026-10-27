"""Tests for flex_pull — the morning Flex statement fetcher.

Covers the date math (month-to-date through yesterday, file
named for yesterday's month), the Flex Web Service conversation
(SendRequest -> GetStatement with the 1019 generation retry),
the statement validation gate (a bad body never overwrites the
existing file), and the job runner's atomic write + backup.

Synthetic data only; fake openers/clients throughout.

Run: python3 -m unittest discover tests
"""
import json
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import flex_pull  # noqa: E402

SAMPLE_CSV = (
    "Statement,Header,Field Name,Field Value\n"
    "Statement,Data,Title,Transaction History\n"
    'Statement,Data,Period,"October 1, 2026 - October 26, 2026"\n'
    "Statement,Data,WhenGenerated,\"2026-10-27, 02:47:08 EDT\"\n"
    "Summary,Header,Field Name,Field Value\n"
    "Summary,Data,Base Currency,USD\n"
    "Transaction History,Header,Date,Account,Description,"
    "Transaction Type,Symbol,Quantity,Price,Price Currency,"
    "Gross Amount ,Commission,Net Amount\n"
    "Transaction History,Data,2026-10-26,zonito,"
    "DELL 20NOV26 510 C,Sell,DELL  261120C00510000,-1.0,18.5,USD,"
    "1850.0,-0.65541,1849.34459\n"
)

SUCCESS_XML = (
    '<?xml version="1.0" encoding="UTF-8"?>'
    "<FlexStatementResponse>"
    "<Status>Success</Status>"
    "<ReferenceCode>REF123</ReferenceCode>"
    "</FlexStatementResponse>"
)

GENERATING_XML = (
    '<?xml version="1.0" encoding="UTF-8"?>'
    "<FlexStatementResponse>"
    "<Status>Fail</Status>"
    "<ErrorCode>1019</ErrorCode>"
    "<ErrorMessage>Statement generation in progress</ErrorMessage>"
    "</FlexStatementResponse>"
)


class FakeResponse:
    def __init__(self, body):
        self._body = body.encode("utf-8") if isinstance(body, str) else body

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeOpener:
    """Routes by URL substring; each route serves its bodies in
    order, repeating the last one."""

    def __init__(self, routes):
        self.routes = [(needle, list(bodies)) for needle, bodies in routes]
        self.urls = []

    def open(self, req, timeout=None):
        url = req.full_url
        self.urls.append(url)
        for needle, bodies in self.routes:
            if needle in url:
                body = bodies.pop(0) if len(bodies) > 1 else bodies[0]
                return FakeResponse(body)
        raise AssertionError(url)


def make_client(routes):
    sleeps = []
    opener = FakeOpener(routes)
    client = flex_pull.FlexClient("TOKEN123", opener=opener,
                                  sleeper=sleeps.append)
    return client, opener, sleeps


class MonthWindowTests(unittest.TestCase):
    def test_mid_month(self):
        start, end, stem = flex_pull.month_window(date(2026, 10, 27))
        self.assertEqual((start, end, stem),
                         (date(2026, 10, 1), date(2026, 10, 26), "Oct26"))

    def test_first_of_month_completes_previous_month(self):
        start, end, stem = flex_pull.month_window(date(2026, 11, 1))
        self.assertEqual((start, end, stem),
                         (date(2026, 10, 1), date(2026, 10, 31), "Oct26"))

    def test_new_year(self):
        start, end, stem = flex_pull.month_window(date(2027, 1, 1))
        self.assertEqual((start, end, stem),
                         (date(2026, 12, 1), date(2026, 12, 31), "Dec26"))


class FetchFlowTests(unittest.TestCase):
    def test_fetch_waits_out_generation_then_returns_csv(self):
        client, opener, sleeps = make_client([
            ("SendRequest", [SUCCESS_XML]),
            ("GetStatement", [GENERATING_XML, GENERATING_XML, SAMPLE_CSV]),
        ])
        body = client.fetch("987", date(2026, 10, 1), date(2026, 10, 26),
                            interval_s=20)
        self.assertEqual(body, SAMPLE_CSV)
        self.assertEqual(sleeps, [20, 20])
        send = opener.urls[0]
        self.assertIn("q=987", send)
        self.assertIn("fd=20261001", send)
        self.assertIn("td=20261026", send)
        self.assertTrue(all("ref=REF123" in u for u in opener.urls[1:]))

    def test_send_request_failure_surfaces(self):
        fail = ("<FlexStatementResponse><Status>Fail</Status>"
                "<ErrorCode>1015</ErrorCode>"
                "<ErrorMessage>Token is invalid</ErrorMessage>"
                "</FlexStatementResponse>")
        client, _, _ = make_client([("SendRequest", [fail])])
        with self.assertRaises(flex_pull.FlexError) as ctx:
            client.fetch("987", date(2026, 10, 1), date(2026, 10, 26))
        self.assertIn("1015", str(ctx.exception))

    def test_get_statement_hard_error_is_not_retried(self):
        fail = ("<FlexStatementResponse><Status>Fail</Status>"
                "<ErrorCode>1010</ErrorCode>"
                "<ErrorMessage>Bad reference</ErrorMessage>"
                "</FlexStatementResponse>")
        client, opener, _ = make_client([
            ("SendRequest", [SUCCESS_XML]),
            ("GetStatement", [fail]),
        ])
        with self.assertRaises(flex_pull.FlexError):
            client.fetch("987", date(2026, 10, 1), date(2026, 10, 26))
        self.assertEqual(len(opener.urls), 2)

    def test_generation_timeout_raises(self):
        client, _, sleeps = make_client([
            ("SendRequest", [SUCCESS_XML]),
            ("GetStatement", [GENERATING_XML]),
        ])
        with self.assertRaises(flex_pull.FlexError) as ctx:
            client.fetch("987", date(2026, 10, 1), date(2026, 10, 26),
                         max_wait_s=40, interval_s=20)
        self.assertIn("still generating", str(ctx.exception))
        self.assertEqual(sleeps, [20, 20])


class ValidationTests(unittest.TestCase):
    def test_good_statement(self):
        info = flex_pull.validate_statement(SAMPLE_CSV)
        self.assertEqual(info["title"], "Transaction History")
        self.assertEqual(info["rows"], 1)
        self.assertIn("October", info["period"])

    def test_wrong_title_refused(self):
        # The Title line is the first occurrence in the sample.
        bad = SAMPLE_CSV.replace("Transaction History", "Trades Only", 1)
        with self.assertRaises(flex_pull.FlexError):
            flex_pull.validate_statement(bad)

    def test_zero_data_rows_refused(self):
        lines = [ln for ln in SAMPLE_CSV.splitlines()
                 if not ln.startswith("Transaction History,Data")]
        with self.assertRaises(flex_pull.FlexError):
            flex_pull.validate_statement("\n".join(lines))

    def test_garbage_refused(self):
        with self.assertRaises(flex_pull.FlexError):
            flex_pull.validate_statement("<html>error</html>")


class StubClient:
    def __init__(self, body=None, error=None):
        self.body = body
        self.error = error
        self.calls = []

    def fetch(self, query_id, from_date, to_date):
        self.calls.append((query_id, from_date, to_date))
        if self.error:
            raise self.error
        return self.body


class RunJobTests(unittest.TestCase):
    def make_job(self, tmp):
        token_file = Path(tmp) / "token.txt"
        token_file.write_text("  TOK999\n", encoding="utf-8")
        return {"name": "zonito", "account_id": "U19377323",
                "query_id": "987", "token_file": str(token_file),
                "output_dir": str(Path(tmp) / "drive")}

    def test_run_writes_validated_file_and_backup(self):
        with tempfile.TemporaryDirectory() as tmp:
            job = self.make_job(tmp)
            out_dir = Path(job["output_dir"])
            out_dir.mkdir(parents=True)
            (out_dir / "Oct26.csv").write_text("OLD", encoding="utf-8")
            seen = {}

            def factory(token):
                seen["token"] = token
                return StubClient(body=SAMPLE_CSV)

            result = flex_pull.run_job(
                job, date(2026, 10, 27), client_factory=factory,
                backup_root=Path(tmp) / "backups")
            self.assertTrue(result["ok"])
            self.assertEqual(seen["token"], "TOK999")
            self.assertEqual(
                (out_dir / "Oct26.csv").read_text(encoding="utf-8"),
                SAMPLE_CSV)
            backups = list((Path(tmp) / "backups").glob("*.csv"))
            self.assertEqual(len(backups), 1)
            self.assertEqual(backups[0].read_text(encoding="utf-8"), "OLD")
            self.assertFalse((out_dir / ".Oct26.csv.tmp").exists())

    def test_invalid_statement_never_overwrites(self):
        with tempfile.TemporaryDirectory() as tmp:
            job = self.make_job(tmp)
            out_dir = Path(job["output_dir"])
            out_dir.mkdir(parents=True)
            target = out_dir / "Oct26.csv"
            target.write_text("OLD", encoding="utf-8")
            factory = lambda token: StubClient(body="not a statement")
            with self.assertRaises(flex_pull.FlexError):
                flex_pull.run_job(job, date(2026, 10, 27),
                                  client_factory=factory,
                                  backup_root=Path(tmp) / "backups")
            self.assertEqual(target.read_text(encoding="utf-8"), "OLD")

    def test_missing_token_file_fails_the_job(self):
        with tempfile.TemporaryDirectory() as tmp:
            job = self.make_job(tmp)
            Path(job["token_file"]).unlink()
            with self.assertRaises(flex_pull.FlexError):
                flex_pull.run_job(job, date(2026, 10, 27),
                                  client_factory=lambda t: StubClient())


class ConfigTests(unittest.TestCase):
    def test_load_config_round_trip(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "flex-pull.json"
            path.write_text(json.dumps({"jobs": [
                {"name": "zonito", "account_id": "U1", "query_id": "9",
                 "token_file": "t", "output_dir": "d"}]}),
                encoding="utf-8")
            jobs = flex_pull.load_config(path)
            self.assertEqual(len(jobs), 1)

    def test_load_config_missing_key(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "flex-pull.json"
            path.write_text(json.dumps({"jobs": [{"name": "x"}]}),
                            encoding="utf-8")
            with self.assertRaises(flex_pull.FlexError):
                flex_pull.load_config(path)

    def test_main_no_jobs_is_exit_2(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "flex-pull.json"
            path.write_text(json.dumps({"jobs": []}), encoding="utf-8")
            code = flex_pull.main(
                ["run", "--config", str(path), "--date", "2026-10-27"])
            self.assertEqual(code, 2)


if __name__ == "__main__":
    unittest.main()
