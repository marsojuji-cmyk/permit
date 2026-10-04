# Permit

**Payment authority for AI agents.** Agents spend on permits, never on raw account access.

[![Tests](https://img.shields.io/badge/tests-98%20passing-brightgreen)](https://github.com/marsojuji-cmyk/permit/actions)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Python 3.9+](https://img.shields.io/badge/python-3.9+-blue.svg)](https://www.python.org/downloads/)
[![Demo](https://img.shields.io/badge/demo-no%20credentials%20needed-brightgreen)](#quick-start)

---

## What it is

Every AI agent that spends money needs an authority layer: the thing that decides what it's *allowed* to spend, on whose terms, with what record. Permit is that layer.

An agent never touches raw account access. It spends on a **permit** — a budget cap, a merchant allowlist, an expiry. Every attempt, allowed or blocked, is written to a tamper-evidary **claim ledger**. An **e-stop** revokes a permit mid-spend and voids every in-flight authorization it can reach.

```
┌─────────┐    ┌─────────┐    ┌─────────┐    ┌─────────┐
│  Agent  │───▶│ Permit  │───▶│  Check  │───▶│ PayPal  │
│  wants  │    │ authority│    │ 4 clauses│    │ sandbox │
│ to buy  │    │ layer    │    │ + ledger │    │  hold   │
└─────────┘    └─────────┘    └─────────┘    └─────────┘
```

---

## ⚡ Quick Start (no credentials needed)

```bash
git clone https://github.com/marsojuji-cmyk/permit
cd permit
python demo.py
```

You'll see a complete agent transaction with 12 receipts — every decision, every payment, every audit trail.

**What just happened?**

| Receipt | What it means |
|---|---|
| `permit.created` | A spending permit was issued: $50 budget, Amazon only, 24h expiry |
| `check.attempt` | Agent tried to buy something — Permit checked the 4 clauses |
| `check.allowed` | Budget OK, merchant OK, not expired, not revoked → allowed |
| `escrow.reserved` | Money held on the permit (not yet captured) |
| `evidence.submitted` | Agent submitted proof of delivery |
| `predicate.passed` | Delivery evidence verified against permit rules |
| `capture.success` | Payment captured — money moved |
| `ledger.sealed` | Receipt chain hashed and sealed |

The full demo runs 12 receipts. No network, no credentials, no PayPal calls.

---

## The authority check

The gate, stated exactly: attempt `a` against permit `P` is authorized iff

```
amount(a) <= remaining(P)
  AND merchant(a) IN allowlist(P)
  AND now < expiry(P)
  AND NOT revoked(P)
```

where `remaining(P) = cap(P) − reserved(P) − captured(P)`.

Four clauses. No discretion, no vibes. Amounts are validated at the boundary: only positive integers; anything else is blocked and receipted.

---

## The spend pipeline

```
check → reserve cap → PayPal AUTHORIZE hold → escrow registered
      → evidence in → predicate evaluated → idempotent capture
```

**Key properties:**

- **Single-flight capture.** Idempotency key derived from permit + claim ID. Retried captures after timeout cannot double-charge.
- **Timeouts go UNKNOWN, never guessed.** If capture times out, escrow is marked UNKNOWN. `reconcile()` re-queries PayPal for truth: CAPTURED if money moved, VOIDED otherwise. Ledger and PayPal always converge.
- **Failed cleanup is retryable.** If voiding a hold fails, escrow waits in CLEANUP_PENDING with reservation held — never silently released.
- **Merchant binding.** Sandbox client binds provider-reported merchant ID, currency, and amount. Refuses mismatched merchants. Fail closed.
- **E-stop vs in-flight.** If revocation lands while authorization is outstanding, hold is voided and spend dies. Capture rechecks revocation immediately before money moves.

---

## Run it

```bash
python demo.py             # end-to-end mock demo with receipt chain
python demo_six_beat.py    # six-beat demo: timeout → UNKNOWN → reconcile
python -m pytest -q        # full suite (mock rail, no credentials, no network)
python server.py           # service at http://127.0.0.1:8741
python trace.py            # drives live server: allowed, blocked, e-stop, ledger
```

**The six beats** (all mock mode, no network):

1. Grant permit → $30 honest purchase captured
2. $60 over-cap blocked → PayPal untouched
3. E-stop voids mid-hold authorization
4. Tampered evidence refused → hold voided
5. Dropped capture → UNKNOWN → reconciles to provider truth
6. Exactly one capture, exactly one receipt chain

---

## Architecture

| Component | Role |
|---|---|
| **Permit authority** | 4-clause gate between agent intent and money |
| **Claim ledger** | SHA-256 hash chain, append-only, chain-verified before capture |
| **PayPal sandbox** | Real REST transactions; blocked attempts never reach PayPal |
| **E-stop** | Revokes future execution + voids uncaptured authorizations |
| **Agent spender** | Operates strictly inside permit boundaries |

**Interlock** ([marsojuji-cmyk/interlock](https://github.com/marsojuji-cmyk/interlock)) is the conceptual origin of the leased-authority model. This repo's ledger is standalone.

---

## Production boundaries (stated plainly)

- **All state is in memory.** Restart loses permits, escrows, ledger.
- **No caller authentication.** Localhost demo boundary.
- **Single-merchant prototype.** Demonstrates *a* payment authority layer — not all agent payments.
- **E-stop voids uncaptured authorizations.** Completed captures need refunds (out of scope).
- **Actors in the loop:** payer (approves), merchant (delivers), Permit operator (issues permits, holds e-stop), credential owner (holds PayPal secret). Agent never holds credentials.

---

## Status

Building in the open. Implemented and tested:
- Permit core with 4-clause authority gate
- Spend pipeline with timeout recovery (UNKNOWN → reconcile)
- PayPal sandbox REST client with merchant binding
- E-stop with in-flight authorization handling
- AI agent spender with transcript replay
- 98 tests passing

**Remaining:** integrated sandbox run (procedure in `docs/sandbox-runbook.md`), video (by Nov 8), Devpost submission (Nov 10).

---

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md). Issues and PRs welcome — I review everything within 24 hours.

## License

MIT. See [LICENSE](LICENSE).

---

Built by Marcus Richards.
