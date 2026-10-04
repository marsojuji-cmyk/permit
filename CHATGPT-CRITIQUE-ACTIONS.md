# ChatGPT outside-witness review — action ledger (2026-10-02 ~15:23 MDT)

Source: ChatGPT reviewed commit 3bf23b3 of marsojuji-cmyk/permit (public, tests green),
ran offline failure reproductions, made no changes. Revised verdict: do not submit
as-is; milestone = reviewed patch closing the five reproduced authority/settlement
failures + completed sandbox approval + one integrated sandbox purchase.

## Status
- A (timeout → UNKNOWN → reconcile, sixth demo beat): DONE 2026-10-02 ~15:50 MDT — 96 tests green, demo_six_beat.py beat 6 verified live (UNKNOWN → reconcile → captured, one capture, chain VERIFIED).
- P1s 1–5 + approval continuation + README reconciliation: DONE 2026-10-02 ~15:55 MDT — regression test per finding (see report below), 96 tests green, pushed to main.
- Integrated sandbox run: DOCUMENTED, not yet executed — procedure at docs/sandbox-runbook.md (needs PERMIT_PAYPAL_CLIENT_ID/SECRET + interactive payer approval; his tap).

## What landed (2026-10-02 ~15:55 MDT, commit on main)
- P1-1 e-stop race: operation tracked BEFORE external authorize (`_outstanding`), post-authorize revocation/expiry recheck voids the hold; capture admission rechecks revocation/expiry; register_escrow fails closed. Tests: tests/test_authority_layer.py:124,162,187,212.
- P1-2 merchant binding: caller side (flow.spend blocks `merchant_not_bound` when paypal.merchant_account_id set) + provider side (sandbox_client._bind_trust: payee.merchant_id + CAD + amount, MerchantMismatch). Tests: test_authority_layer.py:322,339,351; test_sandbox_client.py:80,107,116.
- P1-3 negative amounts: positive-int validation at the authority boundary (check → invalid_amount BLOCKED receipt); read-only eligible() for /check (no reservation, no receipt); HTTP 400 belt-and-braces. Tests: test_authority_layer.py:235,250,260,268,279,291,308.
- P1-4 failed void: CLEANUP_PENDING (reservation held, retryable via retry_cleanup / POST /api/escrows/<id>/retry-cleanup); broken-ledger branch now attempts void. Tests: test_settlement.py:180,211,234; test_server.py retry-cleanup route.
- P1-5 pending capture: PENDING → UNKNOWN, obligation retained; reconcile() converges to provider truth (CAPTURED recorded even if permit dead; otherwise fail-closed void). Tests: test_settlement.py:255; test_reconcile.py:28,52,76,99,135,154.
- P1 service: 202 approval_required + operation registry + POST /api/operations/<id>/resume (same order, reservation held); server passes PERMIT_PAYPAL_MERCHANT_ID through.
- README reconciled: narrowed pitch, hash-chain-not-signed, Interlock-as-origin, mock/sandbox/spike evidence labeled, approval flow documented, production boundaries disclosed, e-stop refund boundary stated. react.py GROK_CLI → PERMIT_GROK_CLI env.

## P1 findings (each needs fix + regression test)
1. E-stop can miss an authorization in progress; capture never rechecks revocation/expiry.
   Reproduced: e-stop between PayPal authorize and escrow registration → release still
   captures $30 on a revoked permit. Fix: track op before external auth; revoke outstanding
   ops; sync capture admission with revocation state; define expiry semantics.
2. Merchant allowlist not bound to actual PayPal payee. sandbox_client copies
   caller-supplied merchant_id into Authorization without comparing payee.merchant_id.
   Fix: bind trusted provider payee + currency + amount; reject permit merchant ≠
   configured merchant account. Test: provider returns ACTUAL_OTHER with ALLOWLISTED label.
3. Negative amounts increase available budget (check() allows −1000, then +6000 on a
   5000 cap). Fix: positive-integer validation at the authority boundary, not just HTTP;
   separate read-only eligibility check from budget reservation.
4. Failed void permanently strands cleanup (REFUSED escrow un-retryable; broken-ledger
   branch never attempts void). Fix: separate "capture forbidden" from "void confirmed";
   keep unresolved cleanup retryable.
5. Pending capture reported as completed spending (verifier ignores provider status).
   Fix: distinguish PENDING/COMPLETED/FAILED; reconcile before declaring success.

## P1 service
- Buyer approval has no HTTP continuation flow: NeedsPayerApproval raised from
  server.py --sandbox has no structured response or resume route. Fix: approval-required
  state + URL, retain op/order/reservation, resume same operation post-approval.

## README / claim reconciliation (witness-verified mismatches)
- Remove "One action; the money stops moving." → narrowed pitch: "Permit puts enforceable
  spending permissions between an AI agent and PayPal: a budget, approved merchants, an
  expiry, and revocation — with a receipt for each decision."
- Ledger is NOT signed: in-memory SHA-256 hash chain. Say exactly that.
- Interlock = conceptual origin of the authority model; ledger dependency was removed.
- Remove "the only existing control is hope" positioning.
- Label mock demo vs sandbox adapter vs separately-recorded sandbox evidence.
- "Agent spender … next" is stale — the agent code exists.
- Document the AI runner dependency (agent/react.py hardcodes /home/hatch path — make
  configurable; no hardcoded local paths in the repo).
- Document the approval flow end-to-end.
- E-stop boundary language: revokes future execution, attempts void of uncaptured
  authorization; completed capture needs a refund path, not a void.

## Production boundaries to disclose
- All authoritative state in memory (restart loses permits/escrows/revocations/ledger).
- HTTP service has no caller auth (localhost demo boundary).
- Ledger can't detect removal of final entries without an external checkpoint.
- Scope: single-merchant sandbox prototype; participating merchants only — not "all
  agent payments". Name the actors: payer, merchant, Permit operator, credential owner,
  buyer-approval step.

## Demo / Devpost
- Build an integrated SANDBOX five-beat run (spike evidence becomes an integrated run,
  not separate history); keep mock clearly labeled if both exist.
- Video storyboard (< 3 min, per first critique): 0:00 permit setup → 0:20 agent does
  real work + $30 sandbox purchase → 0:55 untrusted input triggers blocked over-permit
  attempt → 1:20 revoke during outstanding auth, provider-confirmed void → 1:50 dropped
  provider response, uncertainty + recovery → 2:30 final ledger + supported scope.
- Devpost sections: Inspiration (one specific purchasing failure), What it does
  (incl. unknown outcomes), How we built it (trust boundary, credential ownership,
  durable state, evidence verification), Challenges (one hard race, honestly), What we
  learned, What's next (concrete gaps, no implied capabilities).
- Before/during-hackathon comparison: Permit newly created during submission period
  (Oct 2; period opened Oct 1) — satisfies the "significantly updated/new" rule.
- Prize strategy (rules-verified): Agentic Commerce, PayPal+AI, Most Impactful are all
  Honorable Mentions ($5K); at most ONE Grand + ONE Sponsor OR ONE HM + ONE Sponsor.
  Primary HM target: Best Use of Agentic Commerce; the story also serves the Grand
  Prize criteria (five equally weighted: implementation, design, impact, innovation,
  presentation).

## Test reporting
- Report tests by failure category (policy / provider mocks / sandbox / concurrency /
  crash-recovery), not just a count. Existing protections to keep and name: cumulative
  reserved+captured accounting, per-permit lock, serialized release, capture idempotency key.
