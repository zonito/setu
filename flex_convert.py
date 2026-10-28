#!/usr/bin/env python3
"""flex_convert — Activity Flex CSV -> Transaction History CSV.

IBKR's Flex Web Service serves saved Flex Queries only; the
"Transaction History" statement Zonito uploads every morning is
a portal report, not a query. This converter rebuilds that
exact statement from the SetuTxHistory Activity Flex Query
(sections Trades, Cash Transactions, Cash Report, Transaction
Fees) so flex_pull can fetch it on demand.

The mapping was derived from a real pair (the SetuTxHistory
output and the hand-downloaded statement for the same account
and period, October 2026) and is verified against it:

- TRNT (Trades) executions map 1:1 to Buy/Sell rows; executions
  are NOT aggregated (the statement lists executions too).
- BookTrade rows are settlements: the stock leg becomes an
  Assignment/Exercise row, the option leg is dropped (the
  statement shows only the stock leg).
- A CASH trade becomes a Forex Trade Component row. Its exact
  residual needs IBKR's daily FX translation rate, which Flex
  does not carry, so the converter uses the trade's own MtmPnl;
  the importer skips this row type wholesale regardless.
- CTRN (Cash Transactions) types map to the statement's cash
  types; descriptions are re-rendered in the statement's own
  phrasing (the importer parses dividend rates out of the
  description text, so the phrasing is functional, not cosmetic).
- The statement breaks VAT out of OPRA subscription fees as
  Sales Tax rows; the converter synthesises them (20%).
- Numbers render as Python float reprs, exactly like the
  statement itself (including its float artefacts:
  -187.11 + -0.34257825 -> -187.45257825000002).
- Summary comes from CRTT's BASE_SUMMARY row.

Any source row the converter cannot map is reported as a
warning; flex_pull treats warnings as a failed run and never
overwrites the existing file with a partial conversion.

Stdlib only, like the rest of Setu.
"""
import csv
import io
import re
from datetime import datetime

CONVERT_VERSION = "0.2.1"

TH_HEADER = ["Transaction History", "Header", "Date", "Account",
             "Description", "Transaction Type", "Symbol", "Quantity",
             "Price", "Price Currency", "Gross Amount ", "Commission",
             "Net Amount"]

_MONTHS_FULL = ("January", "February", "March", "April", "May", "June",
                "July", "August", "September", "October", "November",
                "December")
_MONTHS_ABBR = tuple(m[:3] for m in _MONTHS_FULL)

# Words the statement renderer keeps in capitals inside
# descriptions (currencies, KR tax, FEE, brand tokens).
_KEEP_UPPER = {"USD", "GBP", "EUR", "CHF", "JPY", "KR", "FEE",
               "IBKR", "SYEP", "VAT", "NP"}

# Function words the statement renderer lowercases mid-phrase
# ("per Share", "Payment in Lieu of Dividend", "Initiated by").
_FUNCTION_WORDS = {"per", "of", "in", "by", "for", "from"}

_INTEREST_PHRASES = {
    "DEBIT INT": "Debit Interest",
    "CREDIT INT": "Credit Interest",
    "BORROW FEES": "Borrow Fees",
    "IBKR MANAGED SECURITIES (SYEP) INTEREST":
        "IBKR Managed Securities (SYEP) Interest",
}

_CASH_TYPE_MAP = {
    "Dividends": "Dividend",
    "Withholding Tax": "Foreign Tax Withholding",
    "Payment In Lieu Of Dividends": "Payment in Lieu",
    "Other Fees": "Other Fee",
    "Broker Interest Paid": "Debit Interest",
    "Broker Interest Received": "Credit Interest",
}


def _f(value) -> str:
    """Float rendering exactly as the statement does it."""
    return str(float(value))


def _date(yyyymmdd: str) -> str:
    return f"{yyyymmdd[:4]}-{yyyymmdd[4:6]}-{yyyymmdd[6:8]}"


def parse_flex(text: str) -> dict:
    """Split a Flex CSV (HEADER/DATA + section code) into
    {code: [row dict, ...]} keyed by the section's own fields."""
    sections: dict[str, list[dict]] = {}
    fields: dict[str, list[str]] = {}
    for row in csv.reader(io.StringIO(text)):
        if len(row) < 2:
            continue
        kind, code = row[0], row[1]
        if kind == "HEADER":
            fields[code] = row[2:]
            sections.setdefault(code, [])
        elif kind == "DATA" and code in fields:
            sections[code].append(dict(zip(fields[code], row[2:])))
    return sections


def _case_word(word: str) -> str:
    """Statement-case one token: identifiers (digits, *, [, :,
    account masks) and keep-upper words pass through verbatim;
    anything else is capitalised."""
    if not word:
        return word
    core = word.strip("()")
    if any(ch.isdigit() for ch in core) or "*" in core or ":" in core:
        return word
    upper = core.upper()
    if upper in _KEEP_UPPER:
        return word.replace(core, upper)
    cased = core[:1].upper() + core[1:].lower()
    return word.replace(core, cased)


