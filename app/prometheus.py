"""Thin Prometheus query client.

Returns the error ratio (bad/total) over a window for the search-api SLI. The
runtime metric name stays `search_api_requests_total` even though the repo was
renamed to ml-inference-sre, matching the stable identifiers in the cluster.

Design note for the interview: the client prefers a *recording rule*
(`job:slo_error_ratio:ratio_rate<w>`, defined in rules/recording.rules.yml) when
one exists, and only falls back to computing rate() inline. Recomputing
rate(...[6h]) on every evaluation is expensive and duplicates work Prometheus
can do once per scrape; recording rules move that cost out of the hot path.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import httpx


class PrometheusError(RuntimeError):
    pass


@dataclass
class PrometheusClient:
    base_url: str = "http://prometheus.search-sre:9090"
    metric: str = "search_api_requests_total"
    error_selector: str = 'status=~"5.."'
    timeout_seconds: float = 5.0
    # If a recording rule exists it is queried directly; otherwise we build the ratio inline.
    recording_rule_prefix: Optional[str] = "job:slo_error_ratio:ratio_rate"

    def _inline_ratio_query(self, window: str) -> str:
        good_or_bad = f"sum(rate({self.metric}{{{self.error_selector}}}[{window}]))"
        total = f"sum(rate({self.metric}[{window}]))"
        # `... or vector(0)` keeps the ratio defined (0) when there is zero traffic
        # in the window, instead of returning an empty result.
        return f"({good_or_bad} / {total}) or vector(0)"

    def query_expr(self, window: str) -> str:
        if self.recording_rule_prefix:
            return f"{self.recording_rule_prefix}{window}"
        return self._inline_ratio_query(window)

    def error_ratio(self, window: str, client: Optional[httpx.Client] = None) -> float:
        """Instant query for the error ratio over `window`. Returns a float in [0, 1]."""
        expr = self.query_expr(window)
        owns_client = client is None
        client = client or httpx.Client(timeout=self.timeout_seconds)
        try:
            resp = client.get(f"{self.base_url}/api/v1/query", params={"query": expr})
            resp.raise_for_status()
            payload = resp.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise PrometheusError(f"query failed for {expr!r}: {exc}") from exc
        finally:
            if owns_client:
                client.close()

        if payload.get("status") != "success":
            raise PrometheusError(f"prometheus returned status {payload.get('status')} for {expr!r}")
        result = payload["data"]["result"]
        if not result:
            # No series (rule not yet materialised, or truly no data) -> treat as 0 burn.
            return 0.0
        return float(result[0]["value"][1])

    def request_rate(self, window: str, client: Optional[httpx.Client] = None) -> float:
        """Total request rate (req/s) over `window`, for the low-traffic guard."""
        expr = f"sum(rate({self.metric}[{window}])) or vector(0)"
        owns_client = client is None
        client = client or httpx.Client(timeout=self.timeout_seconds)
        try:
            resp = client.get(f"{self.base_url}/api/v1/query", params={"query": expr})
            resp.raise_for_status()
            payload = resp.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise PrometheusError(f"query failed for {expr!r}: {exc}") from exc
        finally:
            if owns_client:
                client.close()
        if payload.get("status") != "success":
            raise PrometheusError(f"prometheus returned status {payload.get('status')} for {expr!r}")
        result = payload["data"]["result"]
        if not result:
            return 0.0
        return float(result[0]["value"][1])
