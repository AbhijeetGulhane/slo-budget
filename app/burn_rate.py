"""Burn-rate calculation and multi-window alert evaluation.

Everything here is a pure function of numbers — no Prometheus, no clock, no I/O —
so the alert logic can be unit-tested exhaustively without a running stack.
The engine layer (engine.py) supplies the error ratios; this layer decides what
they mean.

Definitions
-----------
error ratio   : bad_events / total_events, measured over a window.
error budget  : 1 - objective, the allowed error ratio (e.g. 0.001 for 99.9%).
burn rate     : error_ratio / error_budget.
                r = 1  -> budget is spent exactly at the end of the window.
                r = 14.4 -> spending 14.4x too fast; budget gone in W/14.4.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Optional

from .config import SLO, BurnRatePolicy


def burn_rate(error_ratio: float, error_budget: float) -> float:
    """error_ratio / error_budget. Budget must be > 0 (objective < 100%)."""
    if error_budget <= 0.0:
        raise ValueError("error_budget must be > 0; a 100% objective has no budget to burn")
    if error_ratio < 0.0:
        raise ValueError("error_ratio cannot be negative")
    return error_ratio / error_budget


def budget_consumed(burn_rate_value: float, window_seconds: float, total_window_seconds: float) -> float:
    """Fraction of the *total* window budget consumed if `burn_rate_value` holds
    for `window_seconds`. This is what turns a burn rate into "2% of the month"."""
    return burn_rate_value * (window_seconds / total_window_seconds)


def hours_to_exhaustion(remaining_fraction: float, burn_rate_value: float, total_window_hours: float) -> float:
    """How long the remaining budget lasts if the current burn rate holds.

    remaining_fraction: 1.0 = full budget, 0.0 = exhausted.
    Returns +inf when nothing is burning."""
    if burn_rate_value <= 0.0:
        return math.inf
    return remaining_fraction * total_window_hours / burn_rate_value


@dataclass(frozen=True)
class WindowEval:
    """Burn rate over a single window."""

    window: str
    error_ratio: float
    burn_rate: float
    over_threshold: bool


@dataclass(frozen=True)
class AlertEval:
    """Result of evaluating one multi-window policy."""

    policy: str
    severity: str
    threshold: float
    long: WindowEval
    short: WindowEval
    firing: bool
    long_request_rate: Optional[float] = None
    suppressed_low_traffic: bool = False

    def reason(self) -> str:
        if self.suppressed_low_traffic:
            rate = "unknown" if self.long_request_rate is None else f"{self.long_request_rate:.3g} req/s"
            return (
                f"suppressed: {self.long.window} traffic {rate} below the low-traffic floor "
                f"(both windows were over {self.threshold:g}x, but the ratio is too noisy to page on)"
            )
        if self.firing:
            return (
                f"both windows over {self.threshold:g}x: "
                f"{self.long.window}={self.long.burn_rate:.1f}x, "
                f"{self.short.window}={self.short.burn_rate:.1f}x"
            )
        if not self.long.over_threshold:
            return f"long window {self.long.window} at {self.long.burn_rate:.2f}x is below {self.threshold:g}x"
        # long hot but short cool -> the burn already stopped; short is doing its reset job.
        return (
            f"short window {self.short.window} at {self.short.burn_rate:.2f}x has dropped below "
            f"{self.threshold:g}x (burn no longer active)"
        )


def evaluate_policy(
    policy: BurnRatePolicy,
    slo: SLO,
    long_error_ratio: float,
    short_error_ratio: float,
    long_request_rate: Optional[float] = None,
) -> AlertEval:
    """Evaluate one policy against a long-window and short-window error ratio.

    The multi-window AND condition is the whole point: the long window supplies
    significance (a real chunk of budget is going), the short window supplies a
    fast reset (once errors stop, the alert clears within ~short_window instead
    of lingering for the full long window). A brief blip that only lights the
    short window never pages; a stale condition that only lights the long window
    de-asserts as soon as the short window cools.

    `long_request_rate` (req/s over the long window) enables the low-traffic
    guard: when supplied and below `policy.min_request_rate`, the alert is
    suppressed even if both windows are over threshold, because at trickle traffic
    a single error blows the ratio past the line without meaning anything. Pass
    None (the default) to skip the guard entirely.
    """
    budget = slo.error_budget
    long_r = burn_rate(long_error_ratio, budget)
    short_r = burn_rate(short_error_ratio, budget)

    # Relative epsilon so the float representation of the threshold (e.g. 14.4 *
    # 0.001 / 0.001 landing at 14.39999999999) doesn't flap the alert at the exact
    # boundary. Well below any real burn-rate step, so it never masks a true breach.
    trip = policy.threshold * (1.0 - 1e-9)
    long_eval = WindowEval(policy.long_window, long_error_ratio, long_r, long_r >= trip)
    short_eval = WindowEval(policy.short_window, short_error_ratio, short_r, short_r >= trip)

    both_hot = long_eval.over_threshold and short_eval.over_threshold

    # Guard only bites when a rate is actually supplied and the policy opts in.
    guard_active = long_request_rate is not None and policy.min_request_rate > 0.0
    low_traffic = guard_active and long_request_rate < policy.min_request_rate
    suppressed = both_hot and low_traffic
    firing = both_hot and not low_traffic

    return AlertEval(
        policy=policy.name,
        severity=policy.severity,
        threshold=policy.threshold,
        long=long_eval,
        short=short_eval,
        firing=firing,
        long_request_rate=long_request_rate,
        suppressed_low_traffic=suppressed,
    )
