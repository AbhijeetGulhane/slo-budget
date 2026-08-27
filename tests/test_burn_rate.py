import math

import pytest

from app.config import SLO, FAST_BURN, SLOW_BURN
from app.burn_rate import (
    burn_rate,
    budget_consumed,
    hours_to_exhaustion,
    evaluate_policy,
)

SLO_999 = SLO(name="test", objective=0.999, window_days=30)  # budget = 0.001
BUDGET = 0.001
W_SECONDS = 30 * 86400
W_HOURS = 30 * 24  # 720


# --- burn rate ------------------------------------------------------------

def test_burn_rate_basic():
    # 1.44% errors against a 0.1% budget = 14.4x
    assert burn_rate(0.0144, BUDGET) == pytest.approx(14.4)


def test_burn_rate_at_objective_is_one():
    # error ratio exactly equal to the budget burns at exactly 1x
    assert burn_rate(BUDGET, BUDGET) == pytest.approx(1.0)


def test_burn_rate_hard_outage():
    # 100% errors at a 99.9% objective = 1000x burn
    assert burn_rate(1.0, BUDGET) == pytest.approx(1000.0)


def test_burn_rate_zero_budget_raises():
    with pytest.raises(ValueError):
        burn_rate(0.01, 0.0)  # a 100% objective has no budget


def test_burn_rate_negative_ratio_raises():
    with pytest.raises(ValueError):
        burn_rate(-0.01, BUDGET)


# --- the canonical Workbook derivations -----------------------------------

def test_fast_burn_consumes_2pct_in_one_hour():
    consumed = budget_consumed(14.4, 3600, W_SECONDS)
    assert consumed == pytest.approx(0.02, abs=1e-4)  # 2% of the month


def test_slow_burn_consumes_5pct_in_six_hours():
    consumed = budget_consumed(6.0, 6 * 3600, W_SECONDS)
    assert consumed == pytest.approx(0.05, abs=1e-4)  # 5% of the month


def test_policy_self_check_matches_workbook():
    assert FAST_BURN.budget_consumed_at_threshold == pytest.approx(0.02, abs=1e-3)
    assert SLOW_BURN.budget_consumed_at_threshold == pytest.approx(0.05, abs=1e-3)


# --- time to exhaustion ---------------------------------------------------

def test_eta_full_budget_at_fast_burn():
    # full budget, burning 14.4x, 720h window -> 720/14.4 = 50h
    assert hours_to_exhaustion(1.0, 14.4, W_HOURS) == pytest.approx(50.0)


def test_eta_no_burn_is_infinite():
    assert hours_to_exhaustion(1.0, 0.0, W_HOURS) == math.inf


def test_eta_scales_with_remaining_budget():
    # half the budget left -> half the time
    assert hours_to_exhaustion(0.5, 14.4, W_HOURS) == pytest.approx(25.0)


# --- multi-window firing: the heart of the design -------------------------

def test_fast_burn_fires_when_both_windows_hot():
    # sustained 2% error ratio (=20x burn) on both 1h and 5m
    ev = evaluate_policy(FAST_BURN, SLO_999, long_error_ratio=0.02, short_error_ratio=0.02)
    assert ev.firing
    assert ev.long.burn_rate == pytest.approx(20.0)
    assert ev.short.burn_rate == pytest.approx(20.0)


def test_fast_burn_does_not_fire_on_short_only_blip():
    # a brief spike: 5m window hot, but 1h window hasn't accumulated enough.
    # long below threshold -> no page. This is the "don't wake me for a blip" case.
    ev = evaluate_policy(FAST_BURN, SLO_999, long_error_ratio=0.001, short_error_ratio=0.05)
    assert not ev.firing
    assert not ev.long.over_threshold
    assert ev.short.over_threshold


def test_fast_burn_resets_when_short_cools():
    # burn already stopped: 1h window still hot (lagging average), 5m has cooled.
    # short below threshold -> alert clears fast instead of lingering an hour.
    ev = evaluate_policy(FAST_BURN, SLO_999, long_error_ratio=0.02, short_error_ratio=0.0005)
    assert not ev.firing
    assert ev.long.over_threshold
    assert not ev.short.over_threshold
    assert "no longer active" in ev.reason()


