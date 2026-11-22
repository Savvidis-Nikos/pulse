# Simulated data system: guide for AI agents

Read this instead of the code. It tells you how to generate data, what every file contains, which invariants
hold, and how to add scenarios, tables or metrics without breaking anything.

## 1. Generate data
```bash
python -m marketplace.run [--seed 7] [--out data/eye] [--scenarios all|core] [--user-sample 0.01] [--no-scenarios]
```
- `all` (default) = 22 scenarios; `core` = the original 8 (reproduces the original data bit for bit in the
  original columns); `--no-scenarios` = clean baseline.
- `--user-sample 0` disables the user panel (no users/sessions/clickstream/orders files; ~2x faster).
- Deterministic: same seed + same actions = identical output. Runtime ~25 s for 504 hours.
- Output dir `data/eye/` is git-ignored; regenerate instead of committing data.
- Live mode (one hour per tick, scenarios injected while running):
  `python -m marketplace.live start` (history 1-14 Nov, clock 15 Nov 00:00), `... loop --seconds 2`,
  `... inject <name>` (any name from `... list`), `... tick --hours N`. Live mode has the user panel off. Each tick rewrites `data/eye/`; `data/eye/live.json`
  holds the current clock (`now`), so a monitoring process can poll it.

## 2. Code map (only what you need)
| File | Role |
|---|---|
| `marketplace/catalog.py` | static setup: countries, platforms, categories, rates, AOV, COGS, SKU catalog (Zipf popularity) |
| `marketplace/world.py` | `State` (all component settings) + `Marketplace.step(ts)`: one hour for 48 segments; `_funnel()` draws the funnel |
| `marketplace/scenarios.py` | actions (functions that change `State` and/or log events), scenario builders, timeline, ground truth |
| `marketplace/ops.py` | operations layer: ads auctions, sellers, services, app health, support, pipeline health, payments, wholesale, calendar, status feed |
| `marketplace/context.py` | context layer: audience split (browser, user type), product sales, market, external events, pricing / catalog / browser-bug / competitor effects |
| `marketplace/users.py` | user panel: sampled sessions played click by click, consistent with facts |
| `marketplace/run.py` | CLI, `simulate()`, `write()` (writes every table + `injected.json`) |
| `marketplace/live.py` | live loop + injection (uses the same builders) |

The data layer has no dependency on any monitoring code. The monitoring system (metric catalog, agents) is
built separately and only reads the files in section 3.

## 3. Output contract (`data/eye/`)
Grain "seg" = ts (hourly, `YYYY-MM-DD HH:00:00`) × country × platform × category (48 rows per hour).

