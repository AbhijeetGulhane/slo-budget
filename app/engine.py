"""SLO engine — orchestrates queries, budget accounting, and alert evaluation.

One evaluation pass:
  1. Collect the distinct windows needed by all policies (dedup: fast+slow only
     need 5m/1h/6h/30m -> 4 queries, not 8).
  2. Query Prometheus once per window.
  3. Compute the rolling error budget position (consumed / remaining / ETA).
  4. Evaluate every burn-rate policy against its long+short windows.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, Optional

import httpx

from .burn_rate import (
    burn_rate as compute_burn_rate,
    hours_to_exhaustion,
    evaluate_policy,
    AlertEval,
)
from .config import SLO, BurnRatePolicy, DEFAULT_POLICIES, SEARCH_API_SLO
from .prometheus import PrometheusClient


@dataclass
class BudgetStatus:
    objective: float
    window_days: int
    error_budget: float
    # Rolling position, measured over the full SLO window:
    error_ratio_window: float
    consumed_fraction: float          # 0..1+, share of the month's budget spent
    remaining_fraction: float         # 1 - consumed, floored at 0
    current_burn_rate: float          # burn rate over the full window
    hours_to_exhaustion: float


@dataclass
class SLOReport:
    slo_name: str
    budget: BudgetStatus
    alerts: list[AlertEval] = field(default_factory=list)

    @property
    def firing(self) -> list[AlertEval]:
        return [a for a in self.alerts if a.firing]

    @property
    def page(self) -> bool:
        return any(a.firing for a in self.alerts)


@dataclass
class SLOEngine:
    prometheus: PrometheusClient
    slo: SLO = SEARCH_API_SLO
    policies: tuple[BurnRatePolicy, ...] = DEFAULT_POLICIES

    def _windows(self) -> list[str]:
        seen: list[str] = []
        for p in self.policies:
            for w in (p.long_window, p.short_window):
                if w not in seen:
                    seen.append(w)
        # The full-window ratio backs budget accounting.
        full = f"{self.slo.window_days}d"
        if full not in seen:
            seen.append(full)
        return seen

    def evaluate(self, client: Optional[httpx.Client] = None) -> SLOReport:
        owns = client is None
        client = client or httpx.Client(timeout=self.prometheus.timeout_seconds)
        try:
            ratios = {w: self.prometheus.error_ratio(w, client=client) for w in self._windows()}
            # Only the long (significance) windows gate on traffic.
            long_windows = {p.long_window for p in self.policies}
            request_rates = {w: self.prometheus.request_rate(w, client=client) for w in long_windows}
        finally:
            if owns:
                client.close()
        return self.build_report(ratios, request_rates)

    def build_report(
        self,
        ratios: dict[str, float],
        request_rates: Optional[dict[str, float]] = None,
    ) -> SLOReport:
        """Pure: build a report from already-collected window ratios.
        Split out from evaluate() so it is directly unit-testable.

        request_rates maps long-window -> req/s and drives the low-traffic guard.
        Omit it (None) to evaluate without the guard, e.g. in ratio-only tests."""
        full_window = f"{self.slo.window_days}d"
        window_ratio = ratios[full_window]
        window_burn = compute_burn_rate(window_ratio, self.slo.error_budget)

        # Over the whole window, budget_consumed = burn_rate * (window/window) = burn_rate.
        consumed = window_burn
        remaining = max(0.0, 1.0 - consumed)
        eta = hours_to_exhaustion(remaining, window_burn, self.slo.window_hours)

        budget = BudgetStatus(
            objective=self.slo.objective,
            window_days=self.slo.window_days,
            error_budget=self.slo.error_budget,
            error_ratio_window=window_ratio,
            consumed_fraction=consumed,
            remaining_fraction=remaining,
            current_burn_rate=window_burn,
            hours_to_exhaustion=eta,
        )

        alerts = [
            evaluate_policy(
                p,
                self.slo,
                ratios[p.long_window],
                ratios[p.short_window],
                long_request_rate=(request_rates.get(p.long_window) if request_rates else None),
            )
            for p in self.policies
        ]
        return SLOReport(slo_name=self.slo.name, budget=budget, alerts=alerts)