def statement_case(text: str) -> str:
    words = text.split(" ")
    out = []
    for i, w in enumerate(words):
        if i > 0 and w.lower() in _FUNCTION_WORDS:
            out.append(w.lower())
        elif (w.upper() == "FEE" and out and out[-1] != "-"):
            # "Withdrawal Fee" — but a dividend "- FEE" stays loud.
            out.append("Fee")
        else:
            out.append(_case_word(w))
    return " ".join(out)


def _convert_trade(t: dict, account: str, base_ccy: str,
                   warnings: list) -> list | None:
    """One TRNT row -> one TH row (or None when the row is a
    settlement option leg the statement omits)."""
    codes = t.get("Notes/Codes", "")
    code_set = {c.strip() for c in codes.split(";") if c.strip()}
    date = _date(t["TradeDate"])
    if t["TransactionType"] == "BookTrade":
        if t["AssetClass"] == "OPT":
            if code_set & {"A", "E", "Ep", "Ex"}:
                return None  # settlement leg; statement drops it
            warnings.append(f"unmapped BookTrade option row: {t}")
            return None
        if "A" in code_set:
            kind = "Assignment"
        elif "E" in code_set or "Ep" in code_set:
            kind = "Exercise"
        else:
            warnings.append(f"unmapped BookTrade stock row: {t}")
            return None
        qty = float(t["Quantity"])
        side = "Buy" if qty >= 0 else "Sell"
        desc = (f"{side} {abs(qty):,.0f} {t['Description']} ({kind})")
        return ["Transaction History", "Data", date, account, desc,
                kind, t["Symbol"], _f(t["Quantity"]),
                _f(t["TradePrice"]), t["CurrencyPrimary"],
                _f(t["Proceeds"]), "-", _f(t["Proceeds"])]
    if t["TransactionType"] != "ExchTrade":
        warnings.append(f"unmapped trade row: {t}")
        return None
    if t["AssetClass"] == "CASH":
        amount = _f(t.get("MtmPnl") or "0")
        desc = (f"Net Amount in Base from Forex Trade: "
                f"{_f(t['Quantity'])} {t['Symbol']}")
        return ["Transaction History", "Data", date, account, desc,
                "Forex Trade Component", t["Symbol"],
                _f(t["Quantity"]), _f(t["TradePrice"]), base_ccy,
                amount, "-", amount]
    comm = float(t["IBCommission"])
    net = float(t["Proceeds"]) + comm
    return ["Transaction History", "Data", date, account,
            t["Description"],
            "Buy" if t["Buy/Sell"] == "BUY" else "Sell",
            t["Symbol"], _f(t["Quantity"]), _f(t["TradePrice"]),
            t["CurrencyPrimary"], _f(t["Proceeds"]),
            "-" if comm == 0 else _f(t["IBCommission"]), str(net)]


def _opra_parts(desc: str):
    """Split an OPRA subscription-fee description into
    (kind, head, suffix): kind is "plain" or "cancel"; the
    suffix is the trailing " FOR MON YYYY" (or "")."""
    if ":OPRA" not in desc:
        return None
    m = re.match(r"^CANCEL\[(.+)\]( FOR [A-Z]{3} \d{4})$", desc)
    if m:
        return "cancel", m.group(1), m.group(2)
    m = re.match(r"^(.+?)( FOR ([A-Z]{3}) (\d{4}))$", desc)
    if m:
        return "plain", m.group(1), m.group(2)
    return None


def _fee_description(desc: str) -> str:
    """Statement phrasing for an Other Fees CTRN description."""
    m = re.match(r"^EXPOSURE FEE, EQUITY FOR (\d{8}) FOR .*$", desc)
    if m:
        return f"Exposure Fee, Equity for {m.group(1)}"
    parts = _opra_parts(desc)
    if parts:
        kind, head, suffix = parts
        if kind == "cancel":
            # The statement keeps a cancellation's suffix loud.
            return f"Cancel[{head}]{suffix}"
        mon, year = suffix.split()[-2], suffix.split()[-1]
        return f"{head} for {mon.capitalize()} {year}"
    return statement_case(desc)


def _vat_row(fee_row: dict, account: str,
             base_amount: float) -> list | None:
    """The statement breaks VAT out of OPRA subscription fees.
    Synthesise the companion Sales Tax row (20% of the fee)."""
    parts = _opra_parts(fee_row["Description"])
    if not parts:
        return None
    kind, head, _suffix = parts
    # The statement lowercases the whole masked account token
    # (up to the colon): Z******87 -> z******87 and, proven on
    # Nikhila's October file, N******KR -> n******kr.
    colon = head.index(":")
    head = head[:colon + 1].lower() + head[colon + 1:]
    text = f"VAT Cancel[{head}]" if kind == "cancel" else f"VAT {head}"
    vat = round(0.2 * base_amount, 2)
    date = _date(fee_row["Date/Time"].split(";")[0])
    return ["Transaction History", "Data", date, account, text,
            "Sales Tax", "-", "-", "-", "-", _f(vat), "-", _f(vat)]


