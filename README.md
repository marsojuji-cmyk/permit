# Permit

**Payment authority for AI agents.** Permit puts enforceable spending permissions between an AI agent and PayPal: a budget, approved merchants, an expiry, and revocation — with a receipt for each decision.

Built for the [PayPal AI Hackathon](https://paypalaihackathon.devpost.com/) (Nov 12, 2026).

## The problem

AI agents are about to get wallets. Every major lab is building toward it. What is not yet built, anywhere we can find, is the authority layer: the thing that decides what an agent is *allowed* to spend, on whose terms, with what record. This prototype is one concrete answer to that gap — scoped, testable, and honest about its boundaries (see below).

## What it does

No agent touches raw account access, ever. Each agent spends on a **permit**: an amount cap, a merchant allowlist, an expiry. Every attempt, allowed or blocked, is written to a tamper-evident **claim ledger**. The **e-stop** revokes a permit mid-spend and voids every in-flight authorization it can reach.

The authority check, stated exactly: attempt `a` against permit `P` is authorized iff

```
amount(a) <= remaining(P)
  AND merchant(a) IN allowlist(P)
  AND now < expiry(P)
  AND NOT revoked(P)
```

where `remaining(P) = cap(P) − reserved(P) − captured(P)`. Four clauses. No discretion, no vibes. Amounts are validated at the authority boundary: only positive integers; anything else is blocked and receipted.

Expiry is evaluated twice: at check time and at release time. A release attempted after expiry fails closed — the hold is voided, the reservation freed, no money moves.

## Delegation: permits for teams of agents

An agent holding a permit can carve a **sub-permit** out of its remaining cap for another agent — a buyer delegating to a researcher, a manager to a worker. The child permit cannot exceed the parent's remaining cap, cannot add merchants beyond the parent's allowlist, and cannot outlive the parent's expiry. The carved cap is reserved on the parent, so delegated budget can never be double-spent.

Captures roll up: when the child captures, each ancestor moves reserved→captured by the same amount. Revoking a parent **cascades** — every descendant is revoked, in-flight holds are voided, and unspent carves are released back up the chain. Delegation never increases total spending power; it only subdivides it.

```bash
python3 demo_delegation.py --fast   # two LLM agents, five beats, live dashboard
```

## The spend pipeline

```
check → reserve cap → PayPal AUTHORIZE hold → escrow registered
      → evidence in → predicate evaluated → idempotent capture
```

- **Capture is single-flight and idempotent.** The idempotency key is derived from the permit and claim id (`{permit_id}:{auth_id}:capture`), sent as `PayPal-Request-Id`, so a retried capture after a timeout cannot double-charge.
- **Timeouts go UNKNOWN, never guessed.** If a capture call times out, the escrow is marked UNKNOWN — never optimistically captured or failed. `reconcile()` re-queries PayPal for the authorization's true state and converges the ledger: CAPTURED if the money moved (recorded as truth — a completed capture can't be voided), VOIDED with the reservation freed otherwise. The ledger and PayPal always converge; "always mirrors" is a recovery procedure, not a hope.
- **Failed cleanup is retryable.** If voiding a hold fails, the escrow waits in CLEANUP_PENDING with the reservation held — never silently released — until `retry_cleanup()` confirms the void.
- **Merchant binding.** The sandbox client binds the provider-reported `payee.merchant_id`, currency (CAD), and amount to the authorized operation, and refuses any permit merchant that doesn't match the configured merchant account (`PERMIT_PAYPAL_MERCHANT_ID`). Fail closed on mismatch.
- **E-stop vs in-flight authorization.** The operation is tracked before the external authorize call; if revocation or expiry lands while authorization is outstanding, the hold is voided and the spend dies. Capture admission rechecks revocation and expiry immediately before money moves.

## The approval flow (sandbox)

Each PayPal order needs interactive payer approval:

