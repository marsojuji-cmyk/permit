"""
Calibration score (Sol 6.1) — ADVISORY signal, never a gate.

Adversarial cases first. Fixtures (not calibrated values):
    x* = (1.0, 24.0, 1.0)   w = (0.5, 0.25, 0.25)   s = (0.5, 24.0, 0.25)
With these, 60% of cap remaining gives R = 0.5 * 0.4 / 0.5 = 0.4 and
S = exp(-0.4) = 0.6703 < 0.70.
"""
import math

import pytest

from permit.calibration import (
    BELOW,
    FIXTURE_SCALES,
    FIXTURE_SPEC_X,
    FIXTURE_WEIGHTS,
    MEETS,
    N_MIN,
    TAU_DEFAULT,
    advisory_verdict,
    features,
    score,
    validate_tau,
)

SPEC = FIXTURE_SPEC_X
W = FIXTURE_WEIGHTS
SC = FIXTURE_SCALES


def S(x=SPEC, n=N_MIN, chain_ok=True, w=W, sc=SC, spec=SPEC):
    return score(x, spec, w, sc, n, chain_ok)


# -- adversarial: confidence collapses -------------------------------------

@pytest.mark.parametrize("n", [0, 1, 49, 50, 51, 10_000])
@pytest.mark.parametrize("x", [SPEC, (0.0, 0.0, 0.0), (0.6, 24.0, 1.0), (1e6, -1e6, 1e6)])
def test_tampered_chain_forces_score_zero_for_any_residual_and_count(x, n):
    assert S(x=x, n=n, chain_ok=False) == 0.0


def test_empty_ledger_scores_zero_even_at_spec():
    assert S(n=0) == 0.0


def test_sixty_percent_of_cap_remaining_scores_below_default_tau():
    s = S(x=(0.6, 24.0, 1.0), n=N_MIN)
    assert s == pytest.approx(math.exp(-0.4))
    assert s == pytest.approx(0.6703, abs=1e-4)
    assert s < TAU_DEFAULT
    assert advisory_verdict(s, TAU_DEFAULT) == BELOW


def test_perfect_permit_scores_exactly_one():
    assert S(x=SPEC, n=N_MIN) == 1.0
    assert S(x=SPEC, n=10_000) == 1.0
    assert advisory_verdict(1.0, TAU_DEFAULT) == MEETS


def test_g1_absolute_residual_penalises_expiry_margin_above_spec():
    """Gap G1 (open question): with the absolute residual as specified,
    MORE expiry margin than spec lowers S. 72h vs 24h: R = 0.25*48/24 = 0.5."""
    s = S(x=(1.0, 72.0, 1.0), n=N_MIN)
    assert s == pytest.approx(math.exp(-0.5))
    assert s == pytest.approx(0.6065, abs=1e-4)
    assert s < TAU_DEFAULT


def test_score_exactly_at_tau_meets():
    assert advisory_verdict(0.70, 0.70) == MEETS
    assert advisory_verdict(math.nextafter(0.70, 0.0), 0.70) == BELOW


# -- invariants ---------------------------------------------------------------

def test_idempotent_identical_inputs_identical_scores():
    for x in (SPEC, (0.6, 24.0, 1.0), (0.3, 5.0, 0.5)):
        for n in (0, 10, 50):
            first = S(x=x, n=n)
            assert all(S(x=x, n=n) == first for _ in range(100))


def test_confidence_monotonic_more_receipts_never_lowers_score():
    for x in (SPEC, (0.6, 24.0, 1.0)):
        scores = [S(x=x, n=n) for n in range(0, 101)]
        assert all(a <= b for a, b in zip(scores, scores[1:]))
    assert S(n=10) == pytest.approx(0.2)
    assert S(n=100) == 1.0


def test_residual_monotonic_less_remaining_never_raises_score():
    for n in (1, 25, 50):
        scores = [S(x=(i / 100, 24.0, 1.0), n=n) for i in range(100, -1, -1)]
        assert all(a >= b for a, b in zip(scores, scores[1:]))
    assert S(x=(0.8, 24.0, 1.0)) == pytest.approx(math.exp(-0.2))


