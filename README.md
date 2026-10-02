# Permit

**Payment authority for AI agents.** Agents spend on permits: caps, allowlists, expiry. Never raw access. Every attempt on a tamper-evident ledger, with a kill switch. Permits, not trust.

Built for the [PayPal AI Hackathon](https://paypalaihackathon.devpost.com/) (Nov 12, 2026).

## The problem

AI agents are about to get wallets. Every major lab is building toward it. What is not yet built, anywhere we can find, is the authority layer: the thing that decides what an agent is *allowed* to spend, on whose terms, with what record. Between an agent and your money today there is exactly one control: hope. That is not a control.

## What it does

No agent touches raw account access, ever. Each agent spends on a **permit**: amount caps, a merchant allowlist, an expiry. Every attempt, allowed or blocked, is written to a tamper-evident **claim ledger**. The **e-stop** revokes a permit mid-spend. One action; the money stops moving.

The authority check, stated exactly: attempt `a` against permit `P` is authorized iff

```
amount(a) <= cap(P)
  AND merchant(a) IN allowlist(P)
  AND now < expiry(P)
  AND NOT revoked(P)
```

Four clauses. No discretion, no vibes.

## Architecture

- **PayPal sandbox** as the payment rail. Permit-authorized captures execute as genuine sandbox transactions; blocked attempts never reach PayPal at all.
- **Interlock** ([marsojuji-cmyk/interlock](https://github.com/marsojuji-cmyk/interlock), open-source, MIT) for the claim ledger and the leased-authority model. Every permit issuance, payment attempt, and e-stop is a signed ledger claim.
- An **AI agent** spender operating strictly inside its permit. It can reason, plan, and attempt purchases, but the authority check sits between intent and money.

## Run it

```bash
python -m pytest -q        # full suite (mock rail, no credentials, no network)
python demo.py             # end-to-end mock demo with the receipt chain
python server.py           # the service: http://127.0.0.1:8741
python trace.py            # drives the live server: allowed flow, blocked
                           # attempt that never touches PayPal, e-stop void,
                           # ledger chain verification
```

The service exposes the core verbs as JSON: issue a permit
(`POST /api/permits`), check authority, spend, release an escrow,
e-stop a permit, and read the ledger (`GET /api/ledger`). Mock mode is the
default; `--sandbox` arms the real PayPal sandbox rail (needs
`PERMIT_PAYPAL_CLIENT_ID` / `PERMIT_PAYPAL_CLIENT_SECRET` and interactive
payer approval per order).

## Status

Building in the open, six weeks to the hackathon deadline. The permit core,
the 4-clause authority gate, the spend pipeline, the release-verifier, the
PayPal sandbox REST client, and the e-stop path are implemented and tested
here; the agent spender and the recorded video demo are next.

## License

MIT. See [LICENSE](LICENSE).
