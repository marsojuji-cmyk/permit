"""
Permit: the authorization layer. Decides what an agent is ALLOWED to spend.

Authority check (Grok review fix — cumulative reservation):
    amount(a) <= remaining(P)
    AND merchant(a) IN allowlist(P)
    AND now < expiry(P)
    AND NOT revoked(P)

    remaining(P) = cap(P) − reserved(P) − captured(P)

An authorization hold RESERVES cap; capture moves reserved→captured;
void releases the reservation. Parallel attempts serialize on the permit
(per-permit lock), so concurrent attempts cannot exceed the cap (test C1).

A failed check writes a BLOCKED receipt and never reaches PayPal.
This module MUST NOT import any PayPal client (enforced by import test).

E-stop: revoke() flips the permit to revoked and returns the in-flight
authorization ids the settlement layer must void. The e-stop is itself
a receipted event.

Expiry semantics (authority definition — binding on all workstreams):
    Expiry is evaluated at BOTH boundaries, never just one:
    1. Check time — check()/eligible() block any attempt on an expired
       (or missing, or revoked) permit.
    2. Release/capture-admission time — the settlement layer MUST re-admit
       the permit via permits.get() immediately before capture and refuse
       when the permit is missing, revoked, or expired at that moment.
    Release after expiry therefore fails closed: an escrow authorized
    against a then-valid permit cannot capture once the permit has
    expired or been revoked. This module defines the rule; the settlement
    workstream enforces the admission check at capture time.

Amount discipline (P1-3 witness fix): amounts are positive integers.
_validate_amount() guards the authority boundary (check() and grant());
an invalid amount never reserves, never raises through check() — it is
recorded as a BLOCKED receipt with reason "invalid_amount".

Tighten-only min-gate (v4): a live permit can be NARROWED after issuance
via tighten() — lower cap, fewer merchants, sooner expiry — but never
widened. A tighten cascades to every descendant: each child keeps the
narrower of its own authority and the parent's new bound, so the whole
subtree obeys the tightened mandate with no ancestor walk at check time.
Every tighten writes a TIGHTEN receipt with old→new per narrowed clause;
cascaded applications carry cascade_from. A tighten that would widen or
leave authority unchanged is rejected.
"""

from __future__ import annotations

import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone

from .ledger import Ledger, Receipt


class UnknownPermit(Exception):
    """Raised when an operation names a permit id the store does not hold."""


def _validate_amount(amount_cents) -> int:
    """
    Amount guard at the authority boundary: amount_cents must be an int
    (bool excluded) and strictly positive. Raises ValueError otherwise.
    """
    if isinstance(amount_cents, bool) or not isinstance(amount_cents, int):
        raise ValueError(
            "amount_cents must be a positive int, "
            f"got {type(amount_cents).__name__}: {amount_cents!r}"
        )
    if amount_cents <= 0:
        raise ValueError(f"amount_cents must be > 0, got {amount_cents}")
    return amount_cents


@dataclass
class CheckResult:
    allowed: bool
    reason: str
    # None for read-only evaluations (eligible()): no receipt is written.
    receipt: Receipt | None = None


@dataclass
class DelegateResult:
    ok: bool
    reason: str
    permit: Permit | None = None
    receipt: Receipt | None = None


@dataclass
class Permit:
    permit_id: str
    agent_id: str
    cap_cents: int
    allowlist: tuple[str, ...]
    expiry: datetime
    revoked: bool = False
    reserved_cents: int = 0
    captured_cents: int = 0
    # Authorization ids currently holding a reservation on this permit.
    in_flight: dict[str, int] = field(default_factory=dict)
    # Delegation: set when this permit was carved out of a parent permit.
    parent_id: str | None = None
    # Tighten overlays (v4 min-gate): post-issuance narrowings. Each is
    # None until the first tighten touches that clause; tighten() only
    # ever narrows, so the effective clause is the minimum/intersection
    # of the granted value and every tighten overlay in the lineage.
    tighten_cap_cents: int | None = None
    tighten_allowlist: tuple[str, ...] | None = None
    tighten_expiry: datetime | None = None
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def remaining_cents(self) -> int:
        return self.cap_cents - self.reserved_cents - self.captured_cents


