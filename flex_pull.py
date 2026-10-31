#!/usr/bin/env python3
"""flex_pull — the morning Flex statement fetcher.

Downloads each account's "Transaction History" Flex statement
(month-to-date through yesterday) from IBKR's Flex Web Service
and writes it, in place, as <MonYY>.csv in the synced Google
Drive folder the rest of the stack already reads
(Transactions/IBKR/<person>/<MonYY>.csv). It exists to retire
the manual morning download-and-upload.

The Flex Web Service is token-based: no login session, no
competition with any Gateway. A job needs a saved Activity Flex
Query (the Transaction History statement) and a Flex token for
the login that owns it. Tokens live in their own files under
~/.setu (mode 600), are read at run time, and are never logged,
never written anywhere else.

Config: ~/.setu/flex-pull.json

    {"jobs": [
      {"name": "zonito", "account_id": "U19377323",
       "query_id": "1234567",
       "token_file": "~/.setu/flex-token-U19377323.txt",
       "output_dir": "<local synced Drive folder>/Transactions/IBKR/Zonito"},
      ... one job per account ...
    ]}

A job may set "convert": true — its query is then the
SetuTxHistory Activity Flex Query and flex_pull rebuilds the
Transaction History statement from it via flex_convert before
validating and writing (the raw Flex CSV is kept in
~/.setu/flex-backups). Conversion warnings fail the job.

Usage:
  flex_pull.py run [--config PATH] [--date YYYY-MM-DD]

Exit code 0 only when every job succeeds. A statement that
fails validation is never written over the existing file.
Progress and failures go to stdout (the LaunchAgent sends it
to ~/.setu/logs/flex-pull.log) and a machine-readable summary
lands in ~/.setu/flex-last-run.json.

Stdlib only, like the rest of Setu.
"""
import argparse
import csv
import io
import json
import os
import sys
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta
from pathlib import Path

FLEX_PULL_VERSION = "0.2.8"
FLEX_BASE = ("https://ndcdyn.interactivebrokers.com"
             "/Universal/servlet/FlexStatementService")
# IBKR error 1019: statement generation in progress — retryable.
RETRYABLE_ERROR_CODES = {"1019"}

# SendRequest retry schedule for IBKR error 1001 (seconds):
# minutes-scale on purpose — see the note in FlexClient.fetch.
SEND_RETRY_DELAYS_S = (300, 600, 900)
DEFAULT_MAX_WAIT_S = 1800
DEFAULT_INTERVAL_S = 20
_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun",
           "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


class FlexError(Exception):
    """A Flex failure worth surfacing verbatim in the log.

    Carries the IBKR error code when the failure came from an
    error envelope, so retry policy can read it."""

    def __init__(self, message, code=None):
        super().__init__(message)
        self.code = code


def log_line(message: str) -> None:
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{stamp}] {message}", flush=True)


def month_window(asof: date):
    """(from_date, to_date, file_stem) for the morning file:
    month-to-date through yesterday, named for yesterday's
    month (Oct26). Run on 1 Nov: yesterday is 31 Oct, so the
    October file is completed, not a November stub."""
    yesterday = asof - timedelta(days=1)
    start = yesterday.replace(day=1)
    stem = f"{_MONTHS[yesterday.month - 1]}{yesterday.year % 100:02d}"
    return start, yesterday, stem


def _find_text(root, name):
    for el in root.iter():
        if el.tag.rsplit("}", 1)[-1] == name:
            return (el.text or "").strip()
    return ""


def parse_flex_xml(text: str) -> dict:
    """Parse a FlexStatementResponse envelope. Raises FlexError
    when the body is not the expected XML at all."""
    try:
        # Bytes, not str: the real envelope carries an XML
        # encoding declaration, which ElementTree rejects on
        # already-decoded text.
        root = ET.fromstring(text.encode("utf-8"))
    except (ET.ParseError, ValueError) as exc:
        raise FlexError(f"unparseable Flex response: {exc}") from exc
    return {
        "status": _find_text(root, "Status"),
        "reference": _find_text(root, "ReferenceCode"),
        "error_code": _find_text(root, "ErrorCode"),
        "error_message": _find_text(root, "ErrorMessage"),
    }


def looks_like_statement(text: str) -> bool:
    stripped = text.lstrip("\ufeff \t\r\n")
    return (stripped.startswith("Statement,")
            or stripped.startswith('"Statement",')
            or "Transaction History,Header" in text
            or '"Transaction History","Header"' in text)


