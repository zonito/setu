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
from datetime import date, datetime, timedelta
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
        # IBKR takes the reference in q on GetStatement too;
        # a ref= parameter is ignored and answered 1020
        # (proven live 2026-10-27 against flex_pull 0.2.0,
        # which this assertion originally encoded wrongly).
        self.assertTrue(all("q=REF123" in u for u in opener.urls[1:]))
        self.assertTrue(all("ref=" not in u for u in opener.urls[1:]))

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


FLEX_MINI = (
    "HEADER,CRTT,ClientAccountID,AccountAlias,Model,"
    "CurrencyPrimary,LevelOfDetail,FromDate,ToDate,StartingCash,"
    "EndingCash\n"
    "DATA,CRTT,acct,zonito,,BASE_SUMMARY,BaseCurrency,20261001,"
    "20261026,1000,900\n"
    "DATA,CRTT,acct,zonito,,USD,Currency,20261001,20261026,1000,"
    "900\n"
    "HEADER,TRNT,ClientAccountID,AccountAlias,Model,"
    "CurrencyPrimary,FXRateToBase,AssetClass,SubCategory,Symbol,"
    "Description,TradeDate,TransactionType,Quantity,TradePrice,"
    "Proceeds,IBCommission,NetCash,Buy/Sell,Notes/Codes,MtmPnl\n"
    "DATA,TRNT,acct,zonito,,USD,1,STK,COMMON,XYZ,XYZ CORP,"
    "20261020,ExchTrade,7,26.73,-187.11,-0.34257825,,BUY,,0\n"
    "HEADER,CTRN,ClientAccountID,AccountAlias,Model,"
    "CurrencyPrimary,FXRateToBase,Symbol,Description,Date/Time,"
    "Amount,Type\n"
)


class _FakeResponse:
    def __init__(self, body):
        self._body = body.encode()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self):
        return self._body


class _FakeOpener:
    """Scripts HTTP responses and records request URLs."""

    def __init__(self, bodies):
        self._bodies = list(bodies)
        self.urls = []

    def open(self, req, timeout=None):
        self.urls.append(req.full_url)
        return _FakeResponse(self._bodies.pop(0))


SEND_OK = (
    '<?xml version="1.0" encoding="UTF-8"?>'
    "<FlexStatementResponse><Status>Success</Status>"
    "<ReferenceCode>REF123</ReferenceCode></FlexStatementResponse>")
GENERATING = (
    '<?xml version="1.0" encoding="UTF-8"?>'
    "<FlexStatementResponse><Status>Fail</Status>"
    "<ErrorCode>1019</ErrorCode>"
    "<ErrorMessage>Statement generation in progress"
    "</ErrorMessage></FlexStatementResponse>")


class RealClientTests(unittest.TestCase):
    """The real FlexClient over a fake transport: the URL
    contract is IBKR's, and 0.2.0 shipped q->ref wrong on
    GetStatement (live error 1020) because only stub clients
    were tested."""

    def test_fetch_uses_q_for_reference_and_polls(self):
        opener = _FakeOpener([SEND_OK, GENERATING, SAMPLE_CSV])
        lines = []
        client = flex_pull.FlexClient("TOK999", opener=opener,
                                      sleeper=lambda s: None,
                                      log=lines.append)
        body = client.fetch("1687614", date(2026, 10, 1),
                            date(2026, 10, 26))
        self.assertEqual(body, SAMPLE_CSV)
        self.assertTrue(any("generating" in ln for ln in lines))
        self.assertIn("q=1687614", opener.urls[0])
        self.assertIn("fd=20261001", opener.urls[0])
        for url in opener.urls[1:]:
            self.assertIn("q=REF123", url)
            self.assertNotIn("ref=", url)


GEN_FAIL_1001 = (
    '<?xml version="1.0" encoding="UTF-8"?>'
    "<FlexStatementResponse><Status>Fail</Status>"
    "<ErrorCode>1001</ErrorCode><ErrorMessage>Statement could "
    "not be generated at this time. Please try again shortly."
    "</ErrorMessage></FlexStatementResponse>")


ACTIVITY_CSV = (
    "HEADER,CRTT,ClientAccountID,AccountAlias\n"
    "DATA,CRTT,acct,zonito\n"
    "HEADER,TRNT,ClientAccountID\n")