| File | Grain | Columns |
|---|---|---|
| facts.csv | seg | sessions, searches, zero_result_searches, product_views, add_to_carts, checkouts, payment_attempts, orders, gmv, refunds, discounts, cogs, ad_cost, feed_clicks, sponsored_sessions, ad_impressions, ad_revenue, marketing_cost, gmv_3p, marketplace_fees, chargebacks, ivt_sessions, retail_gross_profit, fulfilment_cost, payment_cost |
| audience.csv | seg × browser × user_type | sessions, product_views, add_to_carts, checkouts, payment_attempts, orders, gmv (browser ∈ Chrome/Safari/Edge/Firefox on web, `app` on ios/android; user_type ∈ new/returning) |
| inventory.csv | ts × category | listed_skus, in_stock_skus |
| products.csv | ts × category × top-50 SKU | popularity_rank, view_share, in_stock, supplier |
| product_sales.csv | ts × category × top-50 SKU | price, price_change, in_stock, views, add_to_carts, orders |
| events.csv | one row per change | type (release/config/flag/campaign/supply/purchasing/pricing/catalog/maintenance/rollback), name, component, owner, deployed_at, first_exposure_at, country, platform, category ('*' = all), planned, ts, end |
| calendar.csv | one row per known event | name, kind, country, start, end, expected_impact (text), metrics (';'-list), expected_low, expected_high (relative range), announced_at |
| status_feed.csv | one row per post | ts, source (external/internal), channel, component, region, severity, state, title |
| market.csv | ts × country × category | price_index (ours / competitors, 1 = parity), demand_index (100 = normal), competitor_promo |
| external_events.csv | one row per competitor move | ts (observed), name, kind, competitor, country, category, discount, start, end, source |
| completeness.csv | ts × source | completeness (web_analytics, orders_db, ads_platform, inventory) |
| pipeline_health.csv | ts × source | completeness, lag_minutes, last_update, heartbeat_ok (+ seller_feeds, support_desk) |
| service_health.csv | ts × service × platform | requests, errors, error_rate, p99_ms, capacity_util (api-gateway, search, recommendations, catalog, cart, checkout, payments, ads-server; catalog-ingest on `backend`) |
| services.csv | ts × platform | checkout/payments/search requests and errors (wide view) |
| app_health.csv | ts × platform | app_version, payment_sdk, sessions, crashes, crash_free_rate |
| payments.csv | ts × country × platform × method | provider (PayFlow/WalletPay/GiroLokal), auth_attempts, auth_failures |
| sellers.csv | ts × seller × category | type (1P/3P), listed_skus, active_offers, orders, gmv, commission, feed_status, last_feed_sync, feed_errors |
| ads_auctions.csv | ts × category | ad_requests, impressions, fill_rate, no_fill_* reasons, timeouts, clicks, ctr, avg_cpc, floor_cpc, ad_revenue, rpm |
| ads_advertisers.csv | ts × advertiser | pacer, daily_budget, spend, spent_today, budget_used, cpc, impressions, clicks, invalid_clicks, attributed_orders/sales, roas, expected_spent_today, pacing_index, status, exhausted_at |
| feed_exposure.csv | ts × category | For You model, inventory_filter, out_of_stock_exposure, impressions, predicted_ctr, actual_ctr, feature_freshness_h |
| wholesale.csv | ts × category × supplier | forecast_units, po_units, due_units, received_units, demand_share, delivery_status |
| support_tickets.csv | ts × country × topic | tickets, avg_sentiment, median_first_response_min |
| support_ticket_samples.csv | sampled tickets | ts, country, topic, subject (customer language) |
| users.csv / sessions.csv / clickstream.csv / orders.csv / order_items.csv | user panel (1 % of sessions) | see headers; clickstream has page, sku, query, results, dwell_s, version (served by), error_code, is_exit |
| users_meta.json | | sample rate of the panel |
| injected.json | | ground truth: `scenarios[]` with id, kind (fault/innocent/data), metric, segment, start, end, cause, first_cause_event, optional drivers/trap/evidence/pattern |

## 4. Invariants (check these after any change)
1. **Core reproduction**: `--scenarios core` reproduces the original columns of facts/inventory/events/completeness
   exactly. Compare against a copy made before your change.
2. **Isolation**: a scenario only changes its own segments and hours. The baseline funnel is drawn from the main
   stream `default_rng([seed, hour])` with baseline inputs `x0`; segments whose inputs differ (`x1`) are re-drawn
   from `ops.rng` and blended (`np.where(affected, alt, base)`). Never add random draws to the main stream or
   change the draw order in `_funnel()`; multiply new effects into `x1` inputs (default arrays of ones in `x0`).
3. **Random streams in use** (pick a new id for new layers): main `[seed, hour]`; panel `[seed, 4241]`,
   `[seed, 4242, hour]`; ops `[seed, 9100]`, `[seed, 9101, hour]`, payments `[seed, 9200, hour]`; context
   `[seed, 9200]`, `[seed, 9201, hour]`. Free: e.g. 9300+.
4. **Consistency**: audience.csv sums exactly to facts per seg (nested thinning: carts ⊆ views, checkouts ⊆ carts,
   attempts ⊆ checkouts, orders ⊆ attempts); panel × (1/sample) ≈ facts within ~2 %; sellers/ads/services are
   driven by the same state as facts.