def looks_like_flex_csv(text: str) -> bool:
    """A raw Activity Flex CSV body: the HEADER/DATA + section
    code envelope (HEADER,CRTT,...). Convert-mode jobs download
    this shape, not the statement envelope — 0.2.3 mistook a
    successful first-poll download for an XML error envelope
    and failed the run on its parse error (live 2026-10-28
    06:45; the error text is byte-identical to feeding the CSV
    to the XML parser).

    Live bodies are FULLY QUOTED ("HEADER","CRTT",...) — the
    fixtures were not, and up to 0.2.7 this check only matched
    the unquoted prefix. Four good morning statements (28-31
    Oct 2026) fell through to the XML parser, were logged as
    "unparseable" and burned the full 1800s poll budget before
    the run failed. The first line is therefore parsed as CSV:
    its first field must be exactly HEADER, quoted or not."""
    stripped = text.lstrip("\ufeff \t\r\n")
    if stripped.startswith("HEADER,") or stripped.startswith('"HEADER",'):
        return True
    first = stripped.split("\n", 1)[0] if stripped else ""
    try:
        row = next(csv.reader(io.StringIO(first)))
    except (csv.Error, StopIteration):
        return False
    return bool(row) and row[0] == "HEADER"


def validate_statement(csv_text: str) -> dict:
    """The write gate: the downloaded body must be the
    Transaction History statement with real data rows, or the
    existing file stays untouched."""
    try:
        rows = list(csv.reader(io.StringIO(csv_text)))
    except csv.Error as exc:
        raise FlexError(f"statement is not valid CSV: {exc}") from exc
    title = ""
    period = ""
    data_rows = 0
    for row in rows:
        if len(row) >= 4 and row[0] == "Statement" and row[1] == "Data":
            if row[2] == "Title":
                title = row[3]
            elif row[2] == "Period":
                period = row[3]
        elif (len(row) >= 2 and row[0] == "Transaction History"
              and row[1] == "Data"):
            data_rows += 1
    if title != "Transaction History":
        raise FlexError(
            f"statement title is {title!r}, not 'Transaction History'")
    if data_rows == 0:
        raise FlexError("statement carries no Transaction History rows")
    return {"title": title, "period": period, "rows": data_rows}


def _no_proxy_opener():
    # The v0.2.2 lesson, applied to a new program: ambient proxy
    # env vars must never reroute (or break) a direct IBKR call.
    return urllib.request.build_opener(urllib.request.ProxyHandler({}))


