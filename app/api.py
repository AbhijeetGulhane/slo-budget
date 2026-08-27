"""FastAPI surface for the SLO service.

  GET /healthz      liveness
  GET /slo/status   budget position + per-policy alert evaluation (JSON)
  GET /metrics      Prometheus exposition: burn-rate gauges + firing state,
                    so the service that *computes* burn rates is itself scrapeable
                    and graphable in Grafana.

Why expose /metrics at all: it closes the loop. Prometheus scrapes search-api,
this service reads those metrics and computes burn rates, and Prometheus scrapes
*this* service so the burn rates and firing state land back on a dashboard next
to the raw SLIs.
"""

from __future__ import annotations

from fastapi import FastAPI, Response

from .engine import SLOEngine
from .prometheus import PrometheusClient

app = FastAPI(title="SLO Error Budget Service", version="0.1.0")
engine = SLOEngine(prometheus=PrometheusClient())


@app.get("/healthz")
def healthz() -> dict:
    return {"status": "ok"}


@app.get("/slo/status")
def slo_status() -> dict:
    report = engine.evaluate()
    b = report.budget
    return {
        "slo": report.slo_name,
        "objective": b.objective,
        "window_days": b.window_days,
        "budget": {
            "error_budget": b.error_budget,
            "consumed_fraction": round(b.consumed_fraction, 6),
            "remaining_fraction": round(b.remaining_fraction, 6),
            "current_burn_rate": round(b.current_burn_rate, 3),
            "hours_to_exhaustion": (None if b.hours_to_exhaustion == float("inf")
                                    else round(b.hours_to_exhaustion, 1)),
        },
        "page": report.page,
        "alerts": [
            {
                "policy": a.policy,
                "severity": a.severity,
                "firing": a.firing,
                "threshold": a.threshold,
                "long": {"window": a.long.window, "burn_rate": round(a.long.burn_rate, 2)},
                "short": {"window": a.short.window, "burn_rate": round(a.short.burn_rate, 2)},
                "reason": a.reason(),
            }
            for a in report.alerts
        ],
    }


@app.get("/metrics")
def metrics() -> Response:
    report = engine.evaluate()
    b = report.budget
    lines = [
        "# HELP slo_error_budget_remaining_ratio Fraction of error budget remaining (1=full).",
        "# TYPE slo_error_budget_remaining_ratio gauge",
        f'slo_error_budget_remaining_ratio{{slo="{report.slo_name}"}} {b.remaining_fraction}',
        "# HELP slo_burn_rate Burn rate per policy window.",
        "# TYPE slo_burn_rate gauge",
    ]
    for a in report.alerts:
        lines.append(
            f'slo_burn_rate{{slo="{report.slo_name}",policy="{a.policy}",window="{a.long.window}"}} {a.long.burn_rate}'
        )
        lines.append(
            f'slo_burn_rate{{slo="{report.slo_name}",policy="{a.policy}",window="{a.short.window}"}} {a.short.burn_rate}'
        )
    lines += [
        "# HELP slo_alert_firing 1 if the multi-window burn-rate policy is firing.",
        "# TYPE slo_alert_firing gauge",
    ]
    for a in report.alerts:
        lines.append(
            f'slo_alert_firing{{slo="{report.slo_name}",policy="{a.policy}",severity="{a.severity}"}} {int(a.firing)}'
        )
    return Response("\n".join(lines) + "\n", media_type="text/plain; version=0.0.4")
