"""
PayPal client interface + mock adapter.

The interface is the contract the release-verifier programs against.
The mock adapter implements the same interface with the same receipt
shapes, so the full demo runs without sandbox credentials — judges and
"run it" verification use mock mode; the recorded video uses sandbox mode.

The real sandbox client (settlement/sandbox_client.py, spike-verified
2026-10-02) implements this same interface against PayPal's REST API
(Orders API, AUTHORIZE intent). The verifier never knows which one it
holds.

Failure semantics (P1 settlement fixes, 2026-10-02):
  - PayPalTimeout: raised on any network timeout. A timed-out call has
    UNKNOWN provider state — the verifier must NEVER optimistically mark
    the escrow captured or failed; it marks UNKNOWN and reconciles
    against provider truth (get_authorization) before doing anything.
  - MerchantMismatch: the provider's payee/amount binding did not match
    the trusted configuration. Fail closed, no hold is usable.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime, timezone
import uuid


class PayPalTimeout(Exception):
    """Network timeout on a PayPal call. Provider state is UNKNOWN."""


class MerchantMismatch(Exception):
    """The provider's payee/amount binding did not match trust."""


@dataclass(frozen=True)
class Authorization:
    auth_id: str
    amount_cents: int
    merchant_id: str
    status: str  # AUTHORIZED | CAPTURED | VOIDED | DENIED | PENDING
    created_at: str


@dataclass(frozen=True)
class Capture:
    capture_id: str
    auth_id: str
    amount_cents: int
    status: str  # COMPLETED | PENDING | FAILED | DENIED
    idempotency_key: str


@dataclass(frozen=True)
class Void:
    auth_id: str
    status: str  # VOIDED


class PayPalClient(ABC):
    """Contract for authorization hold + gated capture."""

    # Trusted, configured merchant account. When set, clients bind the
    # provider's payee to this id before any hold is usable. Subclasses
    # set it in __init__ (mock: None; sandbox: constructor arg).
    merchant_account_id: str | None = None

    @abstractmethod
    def authorize(
        self,
        amount_cents: int,
        merchant_id: str,
        idempotency_key: str | None = None,
    ) -> Authorization:
        ...

    @abstractmethod
    def get_authorization(self, auth_id: str) -> Authorization:
        """
        Provider truth for one authorization. Status may be
        AUTHORIZED / CAPTURED / VOIDED / DENIED / PENDING.
        Raises PayPalTimeout on network timeout.
        """

    @abstractmethod
    def capture(
        self, auth_id: str, amount_cents: int, idempotency_key: str
    ) -> Capture:
        ...

    @abstractmethod
    def void(self, auth_id: str) -> Void:
        ...


class MockPayPalClient(PayPalClient):
    """
    In-memory PayPal stand-in. Same interface, same receipt shapes,
    deterministic ids. No network, no credentials.

    Also records every call, so tests can assert "capture was never
    called" — the negative-test backbone (N1, N2).

    Fault injection (P1 settlement tests + six-beat demo):
      - inject_capture_timeout: None | "before_apply" (raise PayPalTimeout
        before anything is recorded — the request never reached the
        provider) | "after_apply" (record the capture, then raise
        PayPalTimeout — the lost-response case).
      - inject_void_timeout: bool — raise PayPalTimeout instead of
        recording the void.
      - capture_status: the status put on the returned/recorded Capture.
        Tests set "PENDING" to exercise the unknown-capture path.
    """

    def __init__(self, merchant_account_id: str | None = None):
        self.merchant_account_id = merchant_account_id
        self.authorizations: dict[str, Authorization] = {}
        self.captures: dict[str, Capture] = {}  # by idempotency key
        self.voids: dict[str, Void] = {}
        self.capture_calls: list[tuple[str, int, str]] = []
        self.authorize_calls: list[tuple[int, str, str | None]] = []
        self.inject_capture_timeout: str | None = None
        self.inject_void_timeout: bool = False
        self.capture_status: str = "COMPLETED"

    def _now(self) -> str:
        return datetime.now(timezone.utc).isoformat()

    def authorize(
        self,
        amount_cents: int,
        merchant_id: str,
        idempotency_key: str | None = None,
    ) -> Authorization:
        self.authorize_calls.append((amount_cents, merchant_id, idempotency_key))
        auth = Authorization(
            auth_id=f"mock_auth_{uuid.uuid4().hex[:12]}",
            amount_cents=amount_cents,
            merchant_id=merchant_id,
            status="AUTHORIZED",
            created_at=self._now(),
        )
        self.authorizations[auth.auth_id] = auth
        return auth

    def get_authorization(self, auth_id: str) -> Authorization:
        """Provider truth, derived from recorded mock state."""
        auth = self.authorizations.get(auth_id)
        if auth is None:
            raise KeyError(f"unknown auth_id: {auth_id}")
        if auth_id in self.voids:
            status = "VOIDED"
        elif any(c.auth_id == auth_id for c in self.captures.values()):
            status = "CAPTURED"
        else:
            status = "AUTHORIZED"
        return Authorization(
            auth_id=auth.auth_id,
            amount_cents=auth.amount_cents,
            merchant_id=auth.merchant_id,
            status=status,
            created_at=auth.created_at,
        )

    def capture(
        self, auth_id: str, amount_cents: int, idempotency_key: str
    ) -> Capture:
        if self.inject_capture_timeout == "before_apply":
            # The request never reached the provider: nothing recorded.
            raise PayPalTimeout("injected capture timeout before apply")
        self.capture_calls.append((auth_id, amount_cents, idempotency_key))
        # Idempotent: same key returns the same capture, no double charge.
        if idempotency_key in self.captures:
            return self.captures[idempotency_key]
        auth = self.authorizations.get(auth_id)
        assert auth is not None, "unknown auth_id"
        assert auth_id not in self.voids, "cannot capture a voided authorization"
        capture = Capture(
            capture_id=f"mock_cap_{uuid.uuid4().hex[:12]}",
            auth_id=auth_id,
            amount_cents=amount_cents,
            status=self.capture_status,
            idempotency_key=idempotency_key,
        )
        self.captures[idempotency_key] = capture
        if self.inject_capture_timeout == "after_apply":
            # Provider applied the capture; the response was lost.
            raise PayPalTimeout("injected capture timeout after apply")
        return capture

    def void(self, auth_id: str) -> Void:
        if self.inject_void_timeout:
            raise PayPalTimeout("injected void timeout")
        assert auth_id in self.authorizations, "unknown auth_id"
        void = Void(auth_id=auth_id, status="VOIDED")
        self.voids[auth_id] = void
        return void