def _interest_description(c: dict) -> str:
    desc = c["Description"]
    m = re.match(r"^([A-Z]{3}) (.+) FOR ([A-Z]{3})-(\d{4})$", desc)
    if m and m.group(2) in _INTEREST_PHRASES:
        mon = m.group(3).capitalize()
        return (f"{m.group(1)} {_INTEREST_PHRASES[m.group(2)]} "
                f"for {mon}-{m.group(4)}")
    return statement_case(desc)


def _base_amount(c: dict) -> float:
    """CTRN amounts are per-currency; the statement is
    base-currency, so convert at the row's own FX rate
    (GBP debit interest -1.29 x 1.3223 = -1.705767)."""
    return float(c["Amount"]) * float(c.get("FXRateToBase") or "1")


def _convert_cash(c: dict, account: str, warnings: list) -> list | None:
    ctype = c["Type"]
    date = _date(c["Date/Time"].split(";")[0])
    amount = _base_amount(c)
    if ctype == "Deposits/Withdrawals":
        th_type = "Withdrawal" if amount < 0 else "Deposit"
        desc = statement_case(c["Description"])
    elif ctype in _CASH_TYPE_MAP:
        th_type = _CASH_TYPE_MAP[ctype]
        if ctype in ("Broker Interest Paid", "Broker Interest Received"):
            desc = _interest_description(c)
        elif ctype == "Other Fees":
            desc = _fee_description(c["Description"])
        else:
            desc = statement_case(c["Description"])
    else:
        warnings.append(f"unmapped cash transaction: {c}")
        return None
    symbol = c.get("Symbol") or "-"
    return ["Transaction History", "Data", date, account, desc,
            th_type, symbol, "-", "-", "-", str(amount), "-",
            str(amount)]


def _base_currency(crtt: list) -> str:
    summary = next((r for r in crtt
                    if r.get("LevelOfDetail") == "BaseCurrency"), None)
    if summary is not None:
        for r in crtt:
            if (r.get("LevelOfDetail") == "Currency"
                    and r.get("StartingCash") == summary["StartingCash"]
                    and r.get("CurrencyPrimary") not in ("", "BASE_SUMMARY")):
                return r["CurrencyPrimary"]
    return "USD"


def convert(flex_text: str, from_date: str, to_date: str,
            generated_at: datetime | None = None):
    """Convert one Activity Flex CSV to (statement_text, warnings).

    from_date / to_date are YYYYMMDD bounds for the Statement
    Period line. generated_at stamps WhenGenerated (Eastern)."""
    sections = parse_flex(flex_text)
    warnings: list[str] = []
    crtt = sections.get("CRTT", [])
    base_ccy = _base_currency(crtt)
    summary = next((r for r in crtt
                    if r.get("LevelOfDetail") == "BaseCurrency"), None)
    if summary is None:
        raise ValueError("flex output carries no CRTT base summary row")
    account = summary.get("AccountAlias") or summary.get(
        "ClientAccountID", "")

    rows: list[list] = []
    for t in sections.get("TRNT", []):
        row = _convert_trade(t, account, base_ccy, warnings)
        if row is not None:
            rows.append(row)
    vat_rows: list[list] = []
    for c in sections.get("CTRN", []):
        row = _convert_cash(c, account, warnings)
        if row is not None:
            rows.append(row)
        if c.get("Type") == "Other Fees":
            vat = _vat_row(c, account, _base_amount(c))
            if vat is not None:
                vat_rows.append(vat)
    rows.extend(vat_rows)
    # Statement order: newest first; stable within a date.
    rows.sort(key=lambda r: r[2], reverse=True)

    def long_date(d: str) -> str:
        return (f"{_MONTHS_FULL[int(d[4:6]) - 1]} {int(d[6:8])}, {d[:4]}")

    period = f"{long_date(from_date)} - {long_date(to_date)}"
    stamp = ""
    if generated_at is not None:
        from zoneinfo import ZoneInfo
        eastern = generated_at.astimezone(ZoneInfo("America/New_York"))
        stamp = eastern.strftime("%Y-%m-%d, %H:%M:%S %Z")

    out = io.StringIO()
    w = csv.writer(out, lineterminator="\n")
    w.writerow(["Statement", "Header", "Field Name", "Field Value"])
    w.writerow(["Statement", "Data", "Title", "Transaction History"])
    w.writerow(["Statement", "Data", "Period", period])
    w.writerow(["Statement", "Data", "WhenGenerated", stamp])
    w.writerow(["Summary", "Header", "Field Name", "Field Value"])
    w.writerow(["Summary", "Data", "Base Currency", base_ccy])
    w.writerow(["Summary", "Data", "Starting Cash",
                summary["StartingCash"]])
    change = float(summary["EndingCash"]) - float(summary["StartingCash"])
    w.writerow(["Summary", "Data", "Change", str(change)])
    w.writerow(["Summary", "Data", "Ending Cash", summary["EndingCash"]])
    w.writerow(TH_HEADER)
    for row in rows:
        w.writerow(row)
    return out.getvalue(), warnings
