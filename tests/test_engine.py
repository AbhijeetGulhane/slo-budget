import math

import httpx
import pytest

from app.engine import SLOEngine
from app.config import SLO, DEFAULT_POLICIES
from app.prometheus import PrometheusClient

SLO_999 = SLO(name="search-api-availability", objective=0.999, window_days=30)


def make_engine():
    return SLOEngine(prometheus=PrometheusClient(), slo=SLO_999, policies=DEFAULT_POLICIES)


def test_windows_are_deduped():
    eng = make_engine()
    # fast(1h,5m) + slow(6h,30m) + full(30d) = 5 distinct windows, no dupes
    assert eng._windows() == ["1h", "5m", "6h", "30m", "30d"]


def test_report_healthy_service():
    eng = make_engine()
    # search-api chaos test measured ~0.08% monthly spend: ratio ~8e-6 over the month
    ratios = {"1h": 0.0, "5m": 0.0, "6h": 0.0, "30m": 0.0, "30d": 8e-6}
    report = eng.build_report(ratios)
    assert not report.page
    assert report.budget.consumed_fraction == pytest.approx(0.008, abs=1e-3)  # 0.8% of budget
    assert report.budget.remaining_fraction == pytest.approx(0.992, abs=1e-3)
    assert report.budget.hours_to_exhaustion > 720  # months of runway


def test_report_fast_burn_pages():
    eng = make_engine()
    ratios = {"1h": 0.02, "5m": 0.02, "6h": 0.0, "30m": 0.0, "30d": 0.0001}
    report = eng.build_report(ratios)
    assert report.page
    firing = [a.policy for a in report.firing]
    assert "fast_burn" in firing
    assert "slow_burn" not in firing


def test_report_exhausted_budget_floors_at_zero():
    eng = make_engine()
    # month-long ratio above the budget -> consumed > 1, remaining floored to 0
    ratios = {"1h": 0.0, "5m": 0.0, "6h": 0.0, "30m": 0.0, "30d": 0.002}
    report = eng.build_report(ratios)
    assert report.budget.consumed_fraction == pytest.approx(2.0)
    assert report.budget.remaining_fraction == 0.0


def test_engine_queries_prometheus_via_fake_transport():
    # Prove the query path end-to-end without a real server.
    def handler(request: httpx.Request) -> httpx.Response:
        expr = request.url.params["query"]
        if expr.startswith("sum(rate("):
            value = "50"  # healthy request rate (req/s) -> clears the low-traffic floor
        else:
            # recording-rule ratio form: job:slo_error_ratio:ratio_rate<window>
            value = "0.02" if expr.endswith(("1h", "5m")) else "0.0"
        return httpx.Response(200, json={"status": "success", "data": {"result": [
            {"metric": {}, "value": [1700000000, value]}
        ]}})

    transport = httpx.MockTransport(handler)
    client = httpx.Client(transport=transport)
    eng = make_engine()
    report = eng.evaluate(client=client)
    assert report.page
    assert any(a.policy == "fast_burn" and a.firing for a in report.alerts)


# --- budget-position boundaries: exactly 0% and 100% consumed -------------

def test_zero_percent_budget_consumed():
    # Clean 0% edge: no errors over the month -> full budget, infinite runway.
    eng = make_engine()
    report = eng.build_report({"1h": 0.0, "5m": 0.0, "6h": 0.0, "30m": 0.0, "30d": 0.0})
    assert report.budget.consumed_fraction == 0.0
    assert report.budget.remaining_fraction == 1.0
    assert report.budget.hours_to_exhaustion == math.inf
    assert not report.page


def test_hundred_percent_budget_consumed():
    # Clean 100% edge: month-long error ratio exactly equal to the budget means
    # the budget is spent precisely at window close. Float division lands at
    # 0.99999999..., so assert with tolerance; remaining is ~0, never negative.
    eng = make_engine()
    budget = SLO_999.error_budget  # 0.001
    report = eng.build_report({"1h": 0.0, "5m": 0.0, "6h": 0.0, "30m": 0.0, "30d": budget})
    assert report.budget.consumed_fraction == pytest.approx(1.0, abs=1e-9)
    assert report.budget.remaining_fraction == pytest.approx(0.0, abs=1e-9)
    assert report.budget.remaining_fraction >= 0.0  # floor holds, never negative
    assert report.budget.hours_to_exhaustion == pytest.approx(0.0, abs=1e-6)


def test_report_low_traffic_suppresses_page():
    # Hot ratios on fast-burn windows, but 1h traffic below the floor -> no page.
    eng = make_engine()
    ratios = {"1h": 0.05, "5m": 0.05, "6h": 0.0, "30m": 0.0, "30d": 0.0001}
    rates = {"1h": 0.1, "6h": 0.1}  # below the 1.0 req/s floor
    report = eng.build_report(ratios, rates)
    assert not report.page
    fast = next(a for a in report.alerts if a.policy == "fast_burn")
    assert fast.suppressed_low_traffic