1. `POST /api/permits/<id>/spend` → authority check reserves the cap, the pipeline creates the PayPal order and tries to authorize it. Before payer approval, the service answers **202** with `{status: approval_required, operation_id, order_id, approval_url}`. The reservation stays held; the operation, order, and reservation are retained server-side.
2. The payer approves at `approval_url` (approval is confirmed by polling the order's API state, never the checkout page).
3. `POST /api/operations/<operation_id>/resume` → authorizes the **same** order (never a second one) and registers the escrow.
4. `POST /api/escrows/<id>/release` with delivery evidence → predicate check → capture.
5. `POST /api/escrows/<id>/reconcile` / `/retry-cleanup` → recovery paths above.

## Architecture

- **PayPal sandbox** as the payment rail. Permit-authorized captures execute as genuine sandbox transactions; blocked attempts never reach PayPal at all.
- **Claim ledger**: an in-memory SHA-256 hash chain (append-only; chain verification before every capture). It is **not** signed and it is **not** durable — those are stated boundaries, not features.
- **Interlock** ([marsojuji-cmyk/interlock](https://github.com/marsojuji-cmyk/interlock), open-source, MIT) is the conceptual origin of the leased-authority model. The ledger dependency was removed; this repo's ledger is standalone.
- An **AI agent** spender operating strictly inside its permit. It can reason, plan, and attempt purchases, but the authority check sits between intent and money. The runner CLI path is configurable via `PERMIT_GROK_CLI` (the demo default points at the author's workspace Grok runner); `--replay` re-runs a saved transcript with no LLM at all.

## Run it

```bash
python -m pytest -q        # full suite (mock rail, no credentials, no network)
python demo.py             # end-to-end mock demo with the receipt chain
python demo_six_beat.py    # six-beat mock demo incl. timeout → UNKNOWN → reconcile
python server.py           # the service: http://127.0.0.1:8741
python trace.py            # drives the live server: allowed flow, blocked
                           # attempt that never touches PayPal, delegate +
                           # cascade revoke, ledger chain verification
```

The six beats (all in `demo_six_beat.py`, mock mode): grant → $30 honest purchase captured → $60 over-cap blocked with PayPal untouched → delegate a $15 sub-permit, child holds $10, revoke the parent cascades (child revoked, hold voided, carve released) → tampered evidence refused with hold voided → dropped capture response goes UNKNOWN and reconciles to the provider truth with exactly one capture.

The service exposes the core verbs as JSON: issue a permit (`POST /api/permits`), delegate a sub-permit (`POST /api/permits/<id>/delegate`), check authority (read-only: no reservation, no receipt), spend, resume an approval (`POST /api/operations/<id>/resume`), release an escrow, reconcile, retry cleanup, e-stop a permit, revoke a permit and its whole subtree (`POST /api/permits/<id>/revoke-cascade`), and read the ledger (`GET /api/ledger`). Mock mode is the default; `--sandbox` arms the real PayPal sandbox rail (needs `PERMIT_PAYPAL_CLIENT_ID` / `PERMIT_PAYPAL_CLIENT_SECRET` and interactive payer approval per order; set `PERMIT_PAYPAL_MERCHANT_ID` to enable merchant binding).

## Which evidence is which

- `demo_six_beat.py` — **mock rail**: deterministic, no network, no credentials. The recorded camera run.
- `spike.py` / `spike-report.md` — **separately recorded sandbox evidence** (Oct 2 spike): real REST calls, order/authorize/capture/void against PayPal sandbox, merchant identity verified.
- `docs/sandbox-runbook.md` — the **scripted procedure** for an integrated six-beat sandbox run (needs credentials + interactive approval). The integrated sandbox run is a procedure to execute, not a recorded artifact yet.

## Production boundaries (stated plainly)

- **All authoritative state is in memory.** A restart loses permits, escrows, revocations, and the ledger.
- **The HTTP service has no caller authentication** (localhost demo boundary).
- **The ledger can't detect removal of final entries** without an external checkpoint.
- **Single-merchant sandbox prototype.** This demonstrates *a* payment authority layer for participating merchants — not a claim about all agent payments.
- **E-stop revokes future execution and attempts void of uncaptured authorizations.** A completed capture needs a refund path, not a void — refunds are out of scope for this prototype.
- Actors in the loop: the **payer** (approves), the **merchant** (delivers), the **Permit operator** (issues permits, holds the e-stop), the **credential owner** (holds the PayPal secret). The agent never holds credentials.

## Status

Building in the open, six weeks to the hackathon deadline. Implemented and tested: the permit core, the 4-clause authority gate with positive-amount validation, the spend pipeline, the release-verifier with timeout recovery (UNKNOWN → reconcile), the PayPal sandbox REST client with merchant binding and idempotent capture, the e-stop path with in-flight authorization handling, the AI agent spender, and the six-beat mock demo. Remaining: the integrated sandbox run (procedure in `docs/sandbox-runbook.md`), the video (record by Nov 8), the Devpost package (submission-ready Nov 10).

## License

MIT. See [LICENSE](LICENSE).
