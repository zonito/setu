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

FLEX_PULL_VERSION = "0.1.0"
FLEX_BASE = ("https://ndcdyn.interactivebrokers.com"
             "/Universal/servlet/FlexStatementService")
# IBKR error 1019: statement generation in progress — retryable.
RETRYABLE_ERROR_CODES = {"1019"}
DEFAULT_MAX_WAIT_S = 1800
DEFAULT_INTERVAL_S = 20
_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun",
           "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


class FlexError(Exception):
    """A Flex failure worth surfacing verbatim in the log."""


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
            or "Transaction History,Header" in text)


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

    def __init__(self, token: str, opener=None, sleeper=None):
        self._token = token
        self._opener = opener or _no_proxy_opener()
        self._sleep = sleeper if sleeper is not None else time.sleep

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
        info = parse_flex_xml(body)
        if info["status"] == "Success" and info["reference"]:
            return info["reference"]
        raise FlexError(
            "SendRequest failed: "
            f"{info['error_code']} {info['error_message']}".strip())

    def fetch(self, query_id: str, from_date: date, to_date: date,
              max_wait_s: int = DEFAULT_MAX_WAIT_S,
              interval_s: int = DEFAULT_INTERVAL_S) -> str:
        reference = self.send_request(query_id, from_date, to_date)
        query = urllib.parse.urlencode({
            "t": self._token, "ref": reference, "v": "3"})
        url = f"{FLEX_BASE}.GetStatement?{query}"
        waited = 0
        while True:
            body = self._get(url)
            if looks_like_statement(body):
                return body
            info = parse_flex_xml(body)
            if info["error_code"] in RETRYABLE_ERROR_CODES:
                if waited >= max_wait_s:
                    raise FlexError(
                        "statement still generating after "
                        f"{max_wait_s}s (ref {reference})")
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


def run_job(job: dict, asof: date, client_factory=None,
            backup_root: Path | None = None) -> dict:
    name = job["name"]
    token_path = Path(job["token_file"]).expanduser()
    try:
        token = token_path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise FlexError(f"token file unreadable: {token_path}") from exc
    if not token:
        raise FlexError(f"token file is empty: {token_path}")
    factory = client_factory or (lambda tok: FlexClient(tok))
    client = factory(token)
    start, end, stem = month_window(asof)
    csv_text = client.fetch(job["query_id"], start, end)
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
            "period": info["period"]}


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
