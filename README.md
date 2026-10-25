# Setu — the bridge between IB Gateway and Kubera

Setu is a small, read-only daemon that sits next to IB Gateway on the
same machine. Every minute it pulls account values, open orders and
(v0.2.0) the live portfolio over the local Gateway API socket and
pushes them to Kubera's broker ingest endpoints — the same contracts
the IBKR Enhancer userscript uses, so Kubera's diffing, GTC coverage
checks and Pushover margin alerts keep working unchanged, with no
browser tab to keep alive.

Setu **never places, cancels or modifies an order**. It only reads:
the account summary, and all open orders for one configured account
(other accounts on the same login, such as a JISA, are never sent).

## Install — one command

Prerequisite, once per machine: the Kubera ingest token in
`~/.kubera-ingest-token` (one line; the file already exists on the
Mac Studio from the Enhancer setup — on a second machine, copy that
one line into a file of the same name).

```sh
git clone https://github.com/zonito/setu.git
cd setu
./install.sh                                        # Mac Studio: U19377323, local Kubera
./install.sh U26948221 http://100.125.239.56:8000   # second machine: Nikhila's account
```

The installer creates `~/.setu` (Python venv, config, logs), installs
a LaunchAgent (`com.molang.setu`) that starts Setu at every login and
restarts it if it ever exits, starts it immediately, and shows the
first log lines. Re-running `./install.sh` after a `git pull` upgrades
in place and keeps your config. `./uninstall.sh` stops it.

After install, daily life is: **log in to IBKR Gateway. Done.** Setu
reconnects to the Gateway on its own (backoff up to a minute), pushes
changed values within a minute, and heartbeats unchanged values every
5 minutes so Kubera never shows the feed stale while Setu is alive.

## Positions feed (v0.2.0)

Setu subscribes to the Gateway's account-updates stream for the
configured account only (`reqAccountUpdates`). Portfolio updates land
in a positions set keyed by contract id; whenever the mapped set
changes (or on the 5-minute heartbeat) Setu pushes:

```
POST /api/v1/broker-positions/snapshot
{ "account_id": "U…", "ingest_source": "setu",
  "captured_at": "2026-10-23T09:00:00+01:00", "complete": true,
  "positions": [ { "con_id": 265598, "symbol": "ACME",
                   "local_symbol": "ACME", "sec_type": "STK",
                   "expiry": null, "strike": null, "option_type": null,
                   "multiplier": 1, "currency": "USD",
                   "exchange": "NASDAQ", "quantity": 150.0,
                   "avg_cost": 180.40, "market_price": 201.25,
                   "market_value": 30187.5, "unrealized_pnl": 3127.5,
                   "realized_pnl": 0.0 }, … ] }
```

Quantities are signed (shorts negative). `avg_cost` is the raw
per-unit value IBKR reports, never scaled by the multiplier. Option
expiry is ISO (`YYYY-MM-DD`), `option_type` is `Put`/`Call`.

The same stream also fills the account snapshot's `unrealized_pnl`,
`realized_pnl` and `settled_cash` when the account-summary sweep
leaves them null on a login; a real sweep value always wins. (On the
stream these arrive as `$LEDGER-UnrealizedPnL` / `$LEDGER-RealizedPnL`
and a by-date `SettledCashByDate` breakdown, whose latest date Setu
parses — verified live against the real Gateway.)

