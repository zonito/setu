# kubera-cancel-contract.md — staged CANCEL rows (setu-exec v0.3)

The contract between Kubera (staged orders API + panel + orphan
scanner) and setu-exec for the executor's second action: cancelling
an existing working order. Placement rows are unchanged; everything
here is additive. Implemented executor-side in setu-exec v0.3.0
(2026-10-25); the Kubera side is built against this document.

## 1. The cancel row

A staged row is a cancel when it carries `"action": "cancel"`
(absent = "place", every pre-v0.3 row). Fields:

| Field | Meaning |
|---|---|
| `id` | Staged row id (as today). One row produces at most one execution, ever. |
| `account_id` | The IBKR account the order lives in. |
| `action` | `"cancel"`. |
| `con_id` | 0 for Kubera-raised rows: the broker-orders feed carries no con_id, so the executor resolves the contract from the live order it matches by perm id and verifies the descriptive identity fields (symbol/expiry/strike/type) against that live contract. A row that does carry a con_id > 0 must match it exactly. |
| `symbol`, `expiry`, `strike`, `option_type` | The contract identity, copied from the broker-orders feed row at raise time (display + identity check). |
| `target_order_id` | The broker-orders feed's `order_id` for the target — Setu stores the order's **perm id**, globally unique. This is the join key. |
| `target_action` | `"Buy"` / `"Sell"` from the feed row. |
| `target_quantity` | Total quantity from the feed row (display). |
| `target_limit_price` | Limit price from the feed row (display). |
| `reason` | Why, e.g. `"orphan_order: no position for con_id … per coverage"`. Shown in the panel. |
| `source` | `"orphan_scan"` for scanner-raised rows, `"manual"` for rows Zonito raises by hand. Drives the premise gate (C4 applies to orphan_scan only) and the own-order gate (C5: manual rows may cancel setu-exec's own orders; scanner rows never may). |
| `approval_mode` | `"manual"` (Phase 1), `"bypass"` (Phase 2, stamped by Kubera when auto-approving). Recorded on the row forever; the Closed ledger shows it. |

Premium / target_limit are unused for cancel rows.

## 2. Lifecycle and events

The executor polls `state=APPROVED` exactly as today and posts events
to `POST /api/v1/broker-staged-orders/{id}/event`. Kinds for cancel
rows:

| Event kind | Meaning | Kubera maps to |
|---|---|---|
| `executing` | Executor picked the row up (detail: `mode`). | EXECUTING |
| `would_execute` | Simulate outcome, OR live deferral (detail `deferred: true`, gate `runaway_brake`), OR simulate gate failure (detail `simulate_gate_failed: true`, `gate`, `reason`). | Back to APPROVED |
| `gate_failed` | Live gate refusal (detail: `gate`, `reason`). Terminal. | REFUSED |
| `already_gone` | Target no longer a working order at execute time — the cancel's goal is already met. Terminal no-op. | EXECUTED (Closed ledger), outcome `already_gone` |
| `executed` | Cancel transmitted AND verified absent on a fresh open-orders read. Detail: `target_order_id`, `perm_id`, `verified_absent: true`, `approval_mode`, `cancel_streak` (the new value). | EXECUTED (Closed ledger), outcome `cancelled` |
| `failed` | Transmit error (e.g. IB 10147) — detail `reason` verbatim. Terminal. | FAILED |
| `executed` with `recovered: true` | A tracked cancel resolved on a later read after a restart/unverified transmit. | EXECUTED (Closed ledger), outcome `cancelled` |

Notes:

- A cancel whose Gateway callback says Cancelled but which still
  appears on the verification read is NOT reported: it stays
  EXECUTING (tracked by the executor) with a Pushover to Zonito,
  and resolves later via the `recovered` path or a human. Kubera
  should surface long-lived EXECUTING cancel rows rather than
  timing them out silently.
- Gate names: `target`, `identity`, `unfilled`, `premise`,
  `own_order`, `reads_incomplete`, `runaway_brake`.

## 3. The orphan scanner (Kubera side)

- Source of truth: the coverage computation over the broker feed
  (positions + orders snapshots). An order is an orphan when no
  live position exists for its contract (the existing coverage
  rule: an order whose con_id has no position).
- On detecting an orphan with NO live staged cancel row for that
  `target_order_id`, auto-raise a cancel row (§1) with
  `source="orphan_scan"`, reason quoting the coverage evidence.
- Dedupe is absolute: at most one non-terminal cancel row per
  `target_order_id` per account, and if Zonito ever REJECTS a
  raised row, that `target_order_id` is never auto-raised again.
  A rejection is a decision, not a delay. (A manually raised row
  for the same order remains possible — that is a new decision.)
- Rows raised for orders that disappear on their own resolve
  themselves: the executor reports `already_gone`.

## 4. Approval phases and the Closed ledger

- Phase 1: scanner raises → Zonito approves per row → executor runs.
- Phase 2 ("after confidence"): when the executor's reported
  `cancel_streak` reaches the agreed bar (5 clean executions at
  design time) AND Zonito enables auto mode per account, the
  scanner stamps new orphan rows `approval_mode="bypass"` and
  moves them to APPROVED itself. The executor is unchanged.
- LEDGER RULE (Zonito, 2026-10-25): bypassed rows are ledger rows
  like any other. Every executed cancel files into the panel's
  Closed section with its full trail (raised_by scanner,
  approval_mode, events, verification). Bypassed rows carry a
  visible AUTO marker in the Closed list. No row, no execution.
- Cancels of orders covering a live position are never raised by
  the scanner and never bypassed; a manual row for one stays a
  per-row human decision forever. (By the coverage definition the
  scanner only ever sees orphans anyway.)

## 5. Executor guarantees (for the panel copy)

- Setu the daemon is untouched and remains read-only; all cancel
  behaviour lives in setu-exec (client id 91).
- A cancel executes only in live mode with the daily arm file,
  `executor.live`, and the Kubera kill switch off — the same
  posture as placements — and counts against the shared runaway
  brake (60s spacing, 25/day).
- Cancelling another client's order needs client 91 set as the
  account's Master API Client ID in Gateway settings; without it
  the Gateway refuses (IB error 10147) and the row fails with that
  reason. Setup step, verified by the first live rehearsal.

## 6. Web route addendum (setu-exec v0.8)

- Some orders report no socket session id even on a bound
  client-0 session (Client Portal orders; "Class B"). No socket
  cancel can address them, and reqGlobalCancel stays forbidden.
  For those rows only, the executor falls back to the IBKR Web
  API: `DELETE /v1/api/iserver/account/{accountId}/order/{orderId}`
  under OAuth 1.0a self-service credentials held per account on
  the executor's machine (`~/.setu/webapi-<account>.json`, never
  in Kubera, never in the repo).
- Rows may carry `web_order_id` (the Web API order id; in
  practice the same number as the feed's perm id, which remains
  `target_order_id`) and a `cancel_route` stamp ("socket"/"web")
  for display. The executor does not trust the stamp: the route
  is decided at execution from the live target (session id
  present → socket; absent + credentials present → web; absent +
  no credentials → the legacy failure).
- Route selection happens only after every existing gate in
  sections 1–3 passes (arm, kill switch, fresh complete reads,
  identity, unfilled, premise, own-order, runaway brake). The web
  route then re-verifies identity on its own channel (order id,
  account, conid, side, working status, unfilled, quantity,
  limit) before any DELETE; a mismatch is a `web_identity`
  refusal and no DELETE is sent.
- The DELETE response is an acknowledgement, never proof:
  `executed` requires the target absent from BOTH the Web API
  live-orders list and the Setu feed on fresh reads. The DELETE
  counts as the row's one brake transmission. Streak, terminal
  states and the Closed-ledger rule are unchanged and shared
  with the socket route; executed web cancels carry
  `route: "web"` and `web_order_id` in their event detail.
- Kubera needs no change to support the route: rows without
  `web_order_id` work (the perm id is the fallback key), and a
  scanner stamp, when present, is display-only.