class RawFlexCsvDownloadTests(unittest.TestCase):
    """A convert-mode download is the Activity envelope, not
    the statement envelope: the first GetStatement poll can
    already carry it, and fetch must hand it back, not feed
    it to the XML parser (0.2.3 failed a good 06:45 run)."""

    def test_first_poll_activity_csv_is_returned(self):
        opener = _FakeOpener([SEND_OK, ACTIVITY_CSV])
        client = flex_pull.FlexClient("TOK999", opener=opener,
                                      sleeper=lambda s: None)
        body = client.fetch("1687614", date(2026, 10, 1),
                            date(2026, 10, 26))
        self.assertEqual(body, ACTIVITY_CSV)


class SendRetryTests(unittest.TestCase):
    def test_send_1001_is_retried(self):
        opener = _FakeOpener([GEN_FAIL_1001, SEND_OK, SAMPLE_CSV])
        sleeps = []
        client = flex_pull.FlexClient("TOK999", opener=opener,
                                      sleeper=sleeps.append)
        body = client.fetch("1687614", date(2026, 10, 1),
                            date(2026, 10, 26), interval_s=20)
        self.assertEqual(body, SAMPLE_CSV)
        # Send retries are minutes-scale (5 min first), never
        # the 20s statement-poll interval — 1025 taught that.
        self.assertEqual(sleeps, [300])

    def test_send_1001_eventually_gives_up(self):
        opener = _FakeOpener([GEN_FAIL_1001])
        client = flex_pull.FlexClient("TOK999", opener=opener,
                                      sleeper=lambda s: None)
        with self.assertRaises(flex_pull.FlexError):
            client.fetch("1687614", date(2026, 10, 1),
                         date(2026, 10, 26), max_wait_s=0)

    def test_send_other_errors_still_fail_fast(self):
        fail = ('<?xml version="1.0" encoding="UTF-8"?>'
                "<FlexStatementResponse><Status>Fail</Status>"
                "<ErrorCode>1012</ErrorCode><ErrorMessage>Invalid "
                "token</ErrorMessage></FlexStatementResponse>")
        opener = _FakeOpener([fail])
        client = flex_pull.FlexClient("TOK999", opener=opener,
                                      sleeper=lambda s: None)
        with self.assertRaises(flex_pull.FlexError):
            client.fetch("1687614", date(2026, 10, 1),
                         date(2026, 10, 26))


class UnparseableResponseTests(unittest.TestCase):
    """0.2.7: when IBKR's Flex service degrades it can answer
    with non-XML bodies. One junk poll response must not kill
    the run (live 2026-10-29 06:45: a clean 1019, then a
    non-XML poll body 20s later, run dead), the body is saved
    for diagnosis, and junk on SendRequest rides the same
    minutes-scale ladder as 1001."""

    JUNK = "Service temporarily unavailable, please retry"

    def _client(self, routes, saved, sleeps):
        opener = FakeOpener(routes)
        return flex_pull.FlexClient(
            "TOKEN123", opener=opener, sleeper=sleeps.append,
            on_unparseable=lambda stage, body: saved.append((stage, body)))

    def test_junk_poll_then_statement_succeeds(self):
        saved, sleeps = [], []
        client = self._client([
            ("SendRequest", [SUCCESS_XML]),
            ("GetStatement", [GENERATING_XML, self.JUNK, SAMPLE_CSV]),
        ], saved, sleeps)
        body = client.fetch("1687614", date(2026, 10, 1),
                            date(2026, 10, 26))
        self.assertEqual(body, SAMPLE_CSV)
        self.assertEqual(saved, [("GetStatement", self.JUNK)])
        self.assertEqual(sleeps, [20, 20])

    def test_junk_polls_until_budget_fail_with_excerpt(self):
        saved, sleeps = [], []
        client = self._client([
            ("SendRequest", [SUCCESS_XML]),
            ("GetStatement", [self.JUNK]),
        ], saved, sleeps)
        with self.assertRaises(flex_pull.FlexError) as ctx:
            client.fetch("1687614", date(2026, 10, 1),
                         date(2026, 10, 26),
                         max_wait_s=40, interval_s=20)
        self.assertIn("unparseable", str(ctx.exception))
        self.assertEqual(len(saved), 3)

    def test_junk_send_retries_on_the_1001_ladder(self):
        saved, sleeps = [], []
        client = self._client([
            ("SendRequest", [self.JUNK, SUCCESS_XML]),
            ("GetStatement", [SAMPLE_CSV]),
        ], saved, sleeps)
        body = client.fetch("1687614", date(2026, 10, 1),
                            date(2026, 10, 26))
        self.assertEqual(body, SAMPLE_CSV)
        self.assertEqual(sleeps, [300])
        self.assertEqual(saved, [("SendRequest", self.JUNK)])

    def test_junk_send_gives_up_after_the_ladder(self):
        saved, sleeps = [], []
        client = self._client([("SendRequest", [self.JUNK])],
                              saved, sleeps)
        with self.assertRaises(flex_pull.FlexError):
            client.fetch("1687614", date(2026, 10, 1),
                         date(2026, 10, 26))
        self.assertEqual(sleeps, [300, 600, 900])


