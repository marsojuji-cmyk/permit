# PayPal Sandbox Spike Report
Run: 2026-10-02 (sandbox, Permit Hackathon app)

- **VERIFIED** `oauth_token` — client_credentials grant returned a token
- **VERIFIED** `authorize_hold` — order 93P08650EE5009015 status=CREATED intent=AUTHORIZE
- **VERIFIED** `merchant_identity` — purchase_unit merchant/payee fields: {"payee": {"email_address": "sb-wysvs53171534@business.example.com", "merchant_id": "GVBH7M3B2KVPW"}}
- **KNOWN-LIMITED** `fail_closed` — AUTHORIZE intent requires payer approval via approval_url before an authorization exists; a created-but-unapproved order cannot be captured (API rejects). True hold-then-void must be tested with an approved order — see payer-approval note. No money moves without approval + capture: fail-closed holds at the API level.
- **VERIFIED** `authorize_needs_approval` — authorize on unapproved order rejected as expected (422): {"name": "UNPROCESSABLE_ENTITY", "details": [{"issue": "ORDER_NOT_APPROVED", "description": "Payer has not yet approved the Order for payment. Please redirect the payer to the 'rel':'approve' url retu
- **VERIFIED** `capture_needs_approval` — capture on unapproved order -> 422 (expected 422)
- **VERIFIED** `webhooks_list` — GET webhooks -> 200; subscription creation not tested (polling is the demo path)

## Payer-approval gap (honest scoping)
Orders API AUTHORIZE intent needs the payer to approve via approval_url before authorize/capture/void/partial-capture can be exercised end-to-end. The prototype's settlement layer will be built against the Orders API with the mock adapter covering CI; a follow-up spike with an approved sandbox order (buyer account approves via browser) closes: capture, void, partial capture, and the exact merchant/payee string.