class FlexClient:
    """One login's Flex Web Service. Transport and sleeper are
    injectable for tests."""

    def __init__(self, token: str, opener=None, sleeper=None,
                 log=None, on_unparseable=None):
        self._token = token
        self._opener = opener or _no_proxy_opener()
        self._sleep = sleeper if sleeper is not None else time.sleep
        # Progress lines during long waits (0.2.5): a run that
        # sits in a 5-minute send retry or a generation poll
        # used to look hung; the waits are legitimate IBKR
        # behaviour and now say so in the log.
        self._log = log or (lambda message: None)
        # Diagnostic sink for response bodies that are neither
        # the statement nor a parseable error envelope (0.2.7):
        # run_job saves them under the backups dir so the next
        # degraded-IBKR morning is diagnosable from the artefact,
        # not from an XML parser's guess. Never fatal.
        self._on_unparseable = on_unparseable

    def _note_unparseable(self, stage: str, body: str) -> str:
        """Hand an unclassifiable response body to the diagnostic
        sink and return a one-line excerpt for logs/errors."""
        if self._on_unparseable is not None:
            try:
                self._on_unparseable(stage, body)
            except Exception:
                pass  # diagnostics must never kill the fetch
        return " ".join((body or "").split())[:120]

    def _get(self, url: str) -> str:
        req = urllib.request.Request(url, headers={
            "User-Agent": "setu-flex-pull",
            "Accept": "text/csv, application/xml, text/xml",
        })
        with self._opener.open(req, timeout=60) as resp:
            return resp.read().decode("utf-8", errors="replace")

    def send_request(self, query_id: str, from_date: date,
                     to_date: date) -> str:
        query = urllib.parse.urlencode({
            "t": self._token, "q": query_id, "v": "3",
            "fd": from_date.strftime("%Y%m%d"),
            "td": to_date.strftime("%Y%m%d"),
        })
        body = self._get(f"{FLEX_BASE}.SendRequest?{query}")
        try:
            info = parse_flex_xml(body)
        except FlexError as exc:
            # Not XML at all: when IBKR's Flex service degrades
            # it answers some calls with non-XML bodies (seen
            # live in the GetStatement poll 2026-10-29 06:45).
            # Save the body and let fetch()'s send ladder treat
            # it like 1001 — same sickness, same cadence.
            excerpt = self._note_unparseable("SendRequest", body)
            raise FlexError(
                "SendRequest returned an unparseable response: "
                f"{excerpt}", code="unparseable") from exc
        if info["status"] == "Success" and info["reference"]:
            return info["reference"]
        raise FlexError(
            "SendRequest failed: "
            f"{info['error_code']} {info['error_message']}".strip(),
            code=info["error_code"] or None)

    def fetch(self, query_id: str, from_date: date, to_date: date,
              max_wait_s: int = DEFAULT_MAX_WAIT_S,
              interval_s: int = DEFAULT_INTERVAL_S) -> str:
        # 1001 ("could not be generated at this time, try
        # again shortly") is IBKR-side and transient — but
        # "shortly" is minutes, not seconds: retrying every
        # 20s (0.2.2) tripped IBKR's attempt limiter, error
        # 1025, live 2026-10-27. Send retries therefore use
        # a short, minutes-scale schedule (5, 10, 15 min —
        # at most 4 attempts), and 1025 itself never retries.
        waited_send = 0
        delays = list(SEND_RETRY_DELAYS_S)
        while True:
            try:
                reference = self.send_request(query_id, from_date,
                                              to_date)
                break
            except FlexError as exc:
                if (exc.code not in ("1001", "unparseable")
                        or not delays
                        or waited_send + delays[0] > max_wait_s):
                    raise
                what = ("SendRequest 1001 (IBKR busy)"
                        if exc.code == "1001"
                        else "SendRequest unparseable (IBKR degraded)")
                self._log(f"{what}; retrying in {delays[0]}s")
                self._sleep(delays[0])
                waited_send += delays.pop(0)
        # GetStatement takes the reference in q, like
        # SendRequest takes the query id — a "ref" parameter
        # is ignored and IBKR answers 1020 (seen live
        # 2026-10-27, flex_pull 0.2.0).
        query = urllib.parse.urlencode({
            "t": self._token, "q": reference, "v": "3"})
        url = f"{FLEX_BASE}.GetStatement?{query}"
        waited = 0
        unparseable_polls = 0
        while True:
            body = self._get(url)
            if looks_like_statement(body) or looks_like_flex_csv(body):
                return body
            try:
                info = parse_flex_xml(body)
            except FlexError:
                # A non-XML poll body while IBKR is degraded
                # (live 2026-10-29 06:45: one junk poll killed
                # the run 20s after a clean 1019). It carries no
                # verdict about the statement, so it is treated
                # as still-generating: save the body, log it,
                # keep polling inside the same wait budget.
                excerpt = self._note_unparseable("GetStatement", body)
                unparseable_polls += 1
                if waited >= max_wait_s:
                    raise FlexError(
                        "statement not retrievable after "
                        f"{max_wait_s}s (ref {reference}); poll "
                        "responses turned unparseable "
                        f"(x{unparseable_polls}, latest saved): "
                        f"{excerpt}")
                self._log("unparseable poll response (saved); "
                          "treating as still generating; "
                          f"waited {waited}s")
                self._sleep(interval_s)
                waited += interval_s
                continue
            if info["error_code"] in RETRYABLE_ERROR_CODES:
                if waited >= max_wait_s:
                    raise FlexError(
                        "statement still generating after "
                        f"{max_wait_s}s (ref {reference})")
                self._log("statement still generating; "
                          f"waited {waited}s")
                self._sleep(interval_s)
                waited += interval_s
                continue
            raise FlexError(
                "GetStatement failed: "
                f"{info['error_code']} {info['error_message']}".strip())