class PermitStore:
    """Issues permits, evaluates spend attempts, handles e-stop."""

    def __init__(self, ledger: Ledger | None = None):
        # NOTE: explicit None check — an empty Ledger is falsy via __len__,
        # so `ledger or Ledger()` would silently discard a passed empty ledger.
        self.ledger = ledger if ledger is not None else Ledger()
        self._permits: dict[str, Permit] = {}
        # Delegation index: parent permit_id -> child permit_ids.
        self._children: dict[str, list[str]] = {}
        self._store_lock = threading.Lock()

    def grant(
        self,
        agent_id: str,
        cap_cents: int,
        allowlist: list[str],
        expiry: datetime,
    ) -> tuple[Permit, Receipt]:
        _validate_amount(cap_cents)
        permit = Permit(
            permit_id=f"prm_{uuid.uuid4().hex[:12]}",
            agent_id=agent_id,
            cap_cents=cap_cents,
            allowlist=tuple(allowlist),
            expiry=expiry,
        )
        with self._store_lock:
            self._permits[permit.permit_id] = permit
        receipt = self.ledger.append(
            "GRANTED",
            {
                "permit_id": permit.permit_id,
                "agent_id": agent_id,
                "cap_cents": cap_cents,
                "allowlist": list(allowlist),
                "expiry": expiry.isoformat(),
            },
        )
        return permit, receipt

    def get(self, permit_id: str) -> Permit | None:
        with self._store_lock:
            return self._permits.get(permit_id)

    def children_of(self, permit_id: str) -> list[str]:
        """Child permit ids delegated from this permit (a copy)."""
        with self._store_lock:
            return list(self._children.get(permit_id, []))

    def delegate(
        self,
        parent_permit_id: str,
        agent_id: str,
        cap_cents: int,
        allowlist: list[str],
        expiry: datetime,
        now: datetime | None = None,
    ) -> DelegateResult:
        """
        Carve a sub-permit out of a parent permit's remaining cap.

        Constraints — each violation writes a BLOCKED receipt:
          - parent exists, unrevoked, unexpired
          - cap_cents <= parent remaining (can't delegate what isn't free)
          - allowlist ⊆ parent allowlist (no merchant escalation)
          - expiry <= parent expiry (can't outlive the parent)

        The carved cap is RESERVED on the parent, so the parent can never
        double-spend delegated budget. A child capture rolls up: each
        ancestor moves reserved→captured by the captured amount.
        """
        now = now or datetime.now(timezone.utc)
        try:
            _validate_amount(cap_cents)
        except ValueError:
            receipt = self.ledger.append(
                "BLOCKED",
                {
                    "permit_id": parent_permit_id,
                    "agent_id": agent_id,
                    "amount_cents": repr(cap_cents),
                    "reason": "invalid_amount",
                },
            )
            return DelegateResult(False, "invalid_amount", None, receipt)

        parent = self.get(parent_permit_id)
        if parent is None:
            receipt = self.ledger.append(
                "BLOCKED",
                {"permit_id": parent_permit_id, "reason": "unknown_parent"},
            )
            return DelegateResult(False, "unknown_parent", None, receipt)

        child_allowlist = tuple(allowlist)
        # Lock order: store -> parent. No path nests permit -> store,
        # so this ordering cannot deadlock.
        with self._store_lock:
            with parent._lock:
                # Carve against the parent's EFFECTIVE authority (tighten
                # overlays included): a tightened parent cannot delegate
                # what it can no longer spend.
                eff_cap, _, eff_allow, eff_expiry, _ = self._effective(parent)
                eff_remaining = eff_cap - parent.reserved_cents - parent.captured_cents
                if parent.revoked:
                    reason = "parent_revoked"
                elif now >= eff_expiry:
                    reason = "parent_expired"
                elif cap_cents > eff_remaining:
                    reason = "over_parent_remaining"
                elif not set(child_allowlist) <= set(eff_allow):
                    reason = "allowlist_escalation"
                elif expiry > eff_expiry:
                    reason = "expiry_beyond_parent"
                else:
                    reason = None
                if reason is not None:
                    receipt = self.ledger.append(
                        "BLOCKED",
                        {
                            "permit_id": parent.permit_id,
                            "agent_id": agent_id,
                            "amount_cents": cap_cents,
                            "reason": reason,
                            "parent_remaining_cents": parent.remaining_cents(),
                        },
                    )
                    return DelegateResult(False, reason, None, receipt)
                # Carve: the child's cap is reserved on the parent.
                parent.reserved_cents += cap_cents
                child = Permit(
                    permit_id=f"prm_{uuid.uuid4().hex[:12]}",
                    agent_id=agent_id,
                    cap_cents=cap_cents,
                    allowlist=child_allowlist,
                    expiry=expiry,
                    parent_id=parent.permit_id,
                )
                self._permits[child.permit_id] = child
                self._children.setdefault(parent.permit_id, []).append(
                    child.permit_id
                )
                receipt = self.ledger.append(
                    "DELEGATED",
                    {
                        "parent_permit_id": parent.permit_id,
                        "child_permit_id": child.permit_id,
                        "agent_id": agent_id,
                        "cap_cents": cap_cents,
                        "allowlist": list(child_allowlist),
                        "expiry": expiry.isoformat(),
                        "parent_remaining_cents": parent.remaining_cents(),
                    },
                )
                return DelegateResult(True, "delegated", child, receipt)

    def tighten(
        self,
        permit_id: str,
        *,
        cap_cents: int | None = None,
        remove_merchants: list[str] | None = None,
        expiry: datetime | None = None,
        actor: str = "human",
    ) -> tuple[Permit, Receipt]:
        """
        Narrow a live permit. Tighten-only: every supplied parameter must
        strictly narrow the permit's current effective authority, otherwise
        ValueError. At least one parameter is required.

        - cap_cents: new cap, a positive int strictly below the current
          effective cap. May go below reserved+captured (emergency):
          in-flight holds stand, but no new attempt can reserve.
        - remove_merchants: non-empty list; each must be in the current
          effective allowlist; the result must stay non-empty (to kill
          the permit, use e-stop).
        - expiry: timezone-aware datetime, strictly sooner than the current
          effective expiry and still in the future.
        - actor: who ordered the tighten (recorded on the receipt).

        Cannot tighten a revoked permit or one whose effective window has
        passed — fail closed. Writes a TIGHTEN receipt with old→new for
        every narrowed clause, then cascades to every descendant: each
        child keeps the narrower of its own authority and the new bound
        (cascade_from on the receipt). Tighten-only holds at every level —
        a descendant already narrower than the new bound is untouched.
        """
        now = datetime.now(timezone.utc)
        if cap_cents is None and not remove_merchants and expiry is None:
            raise ValueError(
                "tighten requires at least one of cap_cents, "
                "remove_merchants, expiry"
            )
        if not isinstance(actor, str) or not actor.strip():
            raise ValueError("actor must be a non-empty string")
        actor = actor.strip()

        # Validate the requested narrowing once, up front.
        if cap_cents is not None:
            _validate_amount(cap_cents)
        if remove_merchants is not None and (
            not isinstance(remove_merchants, list)
            or not remove_merchants
            or not all(isinstance(m, str) and m.strip() for m in remove_merchants)
        ):
            raise ValueError(
                "remove_merchants must be a non-empty list of merchant id strings"
            )
        rm = {m.strip() for m in remove_merchants} if remove_merchants else set()
        if expiry is not None:
            if expiry.tzinfo is None:
                raise ValueError("expiry must be timezone-aware")
            if expiry <= now:
                raise ValueError("tighten expiry must be in the future")

        permit = self.get(permit_id)
        if permit is None:
            raise UnknownPermit(permit_id)

        # Apply to the target, then cascade breadth-first (parents before
        # children). Each permit is locked individually while updated — no
        # nested locking, so no new lock order is introduced.
        queue: list[tuple[str, str | None]] = [(permit_id, None)]
        seen = {permit_id}
        first_receipt: Receipt | None = None
        while queue:
            pid, cascade_from = queue.pop(0)
            target = self.get(pid)
            if target is None:
                continue
            with target._lock:
                if target.revoked:
                    continue
                eff_cap, _, eff_allow, eff_expiry, _ = self._effective(target)
                if now >= eff_expiry:
                    continue
                changes: dict[str, dict] = {}
                if cap_cents is not None and cap_cents < eff_cap:
                    changes["cap_cents"] = {"from": eff_cap, "to": cap_cents}
                if rm:
                    if rm <= set(eff_allow):
                        new_allow = set(eff_allow) - rm
                        if new_allow:
                            changes["allowlist"] = {
                                "from": sorted(eff_allow),
                                "to": sorted(new_allow),
                                "removed": sorted(rm),
                            }
                if expiry is not None and expiry < eff_expiry:
                    changes["expiry"] = {
                        "from": eff_expiry.isoformat(),
                        "to": expiry.isoformat(),
                    }
                if not changes:
                    # Already narrower than the new bound (tighten-only):
                    # untouched, but the cascade still flows below.
                    pass
                else:
                    if "cap_cents" in changes:
                        target.tighten_cap_cents = cap_cents
                    if "allowlist" in changes:
                        base = (
                            set(target.tighten_allowlist)
                            if target.tighten_allowlist is not None
                            else set(target.allowlist)
                        )
                        target.tighten_allowlist = tuple(sorted(base - rm))
                    if "expiry" in changes:
                        target.tighten_expiry = expiry
                    payload: dict = {
                        "permit_id": target.permit_id,
                        "agent_id": target.agent_id,
                        "actor": actor,
                        "changes": changes,
                    }
                    if cascade_from is not None:
                        payload["cascade_from"] = cascade_from
                    receipt = self.ledger.append("TIGHTEN", payload)
                    if first_receipt is None:
                        first_receipt = receipt
            # Queue children regardless of whether this level narrowed —
            # a grandchild may still be wider than the new bound.
            with self._store_lock:
                kids = list(self._children.get(pid, []))
            for cid in kids:
                if cid not in seen:
                    seen.add(cid)
                    queue.append((cid, pid))
        if first_receipt is None:
            # The target itself was already narrower than every requested
            # bound: tighten-only rejects the no-op.
            raise ValueError(
                "tighten changes nothing: permit is already at or below "
                "every requested bound (tighten-only)"
            )
        return permit, first_receipt

    @staticmethod
    def _effective(
        permit: Permit,
    ) -> tuple[int, bool, frozenset[str], datetime, bool]:
        """
        Per-permit min-gate: the narrowest of the granted clauses and the
        tighten overlays. Returns (cap_cents, cap_tightened, allowlist,
        expiry, expiry_tightened). Ancestor tightens arrive via cascade at
        tighten() time, so no lineage walk is needed at check time.
        Caller must hold permit._lock.
        """
        cap = permit.cap_cents
        cap_tightened = False
        if permit.tighten_cap_cents is not None and permit.tighten_cap_cents < cap:
            cap = permit.tighten_cap_cents
            cap_tightened = True
        allow = set(permit.allowlist)
        if permit.tighten_allowlist is not None:
            allow &= set(permit.tighten_allowlist)
        expiry = permit.expiry
        expiry_tightened = False
        if permit.tighten_expiry is not None and permit.tighten_expiry < expiry:
            expiry = permit.tighten_expiry
            expiry_tightened = True
        return cap, cap_tightened, frozenset(allow), expiry, expiry_tightened

    @staticmethod
    def _evaluate(permit: Permit, amount_cents: int, merchant_id: str, now: datetime) -> str | None:
        """
        The 4-clause authority evaluation through the tighten-only min-gate.
        Returns the block reason, or None when the attempt is allowed.
        Caller must hold permit._lock. Amount is assumed pre-validated by
        _validate_amount.
        """
        if permit.revoked:
            return "revoked"
        cap, cap_tightened, allow, expiry, expiry_tightened = (
            PermitStore._effective(permit)
        )
        if now >= expiry:
            return "tightened_expiry_passed" if expiry_tightened else "expired"
        if merchant_id not in allow:
            return "merchant_not_allowed"
        if amount_cents > cap - permit.reserved_cents - permit.captured_cents:
            return "tightened_cap_exceeded" if cap_tightened else "over_remaining_cap"
        return None

    def eligible(
        self,
        permit_id: str,
        amount_cents: int,
        merchant_id: str,
        now: datetime | None = None,
    ) -> CheckResult:
        """
        READ-ONLY authority evaluation: the same 4-clause logic as check()
        plus amount validation, but it reserves nothing and writes no
        receipt. The server's /check route uses this. receipt is None.
        """
        now = now or datetime.now(timezone.utc)
        try:
            _validate_amount(amount_cents)
        except ValueError:
            return CheckResult(False, "invalid_amount")

        permit = self.get(permit_id)
        if permit is None:
            return CheckResult(False, "unknown_permit")

        with permit._lock:
            reason = self._evaluate(permit, amount_cents, merchant_id, now)
        if reason is not None:
            return CheckResult(False, reason)
        return CheckResult(True, "allowed")

    def check(
        self,
        permit_id: str,
        amount_cents: int,
        merchant_id: str,
        now: datetime | None = None,
    ) -> CheckResult:
        """
        Evaluate a spend attempt: validate → eligible() → reserve + ALLOWED
        receipt. On BLOCKED, writes a BLOCKED receipt and returns — PayPal
        is never touched. Invalid amounts do NOT raise: they are recorded
        as BLOCKED receipts with reason "invalid_amount".
        """
        now = now or datetime.now(timezone.utc)
        try:
            _validate_amount(amount_cents)
        except ValueError:
            receipt = self.ledger.append(
                "BLOCKED",
                {
                    "permit_id": permit_id,
                    "merchant_id": merchant_id,
                    "amount_cents": (
                        amount_cents
                        if isinstance(amount_cents, int)
                        and not isinstance(amount_cents, bool)
                        else repr(amount_cents)
                    ),
                    "reason": "invalid_amount",
                },
            )
            return CheckResult(False, "invalid_amount", receipt)

        permit = self.get(permit_id)
        if permit is None:
            receipt = self.ledger.append(
                "BLOCKED",
                {"permit_id": permit_id, "reason": "unknown_permit"},
            )
            return CheckResult(False, "unknown_permit", receipt)

        # Read-only authority evaluation first (no reservation, no receipt).
        # This is the shared logic the server's /check route also uses.
        probe = self.eligible(permit_id, amount_cents, merchant_id, now=now)

        with permit._lock:
            # Authoritative re-evaluation under the per-permit lock: the
            # probe is read-only, so the lock serializes reservation
            # against concurrent attempts (C1) and against e-stop. The
            # lock's view wins over the probe's (reservations may have
            # been released between the two).
            reason = self._evaluate(permit, amount_cents, merchant_id, now)
            if reason is not None:
                receipt = self.ledger.append(
                    "BLOCKED",
                    {
                        "permit_id": permit.permit_id,
                        "agent_id": permit.agent_id,
                        "amount_cents": amount_cents,
                        "merchant_id": merchant_id,
                        "reason": reason,
                        "remaining_cents": permit.remaining_cents(),
                    },
                )
                return CheckResult(False, reason, receipt)

            # ALLOWED — reserve the amount against the cap.
            permit.reserved_cents += amount_cents
            auth_id = f"auth_{uuid.uuid4().hex[:12]}"
            permit.in_flight[auth_id] = amount_cents
            receipt = self.ledger.append(
                "ALLOWED",
                {
                    "permit_id": permit.permit_id,
                    "agent_id": permit.agent_id,
                    "amount_cents": amount_cents,
                    "merchant_id": merchant_id,
                    "auth_id": auth_id,
                    "remaining_cents": permit.remaining_cents(),
                },
            )
            return CheckResult(True, "allowed", receipt)

    def _rollup_capture(self, permit: Permit, amount_cents: int) -> None:
        """
        Walk the delegation chain: each ancestor moves reserved→captured
        by the captured amount. The delegation carve already encumbers the
        parent, so only captures (real money out) move the parent's books —
        child reserves and voids stay within the carve.
        """
        pid = permit.parent_id
        while pid is not None:
            parent = self.get(pid)
            if parent is None:
                break
            with parent._lock:
                parent.reserved_cents -= amount_cents
                parent.captured_cents += amount_cents
                pid = parent.parent_id

    def settle_capture(self, permit_id: str, auth_id: str) -> Receipt:
        """Move a reservation to captured (called by the settlement layer)."""
        permit = self.get(permit_id)
        assert permit is not None, "unknown permit"
        with permit._lock:
            amount = permit.in_flight.pop(auth_id, None)
            assert amount is not None, "unknown auth_id"
            permit.reserved_cents -= amount
            permit.captured_cents += amount
            receipt = self.ledger.append(
                "CAPTURED",
                {
                    "permit_id": permit_id,
                    "auth_id": auth_id,
                    "amount_cents": amount,
                    "remaining_cents": permit.remaining_cents(),
                },
            )
        # Roll the capture up the delegation chain (outside the child lock:
        # lock order is always ancestor-after-descendant via get()).
        self._rollup_capture(permit, amount)
        return receipt

    def settle_void(self, permit_id: str, auth_id: str) -> Receipt:
        """Release a reservation (called by the settlement layer on void)."""
        permit = self.get(permit_id)
        assert permit is not None, "unknown permit"
        with permit._lock:
            amount = permit.in_flight.pop(auth_id, None)
            assert amount is not None, "unknown auth_id"
            permit.reserved_cents -= amount
            return self.ledger.append(
                "VOIDED",
                {
                    "permit_id": permit_id,
                    "auth_id": auth_id,
                    "amount_cents": amount,
                    "remaining_cents": permit.remaining_cents(),
                },
            )

    def estop(self, permit_id: str) -> tuple[Receipt, list[str]]:
        """
        Emergency stop: revoke the permit immediately. Returns the e-stop
        receipt and the in-flight authorization ids the settlement layer
        must void. No further check() can pass after this.
        """
        permit = self.get(permit_id)
        assert permit is not None, "unknown permit"
        with permit._lock:
            permit.revoked = True
            in_flight = list(permit.in_flight.keys())
            receipt = self.ledger.append(
                "E-STOP",
                {
                    "permit_id": permit_id,
                    "agent_id": permit.agent_id,
                    "in_flight_auth_ids": in_flight,
                    "reserved_cents_released": permit.reserved_cents,
                },
            )
            return receipt, in_flight

    def revoke_subtree(
        self, permit_id: str
    ) -> tuple[Receipt, dict[str, list[str]]]:
        """
        Revoke a permit and every descendant permit (cascade). Returns the
        root receipt and {permit_id: [in-flight auth_ids]} for the whole
        subtree, so the settlement layer can void every outstanding hold.

        Unspent delegation carves are NOT released here — call
        release_carve() per revoked child after in-flight holds are voided
        (post-order: children before parents).

        Concurrency note: delegate() and revoke_subtree() are each atomic,
        but a delegate() racing revoke_subtree() may create a child after
        the subtree was collected. Callers must serialize permit-graph
        mutation (delegate vs revoke); spend-path races against revocation
        are closed by the per-permit lock + capture-time re-admission.
        """
        root = self.get(permit_id)
        assert root is not None, "unknown permit"
        # Collect the subtree (parents before children) under the store lock.
        with self._store_lock:
            order = [permit_id]
            queue = [permit_id]
            while queue:
                pid = queue.pop(0)
                for cid in self._children.get(pid, []):
                    order.append(cid)
                    queue.append(cid)
        in_flight: dict[str, list[str]] = {}
        root_receipt: Receipt | None = None
        for pid in order:
            permit = self.get(pid)
            if permit is None:
                continue
            with permit._lock:
                ids = list(permit.in_flight.keys())
                if ids:
                    in_flight[pid] = ids
                if permit.revoked:
                    continue
                permit.revoked = True
                event = "E-STOP" if pid == permit_id else "REVOKED_CASCADE"
                receipt = self.ledger.append(
                    event,
                    {
                        "permit_id": pid,
                        "agent_id": permit.agent_id,
                        "in_flight_auth_ids": ids,
                        "parent_id": permit.parent_id,
                    },
                )
                if pid == permit_id:
                    root_receipt = receipt
        assert root_receipt is not None, "root permit vanished"
        return root_receipt, in_flight

    def release_carve(self, permit_id: str) -> Receipt | None:
        """
        Release a revoked child's unspent delegation carve back to its
        parent. Call after in-flight holds are voided (so reserved reflects
        only the carve), post-order: children before parents. Returns the
        CARVE_RELEASED receipt, or None when there is nothing to release.
        """
        permit = self.get(permit_id)
        if permit is None or permit.parent_id is None:
            return None
        parent = self.get(permit.parent_id)
        if parent is None:
            return None
        # Lock order: parent before child, consistent with delegate().
        with parent._lock:
            with permit._lock:
                if not permit.revoked:
                    return None
                if permit.parent_id is None:
                    # Already released (idempotent).
                    return None
                unspent = (
                    permit.cap_cents
                    - permit.captured_cents
                    - permit.reserved_cents
                )
                assert unspent >= 0, "delegation carve accounting went negative"
                parent.reserved_cents -= unspent
                parent_remaining = parent.remaining_cents()
                parent_id = permit.parent_id
                # Clear the link: a second call is a no-op.
                permit.parent_id = None
        with self._store_lock:
            kids = self._children.get(parent_id, [])
            if permit_id in kids:
                kids.remove(permit_id)
        return self.ledger.append(
            "CARVE_RELEASED",
            {
                "child_permit_id": permit_id,
                "parent_permit_id": parent_id,
                "released_cents": unspent,
                "parent_remaining_cents": parent_remaining,
            },
        )
