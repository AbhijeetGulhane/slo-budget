"""SLO and burn-rate alert configuration.

Canonical multi-window, multi-burn-rate policy from the Google SRE Workbook,
chapter "Alerting on SLOs" (https://sre.google/workbook/alerting-on-slos/).

The two page-severity policies:

    severity  long   short  burn rate  budget consumed in long window
    --------  -----  -----  ---------  ------------------------------
    page      1h     5m     14.4       2%   (fast burn  -> hard outage)
    page      6h     30m    6          5%   (slow burn  -> steady bleed)

The relationship that ties the numbers together, for a 30-day window W:

    budget_consumed = burn_rate * (long_window / W)

    fast: 14.4 * (1h  / 720h) = 0.02  = 2%
    slow: 6    * (6h  / 720h) = 0.05  = 5%

Both are validated in `BurnRatePolicy.__post_init__` so the config can never
silently drift out of that relationship.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

_DURATION_RE = re.compile(r"^(\d+)(s|m|h|d)$")
_UNIT_SECONDS = {"s": 1, "m": 60, "h": 3600, "d": 86400}


def parse_duration(text: str) -> float:
    """Parse a Prometheus-style duration ('5m', '1h', '6h', '30d') to seconds."""
    m = _DURATION_RE.match(text.strip())
    if not m:
        raise ValueError(f"invalid duration {text!r} (expected forms like '5m', '1h', '6h')")
    value, unit = m.groups()
    return int(value) * _UNIT_SECONDS[unit]


@dataclass(frozen=True)
class SLO:
    """A request-based SLO over a rolling window.

    objective: the target success ratio, e.g. 0.999 for "99.9% of requests succeed".
    window_days: the rolling error-budget window (30 is the near-universal default).
    """

    name: str
    objective: float
    window_days: int = 30

    def __post_init__(self) -> None:
        if not 0.0 < self.objective < 1.0:
            raise ValueError(f"objective must be in (0, 1), got {self.objective}")
        if self.window_days <= 0:
            raise ValueError("window_days must be positive")

    @property
    def error_budget(self) -> float:
        """Allowed error ratio = 1 - objective (e.g. 0.001 for 99.9%)."""
        return 1.0 - self.objective

    @property
    def window_seconds(self) -> float:
        return self.window_days * 86400.0

    @property
    def window_hours(self) -> float:
        return self.window_days * 24.0


@dataclass(frozen=True)
class BurnRatePolicy:
    """One multi-window burn-rate alert rule.

    Fires only when the burn rate over BOTH the long and short window exceeds
    `threshold`. The long window decides significance; the short window is the
    reset guard that makes the alert de-assert quickly once the burn stops.
    """

    name: str
    severity: str
    long_window: str
    short_window: str
    threshold: float
    window_days: int = 30
    # Low-traffic guard: minimum request rate (req/s) over the long window before
    # the policy may fire. Below this the error ratio is statistical noise — a
    # single 500 among a handful of requests can exceed the burn threshold — so the
    # alert is suppressed. 0.0 disables the guard.
    #
    # Default 1.0 req/s => >=3600 requests in a 1h window, >=21600 in 6h; far above
    # the point where one error trips 14.4x (~69 requests). TUNE to search-api's
    # real off-peak baseline; a service that legitimately idles overnight wants this
    # lower, one that never should go quiet wants it higher.
    min_request_rate: float = 1.0
    # Tolerance for the consistency self-check (accounts for the 14.4 rounding).
    _tolerance: float = field(default=1e-3, repr=False)

    def __post_init__(self) -> None:
        if parse_duration(self.short_window) >= parse_duration(self.long_window):
            raise ValueError("short_window must be shorter than long_window")
        if self.threshold <= 0:
            raise ValueError("threshold must be positive")
        # Self-check: burn_rate * (long/W) should equal a sane budget fraction (<1).
        consumed = self.budget_consumed_at_threshold
        if not 0.0 < consumed < 1.0:
            raise ValueError(
                f"policy {self.name!r} implies {consumed:.3f} budget consumed in one "
                f"long window; expected a fraction in (0, 1)"
            )

    @property
    def long_seconds(self) -> float:
        return parse_duration(self.long_window)

    @property
    def short_seconds(self) -> float:
        return parse_duration(self.short_window)

    @property
    def budget_consumed_at_threshold(self) -> float:
        """Fraction of the total window's budget consumed if the burn rate sits
        exactly at `threshold` for one whole long window. 2% fast, 5% slow."""
        window_seconds = self.window_days * 86400.0
        return self.threshold * (self.long_seconds / window_seconds)


# --- Canonical policy set (30-day window) ---------------------------------

FAST_BURN = BurnRatePolicy(
    name="fast_burn",
    severity="page",
    long_window="1h",
    short_window="5m",
    threshold=14.4,
)

SLOW_BURN = BurnRatePolicy(
    name="slow_burn",
    severity="page",
    long_window="6h",
    short_window="30m",
    threshold=6.0,
)

DEFAULT_POLICIES = (FAST_BURN, SLOW_BURN)

# The SLO the search-api service is held to. Its 30-day chaos test measured a
# 0.08% monthly error budget spend, i.e. comfortably inside a 99.9% objective.
SEARCH_API_SLO = SLO(name="search-api-availability", objective=0.999, window_days=30)