5. **Faults need a fingerprint, harmless changes need a record**: a fault changes `State` (numbers move) and
   usually logs an event (decoys too); harmless changes are in the change log or calendar; data scenarios change
   only completeness / pipeline health / frozen values.
6. **Detectability**: scenarios are sized for a detector that alerts at |z| ≥ 4.5 and |rel| ≥ 10 % over a 3-hour window vs the median of the
   same hours on 7 previous days. Small segments (e.g. Safari ≈ 6 % of traffic) need large effects (≥ 30-50 %).

## 5. Add a scenario (recipe)
1. **State**: if no existing knob fits, add a field to `State` in `world.py` (empty default = no effect).
2. **Effect**: apply it in `Marketplace.step` to `x1` (traffic: `organic/sponsored/camp`; rates: `ctr, zero,
   feed_mult, avail, disc_push, pay, checkout_mult, cart_mult, attempt_mult, intent`), or in the ops/context layer
   for non-funnel tables. Keep it a multiplier on segments selected by a mask.
3. **Action**: in `scenarios.py`, write `def my_action(mp, ts, ...)` that appends to the State field and logs the
   change with `mp.log(ts, type_, name, component, owner, spec={"country":..,"platform":..,"category":..},
   planned=True)`. Calendar entries: `calendar_entry(...)` with metrics + expected range; status posts:
   `status_post(...)`; competitor moves: `competitor_promo(...)`.
4. **Builder**: `def b_my_scenario(t0, announce=None): return acts, start, end` where `acts` is a list of
   `(timestamp, action_fn, kwargs)`. Add a decoy (`harmless(...)`) at the same time when it makes sense.
5. **Register**: add to `EXTRA_BUILDERS` and `EXTRA_T0` (pick a free slot in 8-21 Nov; days 1-7 are history),
   and a truth dict to `EXTRA_SCENARIOS` (`id, kind, metric, segment, start, end, cause, first_cause_event`).
   `first_cause_event` must be a substring of the event / calendar / market record name the monitoring system should name.
   Live injection works automatically (`marketplace/live.py` uses `EXTRA_BUILDERS`).
6. **Monitoring coverage**: if the change type is new, tell the monitoring team which component it belongs to
   and which metrics it can move.
7. **Verify**: regenerate, check the effect size on the scenario metric vs the same hours of the previous week,
   check invariant 1, and score the monitoring system against `injected.json`.

## 6. Add a table
- Operations-type table: accumulate rows in `Ops.rows[...]` (or `Context.rows[...]`) inside `hour()`, return it
  from `tables()`; `run.write()` writes every table returned by `mp.ops_tables()` automatically as `<name>.csv`.
- Use its own random stream (invariant 3) and only read `f` (the final facts rows of the hour), `State` and
  component objects, so it stays consistent.
- For the monitoring system to use it: give it a `ts` column and tell the monitoring team its file name, grain
  and dimensions.

## 7. Add a metric
Metric definitions live in the monitoring system, not here. The data layer only provides raw columns:
add a new raw measure as a column in `world.py` and list it under its source in `SOURCES` (`world.py`) so its
completeness is tracked. For the metric itself: ratios must be a ratio of sums, never an average of ratios.
Avoid redundant count metrics (e.g. `x - y`): they create alerts that are not linked to the funnel.

## 8. Gotchas
- Several people edit `world.py`; make small targeted edits and re-read before patching.
- Bash heredocs with nested quotes fail on Windows Git Bash; write patch scripts to a file and run them.
- A changed probability changes how many random numbers an hour consumes - this is why effects go through `x1`.
- Late order rows are applied to facts after the operations layer; the context layer splits the final rows.
- Stock metrics (inventory) have no weekday rhythm; flow metrics (facts) do.
- Scenario windows overlap on some days; keep new faults in different segments/metrics from running ones.