**404-tolerant:** the Kubera positions endpoint is deployed
separately. Until it exists, Setu logs one line ("positions endpoint
not deployed yet"), keeps retrying quietly on change/heartbeat, and
the accounts and orders feeds are never disturbed.

**Layered design rule:** the broker feeds are a live mirror held in
their own store. The transactions ledger stays the book of record
for cost basis, realised profit, tax and history, and the feeds
never write to it. The feeds answer "what is true right now"; the
ledger answers "what happened".

## setu-exec — the guarded GTC executor (`setu_exec.py`)

A **separate program** from the read daemon, with its own Gateway
client id (91) and its own LaunchAgent (`com.molang.setu-exec`,
installed by `./exec-install.sh`). Setu itself stays incapable of
placing orders; the executor is the only component that can, and it
does exactly one job: take Kubera staged orders Zonito has APPROVED
(buy-to-close limits for short options at the 60% take-profit price),
revalidate them live, and report the outcome back to Kubera.

**Modes** live in `~/.setu/executor.json` (the read daemon's
`config.json` is never touched):

- `"simulate"` — the default and the starting mode. Every gate runs,
  including the Gateway what-if preview, and the executor reports
  `would_place`. Simulate is incapable of transmitting: its only
  order call carries `whatIf=True`.
- `"live"` — after the same gates, the same order is transmitted
  with `whatIf=False`. Live additionally requires the enable file
  `~/.setu/executor.live` to exist; mode `live` without it refuses
  to start. (`setu_exec.py enable-live` / `disable-live` manage it.)

**Daily ritual:** the executor also needs a fresh arm every day —
`~/.setu/venv/bin/python ~/.setu/setu_exec.py arm` writes today's
date (Europe/London) into `~/.setu/executor.armed`; yesterday's arm
never works, and `disarm` removes it. Kubera's per-account kill
switch is read fresh every cycle and idles the executor while on.

**Gates, in order, every placement:** armed → kill switch off →
fresh live reads from the Gateway (never Kubera snapshots) → the
short position is real and at least as large as asked (a smaller
short closes what exists, flagged) → no duplicate (nothing carrying
this row's `orderRef`, no working buy on the contract at the same
price) → price sane (target = 0.40 × premium tick-rounded, premium
within 10% of the live per-unit average cost) → frozen order shape
(BUY / LMT / GTC / regular hours / single leg — nothing else can be
constructed) → what-if preview succeeds and does not materially
increase initial margin. Any failure is reported to Kubera as
`gate_failed` with the gate named. There are deliberately **no
quantity caps** (dropped 2026-10-23); the gates carry the safety.

**Placed orders are tracked** in `~/.setu/executor-state.json` and
followed to a terminal status (`filled` / `cancelled` / `rejected`
reported back). On a restart, tracked orders are reconciled by
`orderRef` against live open orders before anything new runs, so a
crash mid-placement can never double-place a row.

**Purpose of the dry run:** simulate mode exists so a run of
`WOULD PLACE` lines can be reconciled against the GTC orders placed
by hand in the portal, proving the gates agree with human judgement
before live mode is enabled.

### Cancel rows (setu-exec v0.3.0)

Staged rows may also carry `action: "cancel"` (contract:
`docs/kubera-cancel-contract.md`). A cancel row names ONE working
order — by the broker-orders feed's order id, which is the order's
perm id — plus the contract identity copied from the feed at raise
time. Kubera's orphan scanner auto-raises these rows for orders the
coverage check flags as orphans; Zonito approves each one (Phase 1).

**Cancel gates, in order:** target still working (absent is a
terminal no-op, `already_gone`, never a refusal) → identity match on
con_id and action → verified completely unfilled (a partial fill is
a human decision) → orphan premise re-check for scanner rows (if a
position has appeared behind the order, the premise changed: refuse)
→ never this program's own placed orders via a scanner row. There is
no what-if for a cancel, so simulate stops at the gates and reports
`would_execute`. Live mode transmits the cancel under the same
runaway brake as placements, then verifies on a FRESH open-orders
read that the target is actually gone before reporting `executed`.
Every cancel, approved or (later) bypassed, lives as a staged row
and files into Kubera's Closed ledger — no row, no execution, ever.

**One extra Gateway setup step for cancels:** cancelling an order
placed by another client requires this executor's client id (91) to
be the account's **Master API Client ID** (Gateway → Configure →
Settings → API → Settings). Without it the Gateway refuses with
error 10147, which the executor surfaces verbatim as the row's
failure reason. The first live cancel is the proof of the setting.

**Phase 2 (after confidence):** when the `cancel_streak` counter in
`executor-state.json` — reported in every `executed` event — reaches
the agreed bar, Kubera may stamp scanner rows
`approval_mode: "bypass"` and approve them without a human wait. The
executor treats those rows identically; every gate still applies.

## Config — `~/.setu/config.json`

| Key | Meaning |
|---|---|
| `account_id` | The one account this machine feeds (allowlisted in Kubera) |
| `account_label` | Optional display label, or `null` |
| `ib_host` / `ib_port` | Gateway API socket, normally `127.0.0.1:4001` |
| `client_id` | API client id (any free number per Gateway) |
| `kubera_base` | Kubera root URL (`127.0.0.1:8000` on the Mac Studio) |
| `token_file` | Path to the ingest token file |
| `interval_seconds` | Push cadence, default 60 |
| `heartbeat_seconds` | Repush unchanged values anyway, default 300 |
| `http_proxy` | Optional; only for reaching Kubera via a proxy |

## Notes & limits

- The Gateway is the always-on session for its login. Opening Client
  Portal with the same username can disconnect the Gateway's
  brokerage session (IBKR allows one); log the Gateway back in and
  Setu recovers by itself.
- IB Gateway needs its weekly re-authentication (after the Saturday
  night server reset). Set Gateway → Configure → Lock and Exit to
  **auto-restart** rather than auto-logoff to minimise babysitting;
  the Sunday re-login is still yours.
- Combo (BAG) orders are reported as one order with an empty legs
  list in v1; leg detail is a planned enhancement.
- Transition: when Setu takes over an account, switch that account's
  browser export off in the Enhancer gear panel so the two feeds do
  not double-report orders during the handover.
- Logs: `~/.setu/logs/setu.log` and `~/.setu/logs/setu-exec.log`.
  Both are bounded (setu v0.2.1 / setu-exec v0.2.2): each program
  rotates its own log at ~2 MB keeping 3 backups (~8 MB max), and
  repeated IB farm/connectivity errors are logged once per run
  with a summary count instead of one line per cycle. A dry run
  that prints the exact snapshots without sending:
  `~/.setu/venv/bin/python ~/.setu/setu.py --dry-run`.

## Growth path

Positions snapshots landed in v0.2.0. The guarded GTC executor
(setu-exec, above) landed as a separate program in simulate-first
form. Next: executions (fills stream in real time, no more CSV).
The ingest side of Kubera grows a new endpoint per step; Setu grows
a new reader.