def test_score_always_in_unit_interval():
    for x in (SPEC, (0.0, 0.0, 0.0), (-5.0, 1e9, 3.0), (1e300, -1e300, 1e300)):
        for n in (0, 7, 50, 999):
            for ok in (True, False):
                assert 0.0 <= S(x=x, n=n, chain_ok=ok) <= 1.0


# -- validation rejects -----------------------------------------------------------

def test_features_rejects_zero_cap():
    with pytest.raises(ValueError):
        features(0, 0, 24.0, 1.0)


@pytest.mark.parametrize("cap", [True, 100.0, -1, "100", None])
def test_features_rejects_non_int_or_non_positive_cap(cap):
    with pytest.raises(ValueError):
        features(50, cap, 24.0, 1.0)


@pytest.mark.parametrize("remaining", [True, 50.0, None])
def test_features_rejects_non_int_remaining(remaining):
    with pytest.raises(ValueError):
        features(remaining, 100, 24.0, 1.0)


def test_features_integer_cents_ratio():
    assert features(6000, 10_000, 24.0, 1.0) == (0.6, 24.0, 1.0)


@pytest.mark.parametrize("n", [True, False, -1, 1.0, 50.0, "50", None])
def test_score_rejects_bool_negative_or_non_int_receipt_count(n):
    with pytest.raises(ValueError):
        S(n=n)


@pytest.mark.parametrize("bad", [math.nan, math.inf, -math.inf])
def test_score_rejects_nan_and_inf_anywhere(bad):
    with pytest.raises(ValueError):
        S(x=(bad, 24.0, 1.0))
    with pytest.raises(ValueError):
        S(spec=(1.0, bad, 1.0))
    with pytest.raises(ValueError):
        S(sc=(0.5, bad, 0.25))
    with pytest.raises(ValueError):
        S(w=(bad, 0.25, 0.25))


@pytest.mark.parametrize("w", [(0.5, 0.5, 0.5), (0.2, 0.2, 0.2), (1.5, -0.25, -0.25), (0.0, 0.0, 0.0)])
def test_score_rejects_weights_not_summing_to_one_or_negative(w):
    with pytest.raises(ValueError):
        S(w=w)


@pytest.mark.parametrize("sc", [(0.0, 24.0, 0.25), (0.5, -24.0, 0.25), (0.5, 24.0, 0.0)])
def test_score_rejects_zero_or_negative_scale(sc):
    with pytest.raises(ValueError):
        S(sc=sc)


@pytest.mark.parametrize("ok", [0, 1, None, "true"])
def test_score_rejects_non_bool_chain_ok(ok):
    with pytest.raises(ValueError):
        S(chain_ok=ok)


@pytest.mark.parametrize("x", [(1.0, 24.0), (1.0, 24.0, 1.0, 1.0), (True, 24.0, 1.0)])
def test_score_rejects_malformed_vectors(x):
    with pytest.raises(ValueError):
        S(x=x)


@pytest.mark.parametrize("n_min", [0, -1, True, 50.0])
def test_score_rejects_bad_n_min(n_min):
    with pytest.raises(ValueError):
        score(SPEC, SPEC, W, SC, 50, True, n_min=n_min)


@pytest.mark.parametrize("tau", [0, 0.0, -0.1, 1.0000001, 2, math.nan, math.inf, True, "0.7", None])
def test_validate_tau_rejects_out_of_range_and_non_numbers(tau):
    with pytest.raises(ValueError):
        validate_tau(tau)


def test_validate_tau_accepts_open_closed_interval():
    assert validate_tau(1) == 1.0
    assert validate_tau(TAU_DEFAULT) == 0.70
    assert validate_tau(1e-9) == 1e-9


@pytest.mark.parametrize("s", [-0.01, 1.01, math.nan])
def test_advisory_verdict_rejects_score_outside_unit_interval(s):
    with pytest.raises(ValueError):
        advisory_verdict(s, TAU_DEFAULT)


def test_calibration_module_imports_no_paypal_client():
    import permit.calibration as cal
    src = open(cal.__file__, encoding="utf-8").read()
    assert "settlement" not in src and "paypal_client" not in src
