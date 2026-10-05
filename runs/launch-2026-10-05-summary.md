# Permit PayPal Sandbox Launch — Run Summary
**Date:** 2026-10-05 (MDT) · **Rail:** PayPal Sandbox (REST) · **Mode:** live, `--sandbox`
**Merchant:** GVBH7M3B2KVPW (sb-wysvs53171534@business.example.com)
**Buyer:** sb-pynfz53124146@personal.example.com (John DoeskipKYC)
**Ledger:** `runs/launch-2026-10-05-ledger.json` (34 receipts, hash chain verified intact)

## Beats (all executed against the real sandbox rail)

| # | Beat | Result | Evidence |
|---|------|--------|----------|
| 1 | Sign-in + buyer credentials | Done | Developer dashboard sign-in via saved vault login; email OTP via contact@marcusrichards.dev; merchant ID confirmed GVBH7M3B2KVPW |
| 2 | $50 permit granted, cap confirmed | Done | seq 0 GRANTED, cap_cents 5000 |
| 3 | $30 spend → buyer approval → authorize | Done | seq 8 AUTHORIZED, PayPal auth 6BC05756WN494434V; seq 14 auth 8GN810568S004501P |
| 4 | $60 over-cap attempt | **BLOCKED before touching PayPal** | seq 33 BLOCKED, reason `over_remaining_cap`, escrow_id null |
| 5 | Tampered evidence | **REFUSED** | seq 9 REFUSED, reason `predicate:hash_mismatch`; PayPal hold auto-voided fail-closed |
| 6 | E-stop | Permit revoked, cascade ran | seq 11 E-STOP; VOIDED seq 10; remaining_cents restored to 5000 |
| 7 | Release evidence → capture ($30 CAD) | **CAPTURED on PayPal** | seq 15/16 CAPTURED; PayPal capture 96386534VB484951J status COMPLETED, CAD 30.00 |
| 8 | Capture timeout → UNKNOWN → reconcile | **UNKNOWN then reconciled** | seq 30 UNKNOWN reason `capture_timeout`; seq 31 VOIDED after reconcile found the auth still live and voided fail-closed |

Two further $30 sandbox captures (seq 20/21, 25/26) also completed during timeout-beat rehearsals.

## Bugs found and fixed live

**P0 — `authorize_order` crashed on the real rail (fixed, regression-tested).**
When the payer's browser completes the authorization itself (the normal hermes
flow for `intent=AUTHORIZE`), POST `/v2/checkout/orders/{id}/authorize` answers
`422 ORDER_ALREADY_AUTHORIZED`. The client asserted on 200/201 and crashed the
request thread, orphaning a live $30 hold with no escrow receipt. Fix: recover
idempotently via GET on `ORDER_ALREADY_AUTHORIZED`, and trust-bind against the
GET order response (the POST response omits `payee` on some flows, which caused
a false `MerchantMismatch`). Two regression tests added;
`tests/test_sandbox_client.py` 12/12 pass. Full suite: 165 passed, 1 failed —
the failure is `test_trace_exits_clean`, a pre-existing port collision (see below),
not the change (verified by stashing).

**Port collision (test isolation).** `trace.py` hardcodes port 8741. While the
live server occupied it, the test's own server failed to bind but its wait-loop
saw the live server responding and ran against it, writing 6 `trace_agent`
receipts (seq 2–7) into the launch ledger. Harmless here; `trace.py` should take
a free port.

## Notes
- PayPal's hermes checkout UI loops visually on "Continue to Review Order" in
  the sandbox, but approvals register server-side (confirmed via API every
  time; one attempt surfaced PayPal's own `PAYMENT_ALREADY_DONE` code).
- First email OTP expired in the 10-minute window between delivery and entry;
  the resend flow worked.
- Ledger and permit store are in-memory; a server restart wipes them (known
  limitation, worth a persistence pass before the recorded demo).
- Sandbox authorizations voided after each beat; no live money moved (sandbox only).
