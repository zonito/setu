# Setu — the bridge between IB Gateway and Kubera

Setu is a small, read-only daemon that sits next to IB Gateway on the
same machine. Every minute it pulls account values and open orders
over the local Gateway API socket and pushes them to Kubera's broker
ingest endpoints — the same contracts the IBKR Enhancer userscript
uses, so Kubera's diffing, GTC coverage checks and Pushover margin
alerts keep working unchanged, with no browser tab to keep alive.

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
- Logs: `~/.setu/logs/setu.log`. A dry run that prints the exact
  snapshots without sending: `~/.setu/venv/bin/python ~/.setu/setu.py --dry-run`.

## Growth path

Positions snapshots, then executions (fills stream in real time, no
more CSV), then what-if margin checks for the wheel pipeline. The
ingest side of Kubera grows a new endpoint per step; Setu grows a new
reader.