def test_threshold_is_inclusive_boundary():
    # exactly 14.4x on both windows should fire (>=)
    ratio_at_threshold = 14.4 * BUDGET  # 0.0144
    ev = evaluate_policy(FAST_BURN, SLO_999, ratio_at_threshold, ratio_at_threshold)
    assert ev.firing


def test_just_below_threshold_does_not_fire():
    ratio = 14.39 * BUDGET
    ev = evaluate_policy(FAST_BURN, SLO_999, ratio, ratio)
    assert not ev.firing


def test_slow_burn_fires_at_6x():
    ratio = 6.0 * BUDGET  # 0.006
    ev = evaluate_policy(SLOW_BURN, SLO_999, ratio, ratio)
    assert ev.firing
    assert ev.long.window == "6h" and ev.short.window == "30m"


def test_slow_burn_ignores_fast_transient():
    # a 3x sustained bleed trips neither policy: too slow to page on, correctly.
    ratio = 3.0 * BUDGET
    fast = evaluate_policy(FAST_BURN, SLO_999, ratio, ratio)
    slow = evaluate_policy(SLOW_BURN, SLO_999, ratio, ratio)
    assert not fast.firing and not slow.firing


def test_zero_traffic_zero_burn_no_fire():
    ev = evaluate_policy(FAST_BURN, SLO_999, 0.0, 0.0)
    assert not ev.firing
    assert ev.long.burn_rate == 0.0


# --- explicit 0% / 100% budget boundaries ---------------------------------

def test_budget_consumed_zero_is_0pct():
    # 0% edge: no burn consumes exactly none of the budget.
    assert budget_consumed(0.0, 3600, W_SECONDS) == 0.0


def test_budget_consumed_full_window_is_100pct():
    # 100% edge (pure form): burning at 1x for the whole window spends exactly
    # the whole budget. Computed as burn_rate * (W/W), so no float division bite.
    assert budget_consumed(1.0, W_SECONDS, W_SECONDS) == 1.0


def test_slo_rejects_objective_boundaries():
    # 0% and 100% objectives are both nonsensical SLOs and must be rejected:
    # objective=1.0 -> zero budget (nothing to burn); objective=0.0 -> all budget.
    with pytest.raises(ValueError):
        SLO(name="t", objective=1.0)
    with pytest.raises(ValueError):
        SLO(name="t", objective=0.0)


# --- low-traffic guard ----------------------------------------------------

def test_low_traffic_suppresses_despite_both_windows_hot():
    # Both windows well over 14.4x, but the 1h window is basically idle:
    # 0.2 req/s (~720 requests/hour). A single error there is noise. Suppress.
    ev = evaluate_policy(FAST_BURN, SLO_999, 0.05, 0.05, long_request_rate=0.2)
    assert not ev.firing
    assert ev.suppressed_low_traffic
    assert "suppressed" in ev.reason()


def test_sufficient_traffic_allows_firing():
    # Same hot ratios, but real traffic (50 req/s) -> the ratio is meaningful -> page.
    ev = evaluate_policy(FAST_BURN, SLO_999, 0.05, 0.05, long_request_rate=50.0)
    assert ev.firing
    assert not ev.suppressed_low_traffic


def test_guard_skipped_when_rate_not_supplied():
    # Backward compatible: no rate given -> guard is a no-op, fires as before.
    ev = evaluate_policy(FAST_BURN, SLO_999, 0.05, 0.05)
    assert ev.firing
    assert not ev.suppressed_low_traffic


def test_guard_does_not_suppress_when_not_firing():
    # Low traffic AND below threshold -> plain not-firing, not a "suppressed" state.
    ev = evaluate_policy(FAST_BURN, SLO_999, 0.0, 0.0, long_request_rate=0.1)
    assert not ev.firing
    assert not ev.suppressed_low_traffic
