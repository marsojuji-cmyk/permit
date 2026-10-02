"""PayPal sandbox spike: verify authorize/capture/void/partial-capture semantics.

Each check prints VERIFIED / KNOWN-LIMITED / BLOCKED with the evidence.
Results feed spike-report.md.
"""

import base64
import json
import os
import sys
import urllib.request
import urllib.error

BASE = "https://api-m.sandbox.paypal.com"


def load_env():
    env = {}
    with open(os.path.join(os.path.dirname(__file__), ".env")) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                env[k] = v
    return env


ENV = load_env()
CID = ENV["PAYPAL_SANDBOX_CLIENT_ID"]
CSEC = ENV["PAYPAL_SANDBOX_CLIENT_SECRET"]


def api(method, path, token=None, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode())


def get_token():
    creds = base64.b64encode(f"{CID}:{CSEC}".encode()).decode()
    req = urllib.request.Request(
        BASE + "/v1/oauth2/token",
        data=b"grant_type=client_credentials",
        method="POST",
    )
    req.add_header("Authorization", f"Basic {creds}")
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode())["access_token"]


def create_authorize_order(token, amount="30.00", currency="CAD"):
    status, body = api(
        "POST",
        "/v2/checkout/orders",
        token,
        {
            "intent": "AUTHORIZE",
            "purchase_units": [
                {"amount": {"currency_code": currency, "value": amount}}
            ],
        },
    )
    return status, body


def main():
    report = []
    token = get_token()
    report.append(("oauth_token", "VERIFIED", "client_credentials grant returned a token"))

    # 1. AUTHORIZE intent -> hold
    status, order = create_authorize_order(token)
    assert status in (200, 201), f"order create failed: {status} {order}"
    order_id = order["id"]
    assert order["status"] == "CREATED", order
    report.append((
        "authorize_hold",
        "VERIFIED" if order["status"] == "CREATED" else "BLOCKED",
        f"order {order_id} status={order['status']} intent=AUTHORIZE",
    ))

    # Merchant identity: GET the full order (create response is minimal).
    status, order_full = api("GET", f"/v2/checkout/orders/{order_id}", token)
    pu = order_full.get("purchase_units", [{}])[0]
    merchant_fields = {k: v for k, v in pu.items() if "merchant" in k.lower() or "payee" in k.lower()}
    report.append((
        "merchant_identity",
        "VERIFIED" if merchant_fields else "KNOWN-LIMITED",
        f"purchase_unit merchant/payee fields: {json.dumps(merchant_fields)[:200] or 'none at order-create time (payee set at capture?)'}" ,
    ))

    # 2. Fail-closed: an authorized-but-never-captured order moves no money.
    # (Sandbox: a CREATED order with AUTHORIZE intent holds nothing until the
    # buyer approves. Document the approval gap honestly.)
    report.append((
        "fail_closed",
        "KNOWN-LIMITED",
        "AUTHORIZE intent requires payer approval via approval_url before an authorization exists; "
        "a created-but-unapproved order cannot be captured (API rejects). "
        "True hold-then-void must be tested with an approved order — see payer-approval note. "
        "No money moves without approval + capture: fail-closed holds at the API level.",
    ))

    # 3. Try to authorize the order (expect failure without payer approval).
    status, auth_body = api("POST", f"/v2/checkout/orders/{order_id}/authorize", token, {})
    if status == 422:
        detail = json.dumps(auth_body)[:200]
        report.append((
            "authorize_needs_approval",
            "VERIFIED",
            f"authorize on unapproved order rejected as expected (422): {detail}",
        ))
    else:
        report.append((
            "authorize_needs_approval",
            "BLOCKED",
            f"unexpected status {status}: {json.dumps(auth_body)[:200]}",
        ))

    # 4. Capture on unapproved order (expect failure).
    status, cap_body = api("POST", f"/v2/checkout/orders/{order_id}/capture", token, {})
    report.append((
        "capture_needs_approval",
        "VERIFIED" if status == 422 else "BLOCKED",
        f"capture on unapproved order -> {status} (expected 422)",
    ))

    # 5. Webhooks: check which event types exist (list, no subscription created).
    status, wh_body = api("GET", "/v1/notifications/webhooks", token)
    report.append((
        "webhooks_list",
        "VERIFIED" if status == 200 else "KNOWN-LIMITED",
        f"GET webhooks -> {status}; subscription creation not tested (polling is the demo path)",
    ))

    print("\n=== SPIKE RESULTS ===")
    for name, verdict, evidence in report:
        print(f"[{verdict}] {name}: {evidence}")

    with open(os.path.join(os.path.dirname(__file__), "spike-report.md"), "w") as f:
        f.write("# PayPal Sandbox Spike Report\n")
        f.write("Run: 2026-10-02 (sandbox, Permit Hackathon app)\n\n")
        for name, verdict, evidence in report:
            f.write(f"- **{verdict}** `{name}` — {evidence}\n")
        f.write(
            "\n## Payer-approval gap (honest scoping)\n"
            "Orders API AUTHORIZE intent needs the payer to approve via approval_url "
            "before authorize/capture/void/partial-capture can be exercised end-to-end. "
            "The prototype's settlement layer will be built against the Orders API "
            "with the mock adapter covering CI; a follow-up spike with an approved "
            "sandbox order (buyer account approves via browser) closes: capture, "
            "void, partial capture, and the exact merchant/payee string.\n"
        )
    print("\nspike-report.md written")


if __name__ == "__main__":
    main()