class ConvertModeTests(unittest.TestCase):
    def make_job(self, tmp, convert=True):
        token_file = Path(tmp) / "token.txt"
        token_file.write_text("TOK999", encoding="utf-8")
        return {"name": "zonito", "account_id": "U19377323",
                "query_id": "1687614", "token_file": str(token_file),
                "output_dir": str(Path(tmp) / "drive"),
                "convert": convert}

    def test_convert_job_writes_statement_and_raw_backup(self):
        with tempfile.TemporaryDirectory() as tmp:
            job = self.make_job(tmp)
            factory = lambda token: StubClient(body=FLEX_MINI)
            result = flex_pull.run_job(
                job, date(2026, 10, 27), client_factory=factory,
                backup_root=Path(tmp) / "backups")
            self.assertTrue(result["converted"])
            written = Path(result["file"]).read_text(encoding="utf-8")
            self.assertTrue(
                written.startswith("Statement,Header"))
            self.assertIn("Transaction History,Data,2026-10-20",
                          written)
            raw = list((Path(tmp) / "backups").glob("*flex-raw*.csv"))
            self.assertEqual(len(raw), 1)
            self.assertEqual(raw[0].read_text(encoding="utf-8"),
                             FLEX_MINI)

    def test_convert_warning_fails_without_overwriting(self):
        with tempfile.TemporaryDirectory() as tmp:
            job = self.make_job(tmp)
            out_dir = Path(job["output_dir"])
            out_dir.mkdir(parents=True)
            target = out_dir / "Oct26.csv"
            target.write_text("OLD", encoding="utf-8")
            bad = FLEX_MINI.replace("ExchTrade", "MysteryTrade")
            factory = lambda token: StubClient(body=bad)
            with self.assertRaises(flex_pull.FlexError):
                flex_pull.run_job(job, date(2026, 10, 27),
                                  client_factory=factory,
                                  backup_root=Path(tmp) / "backups")
            self.assertEqual(target.read_text(encoding="utf-8"),
                             "OLD")


class _RaisingClient:
    def __init__(self, exc):
        self._exc = exc
        self.calls = 0

    def fetch(self, *a, **k):
        self.calls += 1
        raise self._exc


class PauseGuardTests(unittest.TestCase):
    """IBKR's 1025 block lasts ~an hour and covers the query
    with any token (and vice versa); attempts inside the
    window feed it. flex_pull records the block and refuses
    to call IBKR until it lapses."""

    def make_job(self, tmp):
        token_file = Path(tmp) / "token.txt"
        token_file.write_text("TOK999", encoding="utf-8")
        return {"name": "zonito", "account_id": "U19377323",
                "query_id": "1687614", "token_file": str(token_file),
                "output_dir": str(Path(tmp) / "drive")}

    def test_1025_records_pause_and_next_run_stays_quiet(self):
        with tempfile.TemporaryDirectory() as tmp:
            job = self.make_job(tmp)
            backups = Path(tmp) / "backups"
            raiser = _RaisingClient(flex_pull.FlexError(
                "SendRequest failed: 1025", code="1025"))
            with self.assertRaises(flex_pull.FlexError):
                flex_pull.run_job(job, date(2026, 10, 28),
                                  client_factory=lambda t: raiser,
                                  backup_root=backups)
            self.assertEqual(raiser.calls, 1)
            good = StubClient()
            with self.assertRaises(flex_pull.FlexError) as ctx:
                flex_pull.run_job(
                    job, date(2026, 10, 28),
                    client_factory=lambda t: good,
                    backup_root=backups)
            self.assertIn("pause in force", str(ctx.exception))
            self.assertEqual(good.calls, [])

    def test_expired_pause_lets_the_run_through(self):
        with tempfile.TemporaryDirectory() as tmp:
            job = self.make_job(tmp)
            backups = Path(tmp) / "backups"
            backups.mkdir(parents=True)
            past = (datetime.now().astimezone()
                    - timedelta(hours=2)).isoformat()
            (Path(tmp) / "flex-pause.json").write_text(
                json.dumps({"zonito": {"until": past,
                                       "code": "1025"}}),
                encoding="utf-8")
            result = flex_pull.run_job(
                job, date(2026, 10, 27),
                client_factory=lambda t: StubClient(body=SAMPLE_CSV),
                backup_root=backups)
            self.assertTrue(result["ok"])


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