def load_config(path: Path) -> list:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise FlexError(f"config not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise FlexError(f"config is not valid JSON: {exc}") from exc
    jobs = data.get("jobs")
    if not isinstance(jobs, list):
        raise FlexError("config carries no 'jobs' list")
    for job in jobs:
        for key in ("name", "account_id", "query_id",
                    "token_file", "output_dir"):
            if not job.get(key):
                raise FlexError(f"a job is missing '{key}': {job!r}")
    return jobs


IBKR_PAUSE_S = 3600  # documented 1025 pause: one hour


def _pause_path(backup_root) -> Path:
    base = backup_root.parent if backup_root is not None \
        else Path.home() / ".setu"
    return base / "flex-pause.json"


def _pause_until(name: str, backup_root, now: datetime):
    """The recorded 1025 pause deadline for a job, if still in
    force. IBKR's 1025 block lasts about an hour and covers
    the query with any token and the token with any query —
    a fresh token does not help, and every attempt inside
    the window feeds it (learned 2026-10-28)."""
    try:
        data = json.loads(
            _pause_path(backup_root).read_text(encoding="utf-8"))
        until = datetime.fromisoformat(data[name]["until"])
    except (OSError, ValueError, KeyError, TypeError):
        return None
    return until if until > now else None


def _record_pause(name: str, backup_root, now: datetime) -> None:
    path = _pause_path(backup_root)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    data[name] = {"until": (now + timedelta(
        seconds=IBKR_PAUSE_S)).isoformat(), "code": "1025"}
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def run_job(job: dict, asof: date, client_factory=None,
            backup_root: Path | None = None) -> dict:
    name = job["name"]
    paused = _pause_until(name, backup_root,
                          datetime.now().astimezone())
    if paused is not None:
        raise FlexError(
            "IBKR 1025 pause in force until "
            f"{paused:%H:%M} — not calling (attempts inside "
            "the window feed the block)", code="1025")
    token_path = Path(job["token_file"]).expanduser()
    try:
        token = token_path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise FlexError(f"token file unreadable: {token_path}") from exc
    if not token:
        raise FlexError(f"token file is empty: {token_path}")
    def _save_unparseable(stage: str, body: str) -> None:
        if backup_root is None:
            return
        try:
            backup_root.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
            (backup_root / f"{name}-unparseable-{stage}-{stamp}.txt"
             ).write_text(body, encoding="utf-8")
        except OSError:
            pass

    factory = client_factory or (
        lambda tok: FlexClient(tok, log=log_line,
                               on_unparseable=_save_unparseable))
    client = factory(token)
    start, end, stem = month_window(asof)
    try:
        csv_text = client.fetch(job["query_id"], start, end)
    except FlexError as exc:
        if exc.code == "1025":
            _record_pause(name, backup_root,
                          datetime.now().astimezone())
        raise
    raw_text = csv_text
    converted = False
    if job.get("convert"):
        # The morning file is a portal statement, not a Flex
        # query; the SetuTxHistory Activity output is rebuilt
        # into it by flex_convert (see that module). A warning
        # means an unmapped source row: fail the job rather
        # than write a partial statement over a good file.
        try:
            import flex_convert
        except ImportError as exc:
            raise FlexError(
                "convert mode needs flex_convert.py beside "
                "flex_pull.py") from exc
        csv_text, conv_warnings = flex_convert.convert(
            csv_text, start.strftime("%Y%m%d"),
            end.strftime("%Y%m%d"),
            generated_at=datetime.now().astimezone())
        if conv_warnings:
            raise FlexError(
                "conversion incomplete: "
                + "; ".join(conv_warnings[:5]))
        converted = True
        if backup_root is not None:
            backup_root.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
            (backup_root / f"{name}-{stem}-flex-raw-{stamp}.csv"
             ).write_text(raw_text, encoding="utf-8")
    info = validate_statement(csv_text)
    out_dir = Path(job["output_dir"]).expanduser()
    out_dir.mkdir(parents=True, exist_ok=True)
    target = out_dir / f"{stem}.csv"
    if target.exists() and backup_root is not None:
        backup_root.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        (backup_root / f"{name}-{stem}-{stamp}.csv").write_bytes(
            target.read_bytes())
    tmp_path = out_dir / f".{stem}.csv.tmp"
    tmp_path.write_text(csv_text, encoding="utf-8")
    os.replace(tmp_path, target)
    return {"job": name, "account_id": job["account_id"], "ok": True,
            "file": str(target), "rows": info["rows"],
            "period": info["period"], "converted": converted}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="flex_pull")
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run")
    run.add_argument("--config", default="~/.setu/flex-pull.json")
    run.add_argument("--date", default=None,
                     help="as-of date YYYY-MM-DD (default today)")
    args = parser.parse_args(argv)

    asof = (date.fromisoformat(args.date) if args.date else date.today())
    config_path = Path(args.config).expanduser()
    setu_home = config_path.parent
    results = []
    try:
        jobs = load_config(config_path)
    except FlexError as exc:
        log_line(f"flex_pull {FLEX_PULL_VERSION}: {exc}")
        return 2
    if not jobs:
        log_line(f"flex_pull {FLEX_PULL_VERSION}: no jobs configured "
                 f"in {config_path}; nothing to do")
        return 2
    log_line(f"flex_pull {FLEX_PULL_VERSION}: {len(jobs)} job(s), "
             f"as of {asof.isoformat()}")
    for job in jobs:
        try:
            result = run_job(job, asof,
                             backup_root=setu_home / "flex-backups")
        except (FlexError, OSError) as exc:
            result = {"job": job.get("name", "?"), "ok": False,
                      "error": str(exc)}
            log_line(f"  {result['job']}: FAILED — {exc}")
        else:
            log_line(f"  {result['job']}: wrote {result['file']} "
                     f"({result['rows']} rows, {result['period']})")
        results.append(result)
    status = {"at": datetime.now().isoformat(timespec="seconds"),
              "asof": asof.isoformat(), "results": results}
    try:
        (setu_home / "flex-last-run.json").write_text(
            json.dumps(status, indent=2), encoding="utf-8")
    except OSError as exc:
        log_line(f"  (could not write flex-last-run.json: {exc})")
    return 0 if all(r["ok"] for r in results) else 1


if __name__ == "__main__":
    sys.exit(main())
