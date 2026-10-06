# CLIMB-SCORE.md — marsojuji-cmyk/permit

**Method:** self-scored (non-independent; scorer is the maintainer's agent).
**Rubric:** CLIMB-RUBRIC-LOCKED-2026-10-05 (patched 2026-10-05 ~10:02 PM MT).
**Scored:** 2026-10-05, America/Edmonton, at main `1704a2c` (post Cursor-setup commit).
**Total: 85/100.** No kill flags. Letter threshold: qualifies **A** (≥70, no kills).

> Canonical SoT path per the rubric is `/workspace/github/sot/permit/CLIMB-SCORE.md`.
> This copy lives in-repo until that tree exists on the maintainer's side.

## Band A — Front door: 23/25

| Item | Score | Evidence |
|---|---|---|
| Why + boundaries/non-claims (0–8) | 7 | README states the problem, the answer, "Production boundaries (stated plainly)" (6 explicit non-claims), and "Which evidence is which". Dock 1: no explicit "who this is NOT for". |
| Stranger install / runnable example (0–8) | 7 | Zero runtime dependencies; `python -m pytest -q`, `python demo.py`, `python demo_six_beat.py`, `python server.py` all run immediately. Dock 1: no explicit clone→run install block. |
| Topics + description match README (0–4) | 4 | Description "Permit: payment authority for AI agents…" matches README H1/lede; topics (agentic-ai, ai-agents, authorization, fintech, payments, python) all match. |
| LICENSE + SECURITY.md present & honest (0–5) | 5 | MIT LICENSE; SECURITY.md disclaims bounty, states out-of-scope plainly, private reporting enabled (verified live 2026-10-05). |

## Band B — Evidence: 25/30

| Item | Score | Evidence |
|---|---|---|
| Stranger command + last-run result on card (0–10) | 5 | Command named (`python -m pytest -q`); CI runs it. Dock 5: no last-run result (pass count) published on the repo card or README. |
| CI mirrors human test command (0–10) | 10 | `.github/workflows/ci.yml` runs exactly `python -m pytest -q` and `python demo.py`. |
| README numbers match code / sum-check (0–10) | 10 | README carries zero numeric claims (no counts, no %). Six-beat dollar figures verified against `demo_six_beat.py`: $50 grant (cap_cents=5000), $30 license, $60 blocked, $15 sub-permit (1500), $10 child hold — all match. **Drift fixed during scoring:** README referenced `spike.py`/`spike-report.md` as present artifacts; they were removed from the tree (per `docs/submission-checklist.md`). Line rewritten to point at `docs/sandbox-runbook.md` API observations. |

## Band C — Honesty: 23/25

| Item | Score | Evidence |
|---|---|---|
| No unpaired yield / multiplier (0–10) | 10 | None exist anywhere in the tree. |
| Claim tiers or equivalent (0–8) | 6 | "Which evidence is which" + stated boundaries function as evidence tiering (mock vs API observations vs procedure). Dock 2: not formal VERIFIED/INFERRED labels. |
| Negative / UNKNOWN stated where true (0–7) | 7 | UNKNOWN escrows, in-memory state loss on restart, no caller auth, refunds out of scope, ledger blind to final-entry removal — all stated. |

## Band D — Product depth: 14/20

| Item | Score | Evidence |
|---|---|---|
| Real module / proof path (0–10) | 9 | Full working system: authority gate, pipeline, escrow recovery, sandbox REST client with merchant binding, e-stop, agent spender, dashboard. Dock 1: integrated sandbox run still a procedure, not a recorded artifact. |
| Second-seat / independent check (0–5) | 4 | 2026-10-05 independent subagent verification: 165 passed / 1 failed (known port-8741 flake), hash chain intact, P0 `ORDER_ALREADY_AUTHORIZED` fix + 2 regression tests. Dock 1: verifier shares the maintainer's context (not a true outsider). |
| Release / tag / freeze story matches git (0–5) | 1 | No releases or tags; no freeze story. |

## Kill flags

None. Checked: invented stars/users/revenue (none), unpaired savings % (none),
dead front-door links (interlock ✓ live, devpost ✓ 200, LICENSE relative ✓),
doc↔code drift (one found and fixed pre-score), secrets in tree
(`.env.sandbox` gitignored, nothing tracked), public score with no stated
method (n/a — method stated here).

## Promotion path

- **A (≥70, no kills): qualifies now** — pending Marc yes + S-room acknowledgment.
- **S (≥88 + Marc yes + S-room bar hold):** two moves get there —
  1. publish the last-run test result on the README/CI card (+4–5 on B1 → ~90);
  2. tag a release/freeze (v0.1.0) matching git (+3–4 on D3 → ~93).
- Non-owner re-score required to drop the `self-scored` label (per rubric).
