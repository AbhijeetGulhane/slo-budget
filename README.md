# SLO Error Budget Service — burn-rate alerting

Reads the `search-api` SLI from Prometheus, computes the real-time error budget,
and evaluates **multi-window, multi-burn-rate** alerts straight out of the Google
SRE Workbook chapter *Alerting on SLOs* (https://sre.google/workbook/alerting-on-slos/ — free, and the single most useful thing to have read cold for this topic).

## The policy

30-day rolling window, request-based availability SLO (objective 99.9%, budget 0.001):

| policy      | severity | long | short | burn rate | budget burned in long window | intent            |
|-------------|----------|------|-------|-----------|------------------------------|-------------------|
| `fast_burn` | page     | 1h   | 5m    | 14.4      | 2%                           | hard outage       |
| `slow_burn` | page     | 6h   | 30m   | 6         | 5%                           | steady bleed      |

## The math (all in `app/burn_rate.py`, all unit-tested)

```
error_budget      = 1 - objective                 # 0.001 for 99.9%
burn_rate         = error_ratio / error_budget    # 1.0 = spends budget exactly at window end
budget_consumed   = burn_rate * (window / W)      # 14.4 * (1h/720h) = 2%
hours_to_exhaust  = remaining * W_hours / burn_rate   # full budget @14.4x -> 720/14.4 = 50h
```

## Why two windows (the part interviewers push on)

An alert fires only when the burn rate is over threshold on **both** the long
and the short window:

```
fire = burn_rate(long) >= threshold  AND  burn_rate(short) >= threshold
```

- **Long window** decides *significance* — a real 2% (or 5%) slice of the month
  is actually going.
- **Short window** is the *reset guard*. Once the errors stop, the short window
  cools within minutes and the alert de-asserts, instead of lingering for the
  full long window (a 1h stale page is how you train people to ignore pages).
- A brief blip that only lights the short window never pages; a stale condition
  that only lights the long window clears as soon as the short window cools.
  Both failure modes are covered by tests in `tests/test_burn_rate.py`.

## Layout

```
app/
  config.py       SLO + BurnRatePolicy; canonical windows, with a self-check that
                  threshold * (long/W) lands on the documented 2% / 5%.
  burn_rate.py    Pure functions: burn_rate, budget_consumed, exhaustion, evaluate_policy.
  prometheus.py   Query client; prefers recording rules over inline rate().
  engine.py       Dedups windows, queries once each, builds the budget + alert report.
  api.py          /healthz, /slo/status, /metrics (Prometheus exposition).
rules/
  recording.rules.yml   Precompute per-window error ratios.
  alerting.rules.yml    Native-Prometheus equivalent of evaluate() — ship one or the other.
tests/                   24 tests, no live Prometheus needed.
```

## Run

```bash
pip install -r requirements.txt
pytest -q                          # 24 passing
uvicorn app.api:app --port 8080    # then: curl localhost:8080/slo/status
```

## Design decisions & tradeoffs

- **Service vs. Prometheus rules.** `rules/` contains the exact same policy as
  native alerting rules. The service is worth its weight only when SLOs are
  dynamic or multi-tenant, or you want an API + richer budget accounting for
  dashboards; otherwise the rules are fewer moving parts. Both are provided so
  the choice is explicit, not accidental.
- **Recording rules in the hot path.** The client queries
  `job:slo_error_ratio:ratio_rate<w>` rather than recomputing `rate(...[6h])` on
  every evaluation — that work belongs in Prometheus, once per scrape.
- **Inclusive threshold with a relative epsilon.** `14.4 * 0.001 / 0.001` can
  land at 14.3999999 in float; the comparison uses a `1e-9` relative tolerance so
  the boundary doesn't flap. Far below any real burn-rate step, so it never masks
  a breach.

## Known gaps (honest list)

- **No low-traffic guard.** At tiny request volumes a single error spikes the
  ratio and can page. The standard fix is a minimum-events floor or an absolute
  error-count AND-term; not implemented here yet.
- **Instant queries, not range.** Budget accounting uses one `30d` instant query;
  a `max_over_time` or proper budget integral would be sturdier across restarts.
- **Single SLI (availability, 5xx ratio).** No latency SLO yet — that needs a
  histogram-based "good = requests under N ms" ratio, same alerting logic on top.
- **No alert dedup/inhibition.** During a hard outage both policies fire; you'd
  add Alertmanager inhibition so slow_burn is suppressed while fast_burn pages.

## Reading

- Google SRE Workbook, *Alerting on SLOs* — sre.google/workbook/alerting-on-slos/ (free; essential).
- Google SRE Workbook, *Implementing SLOs* — sre.google/workbook/implementing-slos/ (free; the budget-policy framing).
